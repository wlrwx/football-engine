from __future__ import annotations
"""TimeSeriesSplit 回测 - 严格时间切分，杜绝未来信息泄露

设计原则:
  - Expanding window: train on [0..t], predict t+1
  - 或 Rolling window: train on [t-N..t], predict t+1
  - 评估指标: Brier Score, LogLoss, RPS, Hit Rate, ROI
  - 支持 per-league 分组评估
  - 输出逐折指标 + 汇总统计
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

import numpy as np


@dataclass
class BacktestConfig:
    """回测配置"""
    mode: str = "expanding"       # "expanding" | "rolling"
    n_splits: int = 10            # 折数
    test_size: int = 50           # 每折测试集大小（场次数）
    min_train_size: int = 200     # 最小训练集大小
    rolling_window: int = 2000    # rolling模式窗口大小
    # 评估阈值
    value_threshold: float = 1.05  # 期望值阈值（prob*odds > 此值才算value bet）


@dataclass
class FoldResult:
    """单折回测结果"""
    fold_idx: int
    train_size: int
    test_size: int
    brier_score: float
    log_loss: float
    rps: float                   # Ranked Probability Score
    hit_rate: float              # 命中率（最大概率选项）
    n_value_bets: int            # 正期望注数
    value_hit_rate: float        # 正期望命中率
    roi: float                   # 投资回报率
    # 分联赛
    per_league: dict = field(default_factory=dict)


@dataclass
class BacktestReport:
    """完整回测报告"""
    config: dict
    n_folds: int
    folds: list  # list[FoldResult]
    # 汇总
    avg_brier: float = 0.0
    avg_logloss: float = 0.0
    avg_rps: float = 0.0
    avg_hit_rate: float = 0.0
    avg_roi: float = 0.0
    std_brier: float = 0.0
    total_matches: int = 0


def ranked_probability_score(probs: np.ndarray, actuals: np.ndarray) -> float:
    """RPS (Ranked Probability Score) - 三分类专用评估

    比 Brier 更敏感于排序正确性。
    RPS = sum of (cumulative_prob - cumulative_actual)^2 / (K-1)

    Args:
        probs: (n, 3) 预测概率 [home, draw, away]
        actuals: (n,) 实际结果 0/1/2

    Returns:
        平均 RPS（越低越好，0=完美）
    """
    n = len(probs)
    if n == 0:
        return 0.0

    rps_total = 0.0
    for i in range(n):
        # 累积概率
        cum_prob = np.cumsum(probs[i])
        # 累积实际（one-hot 的 cumsum）
        actual_vec = np.zeros(3)
        actual_vec[actuals[i]] = 1.0
        cum_actual = np.cumsum(actual_vec)
        # RPS = sum((cum_prob - cum_actual)^2) / (K-1)
        rps_total += np.sum((cum_prob - cum_actual) ** 2) / 2.0  # K-1 = 2

    return rps_total / n


def brier_score(probs: np.ndarray, actuals: np.ndarray) -> float:
    """多分类 Brier Score

    BS = (1/N) * sum(sum((p_i - o_i)^2))
    """
    n = len(probs)
    if n == 0:
        return 0.0

    one_hot = np.zeros_like(probs)
    for i in range(n):
        one_hot[i, actuals[i]] = 1.0

    return float(np.mean(np.sum((probs - one_hot) ** 2, axis=1)))


def log_loss(probs: np.ndarray, actuals: np.ndarray, eps: float = 1e-15) -> float:
    """多分类 LogLoss"""
    n = len(probs)
    if n == 0:
        return 0.0

    # Clip 避免 log(0)
    probs_clipped = np.clip(probs, eps, 1 - eps)
    loss = 0.0
    for i in range(n):
        loss -= np.log(probs_clipped[i, actuals[i]])
    return loss / n


class TimeSeriesSplitter:
    """时间序列切分回测器

    严格保证: 训练集时间 < 测试集时间，无未来信息泄露。
    """

    def __init__(self, config: Optional[BacktestConfig] = None):
        self.config = config or BacktestConfig()

    def split(self, n_samples: int) -> list[tuple[np.ndarray, np.ndarray]]:
        """生成时间序列切分

        Args:
            n_samples: 总样本数（按时间排序）

        Returns:
            [(train_indices, test_indices), ...] 每折的索引
        """
        cfg = self.config
        splits = []

        if cfg.mode == "expanding":
            # Expanding window: 训练集不断增长
            # 计算每折的起始测试位置
            total_test = cfg.n_splits * cfg.test_size
            if n_samples < cfg.min_train_size + total_test:
                # 样本不够，减少折数
                available_test = n_samples - cfg.min_train_size
                actual_splits = max(1, available_test // cfg.test_size)
            else:
                actual_splits = cfg.n_splits

            # 均匀分布测试折
            test_starts = np.linspace(
                cfg.min_train_size,
                n_samples - cfg.test_size,
                actual_splits,
                dtype=int,
            )

            for start in test_starts:
                train_idx = np.arange(0, start)
                test_idx = np.arange(start, min(start + cfg.test_size, n_samples))
                if len(train_idx) >= cfg.min_train_size and len(test_idx) > 0:
                    splits.append((train_idx, test_idx))

        elif cfg.mode == "rolling":
            # Rolling window: 固定大小训练窗口
            total_test = cfg.n_splits * cfg.test_size
            if n_samples < cfg.rolling_window + cfg.test_size:
                return splits

            test_starts = np.linspace(
                cfg.rolling_window,
                n_samples - cfg.test_size,
                cfg.n_splits,
                dtype=int,
            )

            for start in test_starts:
                train_start = max(0, start - cfg.rolling_window)
                train_idx = np.arange(train_start, start)
                test_idx = np.arange(start, min(start + cfg.test_size, n_samples))
                if len(test_idx) > 0:
                    splits.append((train_idx, test_idx))

        return splits

    def run_backtest(
        self,
        features: np.ndarray,
        labels: np.ndarray,
        odds: Optional[np.ndarray],
        leagues: Optional[np.ndarray],
        predict_fn: Callable[[np.ndarray, np.ndarray], np.ndarray],
    ) -> BacktestReport:
        """执行完整回测

        Args:
            features: 全部特征 (n, d)，按时间排序
            labels: 全部标签 (n,)，0/1/2
            odds: 赔率 (n, 3)，用于计算 ROI（可选）
            leagues: 联赛标签 (n,)，用于分组评估（可选）
            predict_fn: 预测函数 (train_X, train_y) -> model，
                       返回的 model 需有 predict_proba(X) 方法

        Returns:
            BacktestReport
        """
        cfg = self.config
        n = len(features)
        splits = self.split(n)

        if not splits:
            print("  [回测] 样本不足，无法切分")
            return BacktestReport(config=cfg.__dict__, n_folds=0, folds=[])

        fold_results = []

        for fold_idx, (train_idx, test_idx) in enumerate(splits):
            X_train, y_train = features[train_idx], labels[train_idx]
            X_test, y_test = features[test_idx], labels[test_idx]

            # 训练模型（由外部提供的 predict_fn）
            try:
                model = predict_fn(X_train, y_train)
                if model is None:
                    continue
                probs = model.predict_proba(X_test)
                if probs is None:
                    continue
            except Exception as e:
                print(f"  [回测] Fold {fold_idx} 失败: {e}")
                continue

            # 确保 probs 是 numpy array
            probs = np.array(probs)

            # 计算指标
            bs = brier_score(probs, y_test)
            ll = log_loss(probs, y_test)
            rps = ranked_probability_score(probs, y_test)

            # 命中率
            pred_outcomes = np.argmax(probs, axis=1)
            hits = (pred_outcomes == y_test).sum()
            hit_rate = hits / len(y_test)

            # Value betting ROI
            n_value = 0
            value_hits = 0
            total_staked = 0.0
            total_return = 0.0

            if odds is not None:
                test_odds = odds[test_idx]
                for i in range(len(y_test)):
                    best_idx = pred_outcomes[i]
                    prob = probs[i, best_idx]
                    odd = test_odds[i, best_idx] if test_odds[i, best_idx] > 0 else 0

                    if odd > 0 and prob * odd > cfg.value_threshold:
                        n_value += 1
                        total_staked += 1.0
                        if best_idx == y_test[i]:
                            value_hits += 1
                            total_return += odd

            value_hit_rate = value_hits / n_value if n_value > 0 else 0.0
            roi = (total_return - total_staked) / total_staked if total_staked > 0 else 0.0

            # 分联赛评估
            per_league = {}
            if leagues is not None:
                test_leagues = leagues[test_idx]
                unique_leagues = np.unique(test_leagues)
                for lg in unique_leagues:
                    lg_mask = test_leagues == lg
                    if lg_mask.sum() >= 5:
                        lg_probs = probs[lg_mask]
                        lg_actuals = y_test[lg_mask]
                        per_league[str(lg)] = {
                            "brier": round(brier_score(lg_probs, lg_actuals), 4),
                            "hit_rate": round(
                                (np.argmax(lg_probs, axis=1) == lg_actuals).mean(), 4
                            ),
                            "n": int(lg_mask.sum()),
                        }

            fold_results.append(FoldResult(
                fold_idx=fold_idx,
                train_size=len(train_idx),
                test_size=len(test_idx),
                brier_score=round(bs, 4),
                log_loss=round(ll, 4),
                rps=round(rps, 4),
                hit_rate=round(hit_rate, 4),
                n_value_bets=n_value,
                value_hit_rate=round(value_hit_rate, 4),
                roi=round(roi, 4),
                per_league=per_league,
            ))

        # 汇总
        report = BacktestReport(
            config=cfg.__dict__,
            n_folds=len(fold_results),
            folds=fold_results,
        )

        if fold_results:
            briers = [f.brier_score for f in fold_results]
            report.avg_brier = round(np.mean(briers), 4)
            report.std_brier = round(np.std(briers), 4)
            report.avg_logloss = round(np.mean([f.log_loss for f in fold_results]), 4)
            report.avg_rps = round(np.mean([f.rps for f in fold_results]), 4)
            report.avg_hit_rate = round(np.mean([f.hit_rate for f in fold_results]), 4)
            report.avg_roi = round(np.mean([f.roi for f in fold_results]), 4)
            report.total_matches = sum(f.test_size for f in fold_results)

        return report

    def save_report(self, report: BacktestReport, output_path: Path):
        """保存回测报告"""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "config": report.config,
            "n_folds": report.n_folds,
            "summary": {
                "avg_brier": report.avg_brier,
                "std_brier": report.std_brier,
                "avg_logloss": report.avg_logloss,
                "avg_rps": report.avg_rps,
                "avg_hit_rate": report.avg_hit_rate,
                "avg_roi": report.avg_roi,
                "total_matches": report.total_matches,
            },
            "folds": [
                {
                    "fold": f.fold_idx,
                    "train_size": f.train_size,
                    "test_size": f.test_size,
                    "brier": f.brier_score,
                    "logloss": f.log_loss,
                    "rps": f.rps,
                    "hit_rate": f.hit_rate,
                    "value_bets": f.n_value_bets,
                    "value_hit_rate": f.value_hit_rate,
                    "roi": f.roi,
                    "per_league": f.per_league,
                }
                for f in report.folds
            ],
        }
        output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
