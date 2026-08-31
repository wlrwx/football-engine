from __future__ import annotations
"""Isotonic 校准层 - 分区间独立校准概率

设计原则:
  - 对每个结果（主胜/平局/客胜）独立拟合 isotonic regression
  - 修正"模型说60%实际只中50%"这类系统性偏差
  - 样本不足时降级为 Platt scaling（sigmoid）
  - 校准曲线持久化，每周重训
  - 参数外部化到 config/prediction.json["calibration"]
"""
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


@dataclass
class CalibrationConfig:
    """校准配置"""
    method: str = "isotonic"       # "isotonic" | "platt" | "temperature"
    min_samples: int = 100         # 低于此数降级为 temperature
    platt_min_samples: int = 30    # Platt 最低样本数
    n_bins_check: int = 10         # 校准检查分箱数
    # 每个结果独立校准
    per_outcome: bool = True       # True=主/平/客各一个校准器


class IsotonicCalibrator:
    """Isotonic 校准器

    对预测概率进行非参数单调校准，确保:
    - 预测 70% 的事件，实际命中率接近 70%
    - 保持概率排序不变（单调性）
    - 每个结果（H/D/A）独立校准

    降级策略:
    - 样本 >= min_samples: Isotonic regression
    - 样本 >= platt_min_samples: Platt scaling (sigmoid)
    - 样本不足: 不校准（原样输出）
    """

    def __init__(self, save_path: Path, config: Optional[CalibrationConfig] = None):
        self.save_path = save_path
        self.config = config or CalibrationConfig()
        # 三个校准器: home, draw, away
        self._calibrators: dict[str, object] = {}
        self._method_used: str = "none"
        self._n_samples: int = 0

        # 尝试加载
        if save_path.exists():
            self._load()

    def _load(self):
        """加载已保存的校准器"""
        try:
            data = pickle.loads(self.save_path.read_bytes())
            self._calibrators = data.get("calibrators", {})
            self._method_used = data.get("method", "none")
            self._n_samples = data.get("n_samples", 0)
            self._shrunk_tables = data.get("shrunk_tables", {})
        except Exception:
            self._calibrators = {}
            self._shrunk_tables = {}

    def save(self):
        """持久化校准器"""
        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "calibrators": self._calibrators,
            "method": self._method_used,
            "n_samples": self._n_samples,
            "shrunk_tables": getattr(self, "_shrunk_tables", {}),
        }
        self.save_path.write_bytes(pickle.dumps(data))

    def fit(self, predicted_probs: np.ndarray, actuals: np.ndarray):
        """拟合校准器

        Args:
            predicted_probs: 预测概率 (n_samples, 3) [p_home, p_draw, p_away]
            actuals: 实际结果 (n_samples,) 值为 0/1/2
        """
        if not HAS_SKLEARN:
            print("  [校准] sklearn 未安装，跳过")
            return

        n = len(predicted_probs)
        self._n_samples = n

        if n < self.config.platt_min_samples:
            print(f"  [校准] 样本不足 ({n} < {self.config.platt_min_samples})，不校准")
            self._method_used = "none"
            return

        # 选择方法
        if n >= self.config.min_samples and self.config.method == "isotonic":
            method = "isotonic"
        else:
            method = "platt"

        self._method_used = method
        outcome_names = ["home", "draw", "away"]

        # 2026-08-13 修正：小样本过拟合（197 场拆三路每路 ~66 场，
        # 高置信端 70%+ 仅 9 场，isotonic 台阶把噪声当信号学——实测
        # 70-80% 段预测 75% 实际只中 44%）。改为：
        #   1) isotonic 拟合后向整体命中率贝叶斯收缩（样本越少收缩越强）
        #   2) 高置信端样本 < high_conf_min 时强制封顶（不许虚高）
        high_conf_min = max(15, n // 8)     # 高置信端最少样本（约 12%）
        high_conf_cap = 0.55                # 高置信端封顶（实测 70%+ 仅 44%）
        shrink = 0.0
        # 2026-08-31 修复: shrink 仅在 isotonic 分支赋值, 30<=n<100 走 platt 分支时
        # 尾部打印 UnboundLocalError → CI 结算步骤连挂(洁净样本刚过30触发此路径)
        self._shrunk_tables: dict[str, tuple[np.ndarray, np.ndarray]] = {}

        for idx, name in enumerate(outcome_names):
            y_pred = predicted_probs[:, idx]
            y_true = (actuals == idx).astype(float)
            base_rate = y_true.mean()       # 该结果整体命中率（收缩先验）

            if method == "isotonic":
                cal = IsotonicRegression(
                    y_min=0.01, y_max=0.99,
                    out_of_bounds="clip",
                )
                cal.fit(y_pred, y_true)
                # 贝叶斯收缩：isotonic 输出向 base_rate 收缩，权重 = 样本量
                # shrink = n / (n + K)，K=50：样本越少越向先验靠
                shrink = n / (n + 50.0)
                # 用 predict 在阈值点取值（兼容不同 sklearn 版本 f_ 类型）
                xs = np.asarray(cal.X_thresholds_, dtype=float)
                ys = np.asarray([float(cal.predict([x])[0]) for x in xs], dtype=float)
                ys = shrink * ys + (1.0 - shrink) * base_rate
                # 高置信端保护：预测 >=0.60 且端内样本少 → 封顶
                hi_mask = xs >= 0.60
                if hi_mask.sum() >= 1 and (y_pred >= 0.60).sum() < high_conf_min:
                    ys[hi_mask] = np.minimum(ys[hi_mask], high_conf_cap)
                # 保证单调不减（收缩后可能破坏 isotonic 单调性）
                ys = np.maximum.accumulate(ys)
                # 保存收缩表（calibrate 时用 np.interp 查表，避免改 sklearn 对象）
                self._shrunk_tables[name] = (xs, ys)
            else:
                # Platt scaling: logistic regression on predicted prob
                cal = LogisticRegression(C=1.0, solver="lbfgs")
                cal.fit(y_pred.reshape(-1, 1), y_true)

            self._calibrators[name] = cal

        self.save()
        print(f"  [校准] {method} 拟合完成: {n} 样本, 3个校准器, 贝叶斯收缩(shr={shrink:.2f})")

    def calibrate(self, probs: tuple[float, float, float]) -> tuple[float, float, float]:
        """校准单场预测概率

        Args:
            probs: (p_home, p_draw, p_away) 原始预测

        Returns:
            校准后的 (p_home, p_draw, p_away)，归一化
        """
        if not self._calibrators or self._method_used == "none":
            return probs

        outcome_names = ["home", "draw", "away"]
        calibrated = []

        for idx, name in enumerate(outcome_names):
            cal = self._calibrators.get(name)
            if cal is None:
                calibrated.append(probs[idx])
                continue

            p = probs[idx]
            if self._method_used == "isotonic":
                # 收缩表优先（贝叶斯收缩后的校准曲线）
                table = getattr(self, "_shrunk_tables", {}).get(name)
                if table is not None:
                    xs, ys = table
                    cal_p = float(np.interp(p, xs, ys, left=ys[0], right=ys[-1]))
                else:
                    cal_p = float(cal.predict([p])[0])
            else:
                # Platt: predict_proba
                cal_p = float(cal.predict_proba([[p]])[0][1])

            calibrated.append(max(0.01, min(0.99, cal_p)))

        # 归一化
        total = sum(calibrated)
        if total > 0:
            calibrated = [c / total for c in calibrated]

        return tuple(calibrated)

    def calibrate_batch(self, probs_array: np.ndarray) -> np.ndarray:
        """批量校准

        Args:
            probs_array: (n_samples, 3) 原始预测

        Returns:
            校准后 (n_samples, 3)
        """
        results = np.array([self.calibrate(tuple(row)) for row in probs_array])
        return results

    def reliability_report(self, predicted_probs: np.ndarray,
                           actuals: np.ndarray) -> dict:
        """生成可靠性报告（校准前后对比）

        Returns:
            {"before": {"ece": float, "bins": [...]}, "after": {...}}
        """
        n_bins = self.config.n_bins_check
        report = {}

        for label, probs in [("before", predicted_probs),
                             ("after", self.calibrate_batch(predicted_probs))]:
            ece = 0.0
            bins = []
            for idx, name in enumerate(["home", "draw", "away"]):
                y_pred = probs[:, idx]
                y_true = (actuals == idx).astype(float)

                bin_edges = np.linspace(0, 1, n_bins + 1)
                bin_data = []
                for b in range(n_bins):
                    mask = (y_pred >= bin_edges[b]) & (y_pred < bin_edges[b + 1])
                    if mask.sum() > 0:
                        avg_pred = y_pred[mask].mean()
                        avg_true = y_true[mask].mean()
                        gap = abs(avg_pred - avg_true)
                        ece += gap * mask.sum() / len(y_pred)
                        bin_data.append({
                            "bin": f"{bin_edges[b]:.1f}-{bin_edges[b+1]:.1f}",
                            "predicted": round(avg_pred, 3),
                            "actual": round(avg_true, 3),
                            "gap": round(gap, 3),
                            "count": int(mask.sum()),
                        })
                bins.append({"outcome": name, "bins": bin_data})

            report[label] = {"ece": round(ece, 4), "bins": bins}

        return report

    @property
    def is_fitted(self) -> bool:
        return bool(self._calibrators) and self._method_used != "none"

    @property
    def method_used(self) -> str:
        return self._method_used
