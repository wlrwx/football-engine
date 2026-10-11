"""预测信号派生：平局预警分类 + 市场分歧检测（2026-10-11 从 main.py 抽出）

和 `score_candidates.py` 同批拆分：这两段原先埋在 `run_daily_pipeline`
的 for 循环体内，是纯函数（只依赖入参），却无法单独测试。

**行为要求逐位不变** —— 两个字段都落进 predictions.json 并进账本统计，
改了会让历史分层结果不可比。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

SIDES = ("home", "draw", "away")


def classify_draw_alert(
    league_canon: str | None,
    market_probs: Sequence[float] | None,
    high_draw_leagues: Iterable[str],
) -> str | None:
    """平局预警分类。返回 "league_draw" / "cold_draw" / "balanced_draw" / None。

    - league_draw(R1, 2026-08-12)：高平联赛 + 市场平局 P∈[0.20,0.30) 无脑改判。
      回测 +2.9pp，切半稳健。
    - cold_draw：市场强烈看好一方(>50%)，但平局概率 ≥25%。
    - balanced_draw：双方接近(差距<15%)，平局概率 ≥26%。

    判定顺序即优先级，不可调换。`market_probs` 缺失（裸模型场次）返回 None。
    """
    if not market_probs:
        return None
    market_h, market_d, market_a = market_probs
    max_market = max(market_h, market_d, market_a)
    if league_canon in high_draw_leagues and 0.20 <= market_d < 0.30:
        return "league_draw"
    if max_market > 0.50 and market_d >= 0.25:
        return "cold_draw"
    if abs(market_h - market_a) < 0.15 and market_d >= 0.26:
        return "balanced_draw"
    return None


def market_direction(pred: dict[str, Any]) -> str:
    """取预测方向：优先 pred["direction"]，缺失则用三路概率 argmax。"""
    d = pred.get("direction")
    if d:
        return d
    probs = (
        pred.get("home_win_prob") or 0,
        pred.get("draw_prob") or 0,
        pred.get("away_win_prob") or 0,
    )
    return SIDES[probs.index(max(probs))]


def detect_market_disagreement(
    pred: dict[str, Any], ev_threshold: float = 0.05
) -> dict[str, dict[str, Any]]:
    """市场分歧：非模型方向但赔率隐含显著正 EV 的场次。

    8/5 实证：周三002/003 客胜 EV +38%/+115% 被方向纪律拦下且无提示；
    低置信度场次(margin<0.08)尤其容易发生——模型概率接近时市场赔率信息量大。
    存 predictions 供复盘验证"市场分歧方向是否值得破纪律"（数据说话，不预设）。

    返回 {side: {prob, odds, ev}}；无分歧返回 {}。
    """
    direction = market_direction(pred)
    out: dict[str, dict[str, Any]] = {}
    for side, prob_key, odds_key in (
        ("home", "home_win_prob", "home_odds"),
        ("draw", "draw_prob", "draw_odds"),
        ("away", "away_win_prob", "away_odds"),
    ):
        if side == direction:
            continue
        prob = pred.get(prob_key, 0)
        odds = pred.get(odds_key)
        if odds and prob * odds - 1 > ev_threshold:
            out[side] = {
                "prob": round(prob, 3),
                "odds": odds,
                "ev": round(prob * odds - 1, 3),
            }
    return out

