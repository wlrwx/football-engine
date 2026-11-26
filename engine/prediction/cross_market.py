from __future__ import annotations
"""跨市场参考信号提取与亚盘/大小球隐含期望进球反推（仅限参考市场 Pinnacle，严禁触碰目标竞彩价）。"""

import math
from typing import Any

import numpy as np
from scipy.optimize import minimize

CROSS_MARKET_FEATURE_NAMES = [
    "pin_implied_home",     # Pinnacle 去水主胜概率
    "pin_implied_draw",     # Pinnacle 去水平局概率
    "pin_implied_away",     # Pinnacle 去水客胜概率
    "pin_total_xg",         # 亚盘+大小球反推总期望进球
    "pin_xg_diff",          # 亚盘+大小球反推主客期望进球差 (home - away)
    "pin_move_home",        # Pinnacle 1X2 主胜隐含概率开盘→即时位移
    "pin_move_draw",        # Pinnacle 1X2 平局隐含概率开盘→即时位移
    "pin_move_away",        # Pinnacle 1X2 客胜隐含概率开盘→即时位移
    "pin_totals_shift",     # 大小球 Over 隐含概率开盘→即时位移
    "pin_ah_shift",         # 亚盘主队隐含概率开盘→即时位移
    "lineup_strength_diff", # 首发攻击手差 / 伤停攻击手逆差
    "cross_market_missing", # 1.0=无参考盘口信号，0.0=至少有参考市场 1X2 或盘口数据
]


def devig_two_way(odds_a: float, odds_b: float) -> tuple[float, float] | None:
    """两项盘口比例去水，返回 (p_a, p_b) 或 None。"""
    try:
        oa = float(odds_a)
        ob = float(odds_b)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(oa) and math.isfinite(ob)) or oa <= 1.0 or ob <= 1.0:
        return None
    ia = 1.0 / oa
    ib = 1.0 / ob
    tot = ia + ib
    if tot <= 0.0:
        return None
    return (ia / tot, ib / tot)


def devig_three_way(odds_h: float, odds_d: float, odds_a: float) -> tuple[float, float, float] | None:
    """三项参考盘口去水，返回 (p_h, p_d, p_a) 或 None。"""
    try:
        oh, od, oa = float(odds_h), float(odds_d), float(odds_a)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(oh) and math.isfinite(od) and math.isfinite(oa)):
        return None
    if min(oh, od, oa) <= 1.0:
        return None
    ih, id_, ia = 1.0 / oh, 1.0 / od, 1.0 / oa
    tot = ih + id_ + ia
    if tot <= 0.0:
        return None
    return (ih / tot, id_ / tot, ia / tot)


def _dc_score_matrix(lh: float, la: float, rho: float = -0.0895, max_goals: int = 10) -> np.ndarray:
    """构造归一化 Dixon-Coles 比分联合概率矩阵 (max_goals+1, max_goals+1)。"""
    n = max_goals + 1
    k = np.arange(n, dtype=float)
    fact = np.array([math.factorial(int(i)) for i in k], dtype=float)
    ph = np.exp(-lh) * (lh ** k) / fact
    pa = np.exp(-la) * (la ** k) / fact
    mat = np.outer(ph, pa)
    if rho != 0.0:
        mat[0, 0] *= max(0.0, 1.0 - lh * la * rho)
        mat[0, 1] *= max(0.0, 1.0 + lh * rho)
        mat[1, 0] *= max(0.0, 1.0 + la * rho)
        mat[1, 1] *= max(0.0, 1.0 - rho)
    tot = float(mat.sum())
    if tot > 0.0:
        mat /= tot
    return mat


def _split_quarter_line(line: float) -> list[float]:
    """将亚盘/大小球盘口（含 0.25/0.75 四分之一盘）拆分为半/整盘口列表。"""
    rem = abs(line * 4.0 - round(line * 4.0))
    if rem < 1e-6:
        quarter_check = abs(line * 2.0 - round(line * 2.0))
        if quarter_check > 0.1:
            return [line - 0.25, line + 0.25]
    return [line]


def _asian_cover_prob(mat: np.ndarray, ah_line: float) -> float:
    """计算主队在让球线 ah_line 下扣除走水后的公平穿盘概率 win / (win + loss)。"""
    n = mat.shape[0]
    hg = np.arange(n)[:, None]
    ag = np.arange(n)[None, :]
    diff = hg - ag
    lines = _split_quarter_line(ah_line)
    win_h = 0.0
    win_a = 0.0
    for ln in lines:
        margin = diff + ln
        win_h += float(mat[margin > 1e-9].sum())
        win_a += float(mat[margin < -1e-9].sum())
    win_h /= len(lines)
    win_a /= len(lines)
    denom = win_h + win_a
    if denom <= 1e-9:
        return 0.5
    return win_h / denom


def _totals_over_prob(mat: np.ndarray, total_line: float) -> float:
    """计算大小球在总球线 total_line 下扣除走水后的公平 Over 概率。"""
    n = mat.shape[0]
    hg = np.arange(n)[:, None]
    ag = np.arange(n)[None, :]
    tot_goals = hg + ag
    lines = _split_quarter_line(total_line)
    over_p = 0.0
    under_p = 0.0
    for ln in lines:
        margin = tot_goals - ln
        over_p += float(mat[margin > 1e-9].sum())
        under_p += float(mat[margin < -1e-9].sum())
    over_p /= len(lines)
    under_p /= len(lines)
    denom = over_p + under_p
    if denom <= 1e-9:
        return 0.5
    return over_p / denom


def implied_xg_from_ah_totals(
    ah_line: float,
    ah_home_odds: float,
    ah_away_odds: float,
    total_line: float,
    over_odds: float,
    under_odds: float,
    rho: float = -0.0895,
) -> dict[str, float] | None:
    """从 Pinnacle 亚盘与大小球去水赔率联合反推 (home_xg, away_xg) 及 1X2 隐含分布。"""
    ah_fair = devig_two_way(ah_home_odds, ah_away_odds)
    tot_fair = devig_two_way(over_odds, under_odds)
    if ah_fair is None or tot_fair is None:
        return None
    target_ah = ah_fair[0]
    target_over = tot_fair[0]

    def _obj(x: np.ndarray) -> float:
        lh = math.exp(float(x[0]))
        la = math.exp(float(x[1]))
        mat = _dc_score_matrix(lh, la, rho=rho, max_goals=10)
        p_ah = _asian_cover_prob(mat, ah_line)
        p_ov = _totals_over_prob(mat, total_line)
        return (p_ah - target_ah) ** 2 + (p_ov - target_over) ** 2 + 1e-5 * float(x[0] ** 2 + x[1] ** 2)

    x0 = np.array([math.log(max(0.2, total_line * 0.52)), math.log(max(0.2, total_line * 0.48))])
    bounds = [(math.log(0.15), math.log(4.5)), (math.log(0.15), math.log(4.5))]
    res = minimize(_obj, x0, method="L-BFGS-B", bounds=bounds)
    opt = res.x if res.x is not None else x0
    lh = math.exp(float(opt[0]))
    la = math.exp(float(opt[1]))
    mat = _dc_score_matrix(lh, la, rho=rho, max_goals=10)
    p_home = float(np.tril(mat, -1).sum())
    p_draw = float(np.trace(mat))
    p_away = float(np.triu(mat, 1).sum())
    tot = p_home + p_draw + p_away
    if tot > 0:
        p_home /= tot
        p_draw /= tot
        p_away /= tot
    return {
        "home_xg": round(lh, 4),
        "away_xg": round(la, 4),
        "total_xg": round(lh + la, 4),
        "xg_diff": round(lh - la, 4),
        "p_home": round(p_home, 6),
        "p_draw": round(p_draw, 6),
        "p_away": round(p_away, 6),
    }


def _safe_float(val: Any) -> float | None:
    try:
        if val is None or isinstance(val, bool):
            return None
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _parse_pinnacle_1x2(pinnacle_odds: Any) -> tuple[float, float, float] | None:
    if isinstance(pinnacle_odds, (list, tuple)) and len(pinnacle_odds) >= 3:
        return devig_three_way(pinnacle_odds[0], pinnacle_odds[1], pinnacle_odds[2])
    if isinstance(pinnacle_odds, dict):
        h = pinnacle_odds.get("home") or pinnacle_odds.get("1")
        d = pinnacle_odds.get("draw") or pinnacle_odds.get("X")
        a = pinnacle_odds.get("away") or pinnacle_odds.get("2")
        return devig_three_way(h, d, a)
    return None


def extract_cross_market_features(
    opening_movement: dict | None = None,
    pinnacle_odds: tuple[float, float, float] | dict | None = None,
    lineup_info: dict | None = None,
    rho: float = -0.0895,
) -> dict[str, float]:
    """从参考市场（Pinnacle 1X2/亚盘/大小球/开盘位移）和首发信息提取纯 float 特征字典。"""
    out: dict[str, float] = {k: 0.0 for k in CROSS_MARKET_FEATURE_NAMES}
    out["cross_market_missing"] = 1.0

    om = opening_movement if isinstance(opening_movement, dict) else {}

    # 1. 参考市场 1X2 隐含概率与开盘→即时位移
    ft = om.get("1x2_fulltime") if isinstance(om.get("1x2_fulltime"), dict) else {}
    cur_1x2 = ft.get("current") if isinstance(ft.get("current"), dict) else {}
    pin_fair = _parse_pinnacle_1x2(cur_1x2) if cur_1x2 else None
    if pin_fair is None and pinnacle_odds is not None:
        pin_fair = _parse_pinnacle_1x2(pinnacle_odds)

    if pin_fair is not None:
        out["pin_implied_home"] = round(pin_fair[0], 6)
        out["pin_implied_draw"] = round(pin_fair[1], 6)
        out["pin_implied_away"] = round(pin_fair[2], 6)
        out["cross_market_missing"] = 0.0

    shift_1x2 = ft.get("prob_shift") if isinstance(ft.get("prob_shift"), dict) else {}
    if shift_1x2:
        out["pin_move_home"] = round(_safe_float(shift_1x2.get("home") or shift_1x2.get("1")) or 0.0, 6)
        out["pin_move_draw"] = round(_safe_float(shift_1x2.get("draw") or shift_1x2.get("X")) or 0.0, 6)
        out["pin_move_away"] = round(_safe_float(shift_1x2.get("away") or shift_1x2.get("2")) or 0.0, 6)

    # 2. 大小球与亚盘位移 + 联合反推 (total_xg, xg_diff)
    tot_mkt = om.get("totals_main") if isinstance(om.get("totals_main"), dict) else {}
    hdp_mkt = om.get("handicap") if isinstance(om.get("handicap"), dict) else {}

    tot_shift = tot_mkt.get("prob_shift") if isinstance(tot_mkt.get("prob_shift"), dict) else {}
    if tot_shift:
        out["pin_totals_shift"] = round(_safe_float(tot_shift.get("over")) or 0.0, 6)
    hdp_shift = hdp_mkt.get("prob_shift") if isinstance(hdp_mkt.get("prob_shift"), dict) else {}
    if hdp_shift:
        out["pin_ah_shift"] = round(_safe_float(hdp_shift.get("home") or hdp_shift.get("1")) or 0.0, 6)

    tot_cur = tot_mkt.get("current") if isinstance(tot_mkt.get("current"), dict) else {}
    hdp_cur = hdp_mkt.get("current") if isinstance(hdp_mkt.get("current"), dict) else {}
    ah_line = _safe_float(hdp_mkt.get("line") if "line" in hdp_mkt else hdp_cur.get("line"))
    tot_line = _safe_float(tot_mkt.get("line") if "line" in tot_mkt else tot_cur.get("line"))
    ah_h = _safe_float(hdp_cur.get("home") or hdp_cur.get("1"))
    ah_a = _safe_float(hdp_cur.get("away") or hdp_cur.get("2"))
    ov_o = _safe_float(tot_cur.get("over"))
    un_o = _safe_float(tot_cur.get("under"))

    if None not in (ah_line, ah_h, ah_a, tot_line, ov_o, un_o):
        inv = implied_xg_from_ah_totals(
            ah_line=ah_line, ah_home_odds=ah_h, ah_away_odds=ah_a,
            total_line=tot_line, over_odds=ov_o, under_odds=un_o, rho=rho,
        )
        if inv is not None:
            out["pin_total_xg"] = inv["total_xg"]
            out["pin_xg_diff"] = inv["xg_diff"]
            out["cross_market_missing"] = 0.0
            if pin_fair is None:
                out["pin_implied_home"] = inv["p_home"]
                out["pin_implied_draw"] = inv["p_draw"]
                out["pin_implied_away"] = inv["p_away"]

    # 3. 首发与伤停攻击手净差
    if isinstance(lineup_info, dict) and lineup_info:
        hs = _safe_float(lineup_info.get("home_starting_attackers"))
        as_ = _safe_float(lineup_info.get("away_starting_attackers"))
        if hs is not None and as_ is not None:
            out["lineup_strength_diff"] = round(hs - as_, 4)
        else:
            inj = lineup_info.get("injuries") if isinstance(lineup_info.get("injuries"), dict) else lineup_info
            hi = _safe_float(inj.get("home_attackers"))
            ai = _safe_float(inj.get("away_attackers"))
            if hi is not None and ai is not None:
                out["lineup_strength_diff"] = round(ai - hi, 4)

    return out
