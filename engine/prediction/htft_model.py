from __future__ import annotations
"""半全场模型 - 9种HT/FT组合概率

竞彩半全场: 主主/主和/主客/和主/和和/和客/客主/客和/客客

方法:
  1. 用xG的42%估算半场Poisson → P(HT=H/D/A)
  2. 条件概率: P(FT|HT) 基于剩余xG + 动量效应
  3. 联赛修正: 从MatchDB读取历史HT/FT分布作为先验

经验参数:
  - 上半场进球占比 ≈ 42-45% (各大联赛略有差异)
  - 半场领先时, 全场胜率 ≈ 75-80% (动量+战术保守)
  - 半场平局时, 全场主胜概率略高于赛前 (主场优势下半场更明显)
"""
import math
from typing import Optional


# 半场进球占比 (联赛可覆盖)
HT_GOAL_RATIO = 0.43

# 动量效应: 半场领先后全场保持领先的概率加成
MOMENTUM_LEAD = 0.12
# 半场平局时主场优势加成
HT_DRAW_HOME_BOOST = 0.04


def poisson_pmf(k: int, lam: float) -> float:
    """Poisson概率质量函数"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def match_outcome_probs(home_xg: float, away_xg: float, max_goals: int = 6) -> tuple:
    """从xG计算 主胜/平/客胜 概率"""
    p_home, p_draw, p_away = 0.0, 0.0, 0.0
    for hg in range(max_goals + 1):
        for ag in range(max_goals + 1):
            p = poisson_pmf(hg, home_xg) * poisson_pmf(ag, away_xg)
            if hg > ag:
                p_home += p
            elif hg == ag:
                p_draw += p
            else:
                p_away += p
    total = p_home + p_draw + p_away
    return p_home / total, p_draw / total, p_away / total


def htft_probabilities(
    home_xg: float,
    away_xg: float,
    ht_ratio: float = HT_GOAL_RATIO,
    league_htft: Optional[dict] = None,
) -> dict:
    """计算9种半全场概率

    Args:
        home_xg: 全场主队预期进球
        away_xg: 全场客队预期进球
        ht_ratio: 上半场进球占比
        league_htft: 联赛历史HT/FT分布 (可选先验)
            {"HH": 0.25, "HD": 0.05, ...}

    Returns:
        {"HH": prob, "HD": prob, ..., "AA": prob}
    """
    # 半场xG
    ht_home_xg = home_xg * ht_ratio
    ht_away_xg = away_xg * ht_ratio

    # P(HT outcome)
    ht_h, ht_d, ht_a = match_outcome_probs(ht_home_xg, ht_away_xg)

    # 全场xG (用于条件概率)
    ft_h, ft_d, ft_a = match_outcome_probs(home_xg, away_xg)

    # 条件概率 P(FT | HT) 带动力效应
    results = {}

    # HT=主胜 → FT条件
    # 领先后大概率保持, 但可能被追平/反超
    p_ft_h_given_ht_h = min(0.85, ft_h + MOMENTUM_LEAD)
    p_ft_d_given_ht_h = max(0.05, (1 - p_ft_h_given_ht_h) * 0.65)
    p_ft_a_given_ht_h = max(0.02, 1 - p_ft_h_given_ht_h - p_ft_d_given_ht_h)

    results["HH"] = ht_h * p_ft_h_given_ht_h
    results["HD"] = ht_h * p_ft_d_given_ht_h
    results["HA"] = ht_h * p_ft_a_given_ht_h

    # HT=平局 → FT条件
    # 平局时主场优势更明显 (下半场体能+战术调整)
    p_ft_h_given_ht_d = ft_h + HT_DRAW_HOME_BOOST
    p_ft_d_given_ht_d = max(0.15, ft_d * 0.85)
    p_ft_a_given_ht_d = max(0.05, 1 - p_ft_h_given_ht_d - p_ft_d_given_ht_d)
    # 归一化
    total_d = p_ft_h_given_ht_d + p_ft_d_given_ht_d + p_ft_a_given_ht_d
    p_ft_h_given_ht_d /= total_d
    p_ft_d_given_ht_d /= total_d
    p_ft_a_given_ht_d /= total_d

    results["DH"] = ht_d * p_ft_h_given_ht_d
    results["DD"] = ht_d * p_ft_d_given_ht_d
    results["DA"] = ht_d * p_ft_a_given_ht_d

    # HT=客胜 → FT条件
    p_ft_a_given_ht_a = min(0.80, ft_a + MOMENTUM_LEAD)
    p_ft_d_given_ht_a = max(0.05, (1 - p_ft_a_given_ht_a) * 0.60)
    p_ft_h_given_ht_a = max(0.03, 1 - p_ft_a_given_ht_a - p_ft_d_given_ht_a)

    results["AH"] = ht_a * p_ft_h_given_ht_a
    results["AD"] = ht_a * p_ft_d_given_ht_a
    results["AA"] = ht_a * p_ft_a_given_ht_a

    # 联赛先验混合 (如果有历史数据)
    if league_htft:
        blend = 0.3  # 30%先验 + 70%模型
        for key in results:
            prior = league_htft.get(key)
            if prior is not None:
                results[key] = (1 - blend) * results[key] + blend * prior

    # 归一化
    total = sum(results.values())
    if total > 0:
        results = {k: round(v / total, 4) for k, v in results.items()}

    return results


def top_htft(results: dict, n: int = 3) -> list[tuple]:
    """返回概率最高的n个半全场结果"""
    sorted_items = sorted(results.items(), key=lambda x: -x[1])
    return sorted_items[:n]
