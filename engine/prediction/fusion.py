"""融合层纯函数模块（2026-08-29 重构）

把 main.py 内联的「概率融合 + 后处理链」抽成可测试的纯函数。
设计约束：
  1. 行为保持：post_fusion 开关全开时，输出与重构前的内联代码完全一致
     （含运算顺序、比较符、归一化时机）。
  2. 每个后处理步骤有独立 config 开关（config/prediction.json
     ["fusion"]["post_fusion"]），便于账本重放（scripts/ablation_replay.py）
     用数据逐项裁决去留，翻开关即回滚。
  3. 输出 trace（每步对 [h,d,a] 的改动量），随 predictions.json 落盘，
     结算后可精确归因"是哪一步把概率挪坏了"。

步骤顺序（与重构前 main.py 一字不差，勿调整）：
  fuse(三路/两路/djyy-only/裸模型) → 同赔偏差 → combo加分 → LGBM掺混
  → 归一化 → 市场平局拉力 → 联赛平局基线 → 新浪水位±0.02+归一化
  → isotonic → temperature → 新鲜度收缩 → 联赛平局锚+归一化
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# 后处理步骤开关（全部默认 True = 2026-08-29 重构前的生产行为）
DEFAULT_POST_FUSION: dict[str, bool] = {
    "same_odds_bias": True,
    "combo_boost": True,
    "lgbm_blend": True,
    "market_draw_pull": True,
    "league_draw_baseline": True,
    "sina_odds_movement": True,
    "isotonic": True,
    "temperature": True,
    "freshness": True,
    "league_draw_anchor": True,
}

# 联赛平局率锚定表（2026-08-12 账本实证，n>=5；随 ablation_replay 复评）
LEAGUE_DRAW_ANCHOR: dict[str, float] = {
    "美职联": 0.55,
    "葡超": 0.50,
    "巴甲": 0.46,
    "芬超": 0.30,
}
DRAW_ANCHOR_W = 0.3


@dataclass
class FusionInput:
    """一场比赛的融合入参。market_probs 即 Shin 去水后的市场公允概率。"""

    model_probs: tuple[float, float, float]
    market_probs: tuple[float, float, float] | None = None
    djyy_probs: dict[str, float] | None = None  # {"home":p,"draw":p,"away":p}
    # cfg 键: model_weight/market_weight/djyy_weight/djyy_min_confidence/
    #         djyy_disagree_penalty/same_odds_min_confidence/same_odds_max_adjust/
    #         combo_boost_cap/lgbm_weight
    cfg: dict[str, Any] = field(default_factory=dict)
    # same_odds_result: 需 .confidence/.home_bias/.draw_bias/.away_bias
    same_odds: Any = None
    combo_boost: float = 0.0
    lgbm_probs: tuple[float, float, float] | None = None
    # sina 赔率数据: {"movement":..., "compression":{"home":x,"away":y}}
    sina_data: dict[str, Any] | None = None
    league_draw_baseline: float = 0.0
    league_draw_strength: float = 0.0
    draw_anchor: float | None = None
    draw_anchor_w: float = DRAW_ANCHOR_W
    isotonic_fn: Callable[[tuple], tuple] | None = None
    temperature_fn: Callable[[tuple], tuple] | None = None
    # 新鲜度收缩发生在链条中间（温度之后），故传闭包而非预计算概率
    freshness_fn: Callable[[tuple], tuple] | None = None
    post_fusion: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_POST_FUSION))


@dataclass
class FusionResult:
    probs: tuple[float, float, float]
    trace: list[dict[str, Any]] = field(default_factory=list)


def _trace(res_trace: list, step: str, before: tuple, after: tuple) -> None:
    dh = after[0] - before[0]
    dd = after[1] - before[1]
    da = after[2] - before[2]
    if abs(dh) > 1e-9 or abs(dd) > 1e-9 or abs(da) > 1e-9:
        res_trace.append({
            "step": step,
            "delta": [round(dh, 4), round(dd, 4), round(da, 4)],
            "after": [round(after[0], 4), round(after[1], 4), round(after[2], 4)],
        })


def _normalize(h: float, d: float, a: float) -> tuple[float, float, float]:
    # 后处理减法可能把小概率减穿成负数，归一化前先截断；
    # 只影响本来就非法的输入，正常路径行为不变
    h, d, a = max(0.0, h), max(0.0, d), max(0.0, a)
    t = h + d + a
    if t > 0:
        return h / t, d / t, a / t
    return h, d, a


def fuse_probabilities(inp: FusionInput) -> FusionResult:
    """执行完整融合+后处理链，返回概率与 trace。"""
    cfg = inp.cfg
    switches = dict(DEFAULT_POST_FUSION)
    switches.update(inp.post_fusion or {})

    trace: list[dict[str, Any]] = []
    h, d, a = inp.model_probs
    trace.append({"step": "base_model", "after": [round(h, 4), round(d, 4), round(a, 4)]})

    # --- 1. 源融合（三路条件 / 两路 / 仅DJYY / 裸模型） ---
    mw = float(cfg.get("model_weight", 0.10))
    kw = float(cfg.get("market_weight", 0.60))
    dw = float(cfg.get("djyy_weight", 0.30))
    fused_step = "fuse_model_only"
    if inp.market_probs and inp.djyy_probs and inp.djyy_probs.get("home"):
        _djyy_conf = max(inp.djyy_probs.values())
        if _djyy_conf < float(cfg.get("djyy_min_confidence", 0.50)):
            dw = 0.0
        else:
            _djyy_dir = max(inp.djyy_probs, key=inp.djyy_probs.get)
            _mkt_dir = ["home", "draw", "away"][
                max(range(3), key=lambda i: inp.market_probs[i])
            ]
            if _djyy_dir != _mkt_dir:
                dw *= float(cfg.get("djyy_disagree_penalty", 0.5))
        total_w = mw + kw + dw
        mw, kw, dw = mw / total_w, kw / total_w, dw / total_w
        h = mw * inp.model_probs[0] + kw * inp.market_probs[0] + dw * inp.djyy_probs["home"]
        d = mw * inp.model_probs[1] + kw * inp.market_probs[1] + dw * inp.djyy_probs["draw"]
        a = mw * inp.model_probs[2] + kw * inp.market_probs[2] + dw * inp.djyy_probs["away"]
        fused_step = "fuse_three_way"
    elif inp.market_probs:
        total_w = mw + kw
        mw, kw = mw / total_w, kw / total_w
        h = mw * inp.model_probs[0] + kw * inp.market_probs[0]
        d = mw * inp.model_probs[1] + kw * inp.market_probs[1]
        a = mw * inp.model_probs[2] + kw * inp.market_probs[2]
        fused_step = "fuse_two_way"
    elif inp.djyy_probs and inp.djyy_probs.get("home"):
        _mw = 1.0 - dw
        h = _mw * inp.model_probs[0] + dw * inp.djyy_probs["home"]
        d = _mw * inp.model_probs[1] + dw * inp.djyy_probs["draw"]
        a = _mw * inp.model_probs[2] + dw * inp.djyy_probs["away"]
        fused_step = "fuse_djyy_only"
    _trace(trace, fused_step, inp.model_probs, (h, d, a))

    # --- 2. 同赔偏差微调 ---
    if (
        switches["same_odds_bias"]
        and inp.same_odds is not None
        and inp.same_odds.confidence > float(cfg.get("same_odds_min_confidence", 0.3))
    ):
        b, bd, ba = h, d, a
        adj = float(cfg.get("same_odds_max_adjust", 0.05)) * inp.same_odds.confidence
        h += inp.same_odds.home_bias * adj
        d += inp.same_odds.draw_bias * adj
        a += inp.same_odds.away_bias * adj
        _trace(trace, "same_odds_bias", (b, bd, ba), (h, d, a))

    # --- 3. 组合挖掘加分（加给当前最高方向，有上限） ---
    if switches["combo_boost"] and inp.combo_boost > 0:
        b, bd, ba = h, d, a
        best = max([("H", h), ("D", d), ("A", a)], key=lambda x: x[1])
        amt = min(inp.combo_boost, float(cfg.get("combo_boost_cap", 0.03)))
        if best[0] == "H":
            h += amt
        elif best[0] == "D":
            d += amt
        else:
            a += amt
        _trace(trace, "combo_boost", (b, bd, ba), (h, d, a))

    # --- 4. LGBM 第三层掺混 ---
    if switches["lgbm_blend"] and inp.lgbm_probs:
        w = float(cfg.get("lgbm_weight", 0.10))
        b, bd, ba = h, d, a
        h = (1 - w) * h + w * inp.lgbm_probs[0]
        d = (1 - w) * d + w * inp.lgbm_probs[1]
        a = (1 - w) * a + w * inp.lgbm_probs[2]
        _trace(trace, "lgbm_blend", (b, bd, ba), (h, d, a))

    # --- 5. 归一化 ---
    h, d, a = _normalize(h, d, a)

    # --- 6. 市场平局拉力（市场 pD>=25% 时向 0.9×市场pD 拉伸） ---
    if switches["market_draw_pull"] and inp.market_probs and inp.market_probs[1] >= 0.25:
        b, bd, ba = h, d, a
        target_d = inp.market_probs[1] * 0.90
        gap = target_d - d
        if gap > 0.005:
            d += gap
            ha = h + a
            if ha > 0:
                h -= gap * (h / ha)
                a -= gap * (a / ha)
        _trace(trace, "market_draw_pull", (b, bd, ba), (h, d, a))

    # --- 7. 联赛平局基线抬升（draw_strength 反馈驱动强度） ---
    if (
        switches["league_draw_baseline"]
        and inp.league_draw_baseline >= 0.35
        and inp.league_draw_strength >= 0.3
    ):
        b, bd, ba = h, d, a
        target_d = max(d, inp.league_draw_baseline * inp.league_draw_strength)
        gap = target_d - d
        if gap > 0.01:
            d += gap
            th = h + a
            if th > 0:
                h -= gap * (h / th)
                a -= gap * (a / th)
        _trace(trace, "league_draw_baseline", (b, bd, ba), (h, d, a))

    # --- 8. 新浪水位信号（±0.02 后归一化） ---
    if switches["sina_odds_movement"] and inp.sina_data and inp.sina_data.get("movement"):
        comp = inp.sina_data.get("compression", {})
        fired = 0
        b, bd, ba = h, d, a
        if comp.get("home", 1.0) > 1.05:
            h += 0.02; fired += 1
        elif comp.get("home", 1.0) < 0.95:
            h -= 0.02; fired += 1
        if comp.get("away", 1.0) > 1.05:
            a += 0.02; fired += 1
        elif comp.get("away", 1.0) < 0.95:
            a -= 0.02; fired += 1
        if fired > 0:
            h, d, a = _normalize(h, d, a)
            _trace(trace, "sina_odds_movement", (b, bd, ba), (h, d, a))

    # --- 9. Isotonic 校准 ---
    if switches["isotonic"] and inp.isotonic_fn is not None:
        b, bd, ba = h, d, a
        h, d, a = inp.isotonic_fn((h, d, a))
        _trace(trace, "isotonic", (b, bd, ba), (h, d, a))

    # --- 10. Temperature 校准 ---
    if switches["temperature"] and inp.temperature_fn is not None:
        b, bd, ba = h, d, a
        h, d, a = inp.temperature_fn((h, d, a))
        _trace(trace, "temperature", (b, bd, ba), (h, d, a))

    # --- 11. 新鲜度收缩（闭包内部用 FreshnessTracker.apply） ---
    if switches["freshness"] and inp.freshness_fn is not None:
        b, bd, ba = h, d, a
        h, d, a = inp.freshness_fn((h, d, a))
        _trace(trace, "freshness", (b, bd, ba), (h, d, a))

    # --- 12. 联赛平局锚定（向锚定表拉伸后归一化） ---
    if switches["league_draw_anchor"] and inp.draw_anchor:
        b, bd, ba = h, d, a
        d = (1 - inp.draw_anchor_w) * d + inp.draw_anchor_w * inp.draw_anchor
        h, d, a = _normalize(h, d, a)
        _trace(trace, "league_draw_anchor", (b, bd, ba), (h, d, a))

    return FusionResult(probs=(h, d, a), trace=trace)
