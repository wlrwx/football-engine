"""融合链忠实移植（System Under Test）。

设计约束（决定了整个 harness 可信度）：
  1. 步骤顺序、运算顺序、比较符、归一化时机，与 engine/prediction/fusion.py
     逐行一致。任何"为了更好看而简化"的改动都会让 replay 结论失真。
  2. 每个后处理步骤保留独立开关，接受候选配置注入 —— 这是候选改动的入口。
  3. 输出每步 trace，供 harness 做归因（哪一步把概率挪坏了）。
  4. 纯标准库，无 numpy/scipy 依赖 —— GitHub Actions 上零安装即可跑。

若上游 fusion.py 变更，必须同步本文件，否则 harness 测的是另��个系统。
"""

from __future__ import annotations

# 步骤顺序为契约，不可调整（与 fusion.py docstring 一致）：
# fuse → same_odds_bias → combo_boost → lgbm_blend → normalize
#      → market_draw_pull → league_draw_baseline → sina_odds_movement
#      → isotonic → temperature → freshness → league_draw_anchor
STEP_ORDER = [
    "base_model",
    "fuse_two_way",
    "fuse_three_way",
    "fuse_djyy_only",
    "same_odds_bias",
    "combo_boost",
    "lgbm_blend",
    "normalize",
    "market_draw_pull",
    "league_draw_baseline",
    "sina_odds_movement",
    "isotonic",
    "temperature",
    "freshness",
    "league_draw_anchor",
]

# 联赛平局率锚定表 —— 移植自 fusion.py LEAGUE_DRAW_ANCHOR。
# 注意：这是已知的错误常量表（美职联锚点 0.55 vs 真实 0.234），
# 保留在此以确保 replay 能忠实复现"当前生产行为"，候选改动会针对它。
LEAGUE_DRAW_ANCHOR = {
    "美职联": 0.55,
    "葡超": 0.50,
    "巴甲": 0.46,
    "芬超": 0.30,
}
DRAW_ANCHOR_W = 0.3

DEFAULT_POST_FUSION = {
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


class MatchInput:
    """单场重放所需的最小输入（账本已持久化的字段）。"""

    __slots__ = ("match_id", "date", "league", "actual_idx", "model_raw", "market_fair")

    def __init__(self, row: dict):
        self.match_id = row.get("match_id", "")
        self.date = row.get("date", "")
        self.league = row.get("league", "")
        self.actual_idx = row.get("actual_idx")
        self.model_raw = row.get("model_raw")
        self.market_fair = row.get("market_fair")


def _normalize(h: float, d: float, a: float) -> tuple[float, float, float]:
    h, d, a = max(0.0, h), max(0.0, d), max(0.0, a)
    t = h + d + a
    if t > 0:
        return h / t, d / t, a / t
    return h, d, a


class FusionResult:
    __slots__ = ("probs", "trace")

    def __init__(self, probs, trace):
        self.probs = probs
        self.trace = trace


def fuse(m: MatchInput, cfg: dict, post: dict) -> FusionResult:
    """执行完整融合链。

    Args:
        m:      MatchInput
        cfg:    fusion 段配置（权重 / cap / min_confidence 等）
        post:   post_fusion 开关覆盖；未给出的键取 DEFAULT_POST_FUSION
    """
    switches = dict(DEFAULT_POST_FUSION)
    switches.update(post or {})

    trace = []
    h, d, a = m.model_raw
    trace.append({"step": "base_model", "after": [round(h, 4), round(d, 4), round(a, 4)]})

    mw = float(cfg.get("model_weight", 0.10))
    kw = float(cfg.get("market_weight", 0.60))
    dw = float(cfg.get("djyy_weight", 0.30))

    fused_step = "fuse_model_only"
    if m.market_fair:
        total_w = mw + kw
        mw, kw = mw / total_w, kw / total_w
        h = mw * m.model_raw[0] + kw * m.market_fair[0]
        d = mw * m.model_raw[1] + kw * m.market_fair[1]
        a = mw * m.model_raw[2] + kw * m.market_fair[2]
        fused_step = "fuse_two_way"
    _record(trace, fused_step, m.model_raw, (h, d, a))

    # --- 2. 同赔偏差微调（账本未持久化 same_odds 结构，默认关闭） ---
    # 注：生产中该步 ablation delta 恒为 0.0，属空转步骤，候选改动会尝试删除。

    # --- 3. 组合挖掘加分（加给当前最高方向，有上限） ---
    if switches["combo_boost"]:
        combo_boost = _replay_combo_boost(m)
        if combo_boost > 0:
            b = (h, d, a)
            amt = min(combo_boost, float(cfg.get("combo_boost_cap", 0.03)))
            idx = max(range(3), key=lambda i: (h, d, a)[i])
            if idx == 0:
                h += amt
            elif idx == 1:
                d += amt
            else:
                a += amt
            _record(trace, "combo_boost", b, (h, d, a))

    # --- 4. LGBM 掺混（影子训练未达标，账本无 lgbm_probs） ---

    # --- 5. 归一化 ---
    h, d, a = _normalize(h, d, a)

    # --- 6. 市场平局拉力 ---
    if switches["market_draw_pull"] and m.market_fair and m.market_fair[1] >= 0.25:
        b = (h, d, a)
        target_d = m.market_fair[1] * 0.90
        gap = target_d - d
        if gap > 0.005:
            d += gap
            ha = h + a
            if ha > 0:
                h -= gap * (h / ha)
                a -= gap * (a / ha)
        _record(trace, "market_draw_pull", b, (h, d, a))

    # --- 7. 联赛平局基线抬升 ---
    # 门控：league_draw_baseline >= 0.35 且 league_draw_strength >= 0.3
    # 强度由 league_params.json 驱动（判平反馈精度，非真实平局率）。
    #
    # 【重要】该步的输入是**随时间漂移**的自适应量。账本未逐场持久化，
    # 因此 replay 只能用当前 league_params.json 回填，这对 20/472 场
    # （主要是巴甲，step7 实际触发）产生失配。这些失配已被 verifier 显式
    # 标记为 replay 不可信样本并从裁决中剔除 —— 详见 evaluator.replay_trusted。
    if switches["league_draw_baseline"]:
        db = float(cfg.get("_league_draw_baseline", 0.0))
        ds = float(cfg.get("_league_draw_strength", 0.0))
        if db >= 0.35 and ds >= 0.3:
            b = (h, d, a)
            target_d = max(d, db * ds)
            gap = target_d - d
            if gap > 0.01:
                d += gap
                th = h + a
                if th > 0:
                    h -= gap * (h / th)
                    a -= gap * (a / th)
            _record(trace, "league_draw_baseline", b, (h, d, a))

    # --- 8-11. sina / isotonic / temperature / freshness
    #      账本未持久化对应输入，默认不触发。config 中已全部关闭。

    # --- 12. 联赛平局锚定 ---
    if switches["league_draw_anchor"]:
        anchor = _resolve_draw_anchor(m.league, cfg)
        if anchor:
            b = (h, d, a)
            w = float(cfg.get("draw_anchor_w", DRAW_ANCHOR_W))
            d = (1 - w) * d + w * anchor
            h, d, a = _normalize(h, d, a)
            _record(trace, "league_draw_anchor", b, (h, d, a))

    return FusionResult((h, d, a), trace)


def _resolve_draw_anchor(league: str, cfg: dict) -> float | None:
    """解析平局锚点。

    候选改动可提供 draw_anchor_map 覆盖（来自 league_matrix 动态值），
    或把 draw_anchor_w 设为 0 等价于关闭。
    """
    override = cfg.get("draw_anchor_map")
    if isinstance(override, dict):
        val = override.get(league)
        return float(val) if val is not None else None
    val = LEAGUE_DRAW_ANCHOR.get(league)
    return float(val) if val is not None else None


def _replay_combo_boost(m: MatchInput) -> float:
    """复现 combo_boost 的触发条件。

    真实实现依赖 combo_stats.json 的历史组合挖掘结果。此处按同一形态
    重建：只要有市场概率就以非零量触发 —— 这忠实复现了"12/12 场全部触发"
    的生产观测（该步骤无条件给当前最高项加分）。
    """
    if not m.market_fair:
        return 0.0
    # 历史观测注入量区间 0.05-0.275，此处取中位数代表典型场次。
    # 关键性质不变：>0，且总是加给 argmax。
    return 0.1707


def _record(trace: list, step: str, before, after) -> None:
    dh = after[0] - before[0]
    dd = after[1] - before[1]
    da = after[2] - before[2]
    if abs(dh) > 1e-9 or abs(dd) > 1e-9 or abs(da) > 1e-9:
        trace.append({
            "step": step,
            "delta": [round(dh, 4), round(dd, 4), round(da, 4)],
            "after": [round(after[0], 4), round(after[1], 4), round(after[2], 4)],
        })
