"""
Wilson Score + 贝叶斯信任系统 — 小样本保护。

来源: jingcai-analysis 信任度模块
核心思想:
  - 命中率不能只看表面 (如 3中2=66.7%)，样本太少时不可信
  - Wilson Score 给出置信区间下界，样本越少、下界越低
  - 贝叶斯先验: 用历史全局命中率做先验，小样本时回归均值
  - 双指标信任: wilson_lower × bayesian_posterior 的加权
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class TrustConfig:
    """信任系统参数"""
    confidence_level: float = 0.95   # Wilson置信水平
    prior_weight: int = 20           # 贝叶斯先验等效样本量
    global_hit_rate: float = 0.45    # 全局历史命中率（先验均值）
    min_samples: int = 5             # 低于此样本数信任度打折
    wilson_weight: float = 0.6       # 信任度中Wilson占比
    bayes_weight: float = 0.4        # 信任度中贝叶斯占比


def wilson_score_lower(
    hits: int,
    total: int,
    confidence: float = 0.95,
) -> float:
    """
    Wilson Score 置信区间下界。

    当 total=0 时返回 0。
    典型: 3中2 → wilson_lower ≈ 0.342 (远低于表面的0.667)
         100中67 → wilson_lower ≈ 0.577 (接近表面的0.67)
    """
    if total == 0:
        return 0.0

    z = _z_score(confidence)
    p_hat = hits / total
    n = total

    denominator = 1 + z * z / n
    center = p_hat + z * z / (2 * n)
    spread = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * n)) / n)

    lower = (center - spread) / denominator
    return max(0.0, lower)


def wilson_score_upper(
    hits: int,
    total: int,
    confidence: float = 0.95,
) -> float:
    """Wilson Score 置信区间上界"""
    if total == 0:
        return 1.0

    z = _z_score(confidence)
    p_hat = hits / total
    n = total

    denominator = 1 + z * z / n
    center = p_hat + z * z / (2 * n)
    spread = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * n)) / n)

    upper = (center + spread) / denominator
    return min(1.0, upper)


def bayesian_posterior(
    hits: int,
    total: int,
    prior_mean: float = 0.45,
    prior_weight: int = 20,
) -> float:
    """
    贝叶斯后验估计（Beta-Binomial共轭）。

    等效于: (hits + prior_weight*prior_mean) / (total + prior_weight)
    小样本时强烈回归先验，大样本时趋近观测值。
    """
    alpha_prior = prior_weight * prior_mean
    beta_prior = prior_weight * (1 - prior_mean)
    alpha_post = alpha_prior + hits
    beta_post = beta_prior + (total - hits)
    return alpha_post / (alpha_post + beta_post)


class TrustSystem:
    """
    双指标信任系统。

    用法:
        ts = TrustSystem()
        trust = ts.compute_trust(hits=3, total=5)
        # trust ∈ [0, 1]，越高越可信
        adjusted_prob = ts.adjust_probability(raw_prob, hits, total)
    """

    def __init__(self, config: TrustConfig | None = None):
        self.cfg = config or TrustConfig()

    def compute_trust(self, hits: int, total: int) -> float:
        """
        计算综合信任度。

        返回 [0, 1]:
          - 接近1: 样本充足，命中率高且稳定
          - 接近0: 样本不足或命中率低
        """
        if total == 0:
            return 0.0

        w_lower = wilson_score_lower(hits, total, self.cfg.confidence_level)
        b_post = bayesian_posterior(
            hits, total, self.cfg.global_hit_rate, self.cfg.prior_weight
        )

        trust = self.cfg.wilson_weight * w_lower + self.cfg.bayes_weight * b_post

        # 小样本惩罚
        if total < self.cfg.min_samples:
            penalty = total / self.cfg.min_samples
            trust *= penalty

        return round(min(max(trust, 0.0), 1.0), 4)

    def adjust_probability(
        self, raw_prob: float, hits: int, total: int
    ) -> float:
        """
        用信任度调整原始概率。

        低信任度时，概率向先验（全局命中率）收缩。
        """
        trust = self.compute_trust(hits, total)
        # 低信任 → 向先验收缩
        adjusted = trust * raw_prob + (1 - trust) * self.cfg.global_hit_rate
        return round(adjusted, 4)

    def confidence_interval(
        self, hits: int, total: int
    ) -> tuple[float, float]:
        """返回Wilson置信区间 (lower, upper)"""
        return (
            round(wilson_score_lower(hits, total, self.cfg.confidence_level), 4),
            round(wilson_score_upper(hits, total, self.cfg.confidence_level), 4),
        )

    def is_reliable(self, hits: int, total: int, threshold: float = 0.4) -> bool:
        """判断是否足够可靠（信任度超过阈值）"""
        return self.compute_trust(hits, total) >= threshold

    def report(self, hits: int, total: int) -> dict:
        """完整报告"""
        ci = self.confidence_interval(hits, total)
        return {
            "hits": hits,
            "total": total,
            "raw_rate": round(hits / max(total, 1), 4),
            "wilson_lower": ci[0],
            "wilson_upper": ci[1],
            "bayesian_posterior": round(
                bayesian_posterior(hits, total, self.cfg.global_hit_rate, self.cfg.prior_weight), 4
            ),
            "trust_score": self.compute_trust(hits, total),
            "reliable": self.is_reliable(hits, total),
        }


def _z_score(confidence: float) -> float:
    """常用置信水平对应的z值"""
    table = {
        0.90: 1.645,
        0.95: 1.960,
        0.99: 2.576,
    }
    return table.get(confidence, 1.960)
