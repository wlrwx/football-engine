"""Temperature Scaling 校准层

参考 Gunnerista/worldcup-predictor 的方法:
- 在最终 logits 上乘 1/T，T 通过验证集最小化 NLL 求得
- 不改变概率排序，只压缩/拉伸分布
- ECE 从 0.103 → 0.027 的关键步骤

用法:
    cal = TemperatureScaler(save_path)
    cal.fit(predicted_probs, actuals)  # 拟合 T
    calibrated = cal.calibrate(probs)  # 推理时用
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np


class TemperatureScaler:
    """Temperature Scaling 校准器

    核心原理:
        logits = log(probs)
        calibrated = softmax(logits / T)
    T > 1: 压缩分布（降低过度自信）
    T < 1: 拉伸分布（增强信心）
    T = 1: 不变

    拟合: 最小化 NLL (negative log-likelihood) on validation set
    """

    def __init__(self, save_path: Path):
        self.save_path = save_path
        self.temperature: float = 1.0
        self._n_samples: int = 0
        self._ece_before: float = 0.0
        self._ece_after: float = 0.0
        self._fitted: bool = False

        if save_path.exists():
            self._load()

    def _load(self):
        try:
            data = json.loads(self.save_path.read_text(encoding="utf-8"))
            self.temperature = data.get("temperature", 1.0)
            self._n_samples = data.get("n_samples", 0)
            self._ece_before = data.get("ece_before", 0.0)
            self._ece_after = data.get("ece_after", 0.0)
            self._fitted = data.get("fitted", False)
        except Exception:
            pass

    def save(self):
        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        self.save_path.write_text(json.dumps({
            "temperature": round(self.temperature, 6),
            "n_samples": self._n_samples,
            "ece_before": round(self._ece_before, 4),
            "ece_after": round(self._ece_after, 4),
            "fitted": self._fitted,
        }, indent=2), encoding="utf-8")

    def fit(self, predicted_probs: np.ndarray, actuals: np.ndarray):
        """拟合温度参数 T

        使用黄金分割搜索最小化 NLL，无需 scipy

        Args:
            predicted_probs: (n, 3) 模型原始概率
            actuals: (n,) 实际结果 0/1/2
        """
        n = len(predicted_probs)
        self._n_samples = n
        if n < 30:
            print(f"  [Temperature] 样本不足 ({n} < 30)，跳过")
            return

        probs = np.clip(predicted_probs, 1e-8, 1 - 1e-8)

        # ECE before
        self._ece_before = self._compute_ece(probs, actuals)

        # NLL as function of T
        def nll(T):
            logits = np.log(probs)
            scaled = logits / T
            # softmax
            scaled = scaled - scaled.max(axis=1, keepdims=True)
            exp = np.exp(scaled)
            cal_probs = exp / exp.sum(axis=1, keepdims=True)
            cal_probs = np.clip(cal_probs, 1e-15, 1 - 1e-15)
            return -np.mean(np.log(cal_probs[np.arange(n), actuals]))

        # Golden section search for T in [0.5, 5.0]
        lo, hi = 0.5, 5.0
        phi = (math.sqrt(5) - 1) / 2  # 0.618
        for _ in range(50):
            a = hi - phi * (hi - lo)
            b = lo + phi * (hi - lo)
            if nll(a) < nll(b):
                hi = b
            else:
                lo = a

        self.temperature = (lo + hi) / 2
        self._fitted = True

        # ECE after
        cal_probs = self._calibrate_array(probs)
        self._ece_after = self._compute_ece(cal_probs, actuals)

        self.save()
        print(f"  [Temperature] T={self.temperature:.3f}, "
              f"ECE {self._ece_before:.4f} → {self._ece_after:.4f} "
              f"({(self._ece_after - self._ece_before) / max(self._ece_before, 0.001) * 100:+.1f}%)")

    def calibrate(self, probs: tuple[float, float, float]) -> tuple[float, float, float]:
        """校准单场预测"""
        if not self._fitted or self.temperature == 1.0:
            return probs

        arr = np.array(list(probs), dtype=float)
        cal = self._calibrate_array(arr.reshape(1, -1))[0]

        # 归一化
        total = cal.sum()
        if total > 0:
            cal = cal / total
        return tuple(float(x) for x in cal)

    def _calibrate_array(self, probs: np.ndarray) -> np.ndarray:
        """批量校准"""
        if not self._fitted or self.temperature == 1.0:
            return probs

        probs = np.clip(probs, 1e-8, 1 - 1e-8)
        logits = np.log(probs)
        scaled = logits / self.temperature
        # softmax
        scaled = scaled - scaled.max(axis=-1, keepdims=True)
        exp = np.exp(scaled)
        cal = exp / exp.sum(axis=-1, keepdims=True)
        return cal

    @staticmethod
    def _compute_ece(probs: np.ndarray, actuals: np.ndarray, n_bins: int = 10) -> float:
        """Classwise average Expected Calibration Error in [0, 1]."""
        n = len(probs)
        n_classes = probs.shape[1] if probs.ndim == 2 else 0
        if n == 0 or n_classes == 0:
            return 0.0
        ece = 0.0
        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        for idx in range(n_classes):
            y_pred = probs[:, idx]
            y_true = (actuals == idx).astype(float)
            for b in range(n_bins):
                if b == n_bins - 1:
                    mask = (y_pred >= bin_edges[b]) & (y_pred <= bin_edges[b + 1])
                else:
                    mask = (y_pred >= bin_edges[b]) & (y_pred < bin_edges[b + 1])
                if mask.sum() > 0:
                    avg_pred = y_pred[mask].mean()
                    avg_true = y_true[mask].mean()
                    ece += abs(avg_pred - avg_true) * mask.sum() / n
        return float(ece / n_classes)

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def temperature_value(self) -> float:
        return self.temperature
