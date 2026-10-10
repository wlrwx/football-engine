"""候选改动生成器 —— 系统能提出什么改动。

设计原则：
  1. 候选必须是**结构化的配置/代码改动**，不是任意代码 —— 否则无人能审计。
  2. 候选必须**先验可辩护**：每个候选都附带一句"为什么认为它可能有用"，
     即使最终被数据否决，这份理由会留在 PR 里供主线 agent 复核。
  3. 候选空间必须**包含"关掉某个东西"** —— 最强的改进往往是删除。
  4. 生成器不做判断。判断只发生在 harness 裁决阶段。
"""

from __future__ import annotations

import copy
import itertools
import random
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Candidate:
    """一个候选改动。"""

    cid: str
    family: str
    title: str
    rationale: str          # 先验理由（写进 PR，主线 agent 要看）
    patch: dict[str, Any] = field(default_factory=dict)
    prior: str = "neutral"  # neutral / likely_good / likely_bad
    reversible: bool = True
    touches_code: bool = False
    files: list[str] = field(default_factory=list)
    # ---- known_issues 注册表带出的字段 ----
    evidence: dict = field(default_factory=dict)      # 支撑结论的实测数字
    detect_by: str = "replay"   # replay | backtest | invariant | audit
    auto_mergeable: bool = False  # 事实性错误：不等统计显著性
    verify: str = ""               # 自动验证方式
    side_effects: list = field(default_factory=list)  # 连带影响，主线需一并看
    invariant_id: str = ""          # known_issues 对应的不变式 id（若与 cid 不同）
    prior_rationale: str = ""       # 兼容字段

    @property
    def is_issue(self) -> bool:
        # issue/（静态审计）和 diagnostic/（动态扫描）都是「已知问题类」，
        # 走 judge_issue 专用通道（backtest/invariant/audit），不进统计裁决。
        # 之前只认 issue/，导致动态诊断发现的 fact 类错误被误送进 Brier 配对
        # 检验（Δ=0 全被 REJECT），把「巴甲评局概率钉死」这种 blocking 漏掉。
        return self.family.startswith("issue/") or self.family.startswith("diagnostic/")

    def describe(self) -> str:
        return f"[{self.family}] {self.title}"


# ---------------------------------------------------------------- 单步开关候选

def toggle_candidates(base_post: dict) -> list[Candidate]:
    """逐个关闭后处理开关。

    这是最高杠杆的候选族：项目自己的 ablation 显示多个步骤 t 值不显著，
    而"无条件给 argmax 加分"这类步骤在数学上不含新信息。
    """
    out = []
    for step in sorted(base_post.keys()):
        if not base_post.get(step):
            continue  # 已关闭的开关无需再生成"关闭"候选
        out.append(Candidate(
            cid=f"toggle_off_{step}",
            family="ablation_toggle",
            title=f"关闭后处理步骤 `{step}`",
            rationale=(
                f"`{step}` 当前为开启。项目自带 ablation_report 显示该步在 full 样本"
                f"的 delta_brier 接近 0 或方向存疑；若该步不携带增量信息，"
                f"关闭应使输出更接近市场公允概率（账本实证市场是更强的预测器）。"
            ),
            patch={"post": {step: False}},
            prior="likely_good" if step in _LIKELY_ENTROPY else "neutral",
            files=["config/prediction.json"],
        ))
    return out


# 这几步在数学上"无条件作用于 argmax"，是熵减器而非信号
_ENTROPY_ONLY = {"combo_boost", "league_draw_anchor", "league_draw_baseline",
                 "market_draw_pull", "same_odds_bias"}
_LIKELY_ENTROPY = _ENTROPY_ONLY


# ---------------------------------------------------------------- 权重候选

def weight_candidates(base_cfg: dict) -> list[Candidate]:
    """扫描融合权重网格。

    账本与独立回测都指向 model 权重应显著低于当前值。这里给出的是
    粗网格 —— 精细调参应在 verifier 阶段由 walk-forward 决定。
    """
    out = []
    mw0 = float(base_cfg.get("model_weight", 0.10))
    for mw in [0.0, 0.05, 0.10, 0.15, 0.20, 0.30]:
        for kw in [0.60, 0.75, 0.90, 1.0]:
            if kw < mw:
                continue
            key = f"w_m{mw:.2f}_k{kw:.2f}"
            out.append(Candidate(
                cid=key,
                family="fusion_weight",
                title=f"融合权重 model={mw:.2f} market={kw:.2f}",
                rationale=(
                    f"当前 model_weight={mw0:.2f}。独立 walk-forward 回测（7904 场）显示"
                    f"模型流对市场的最优贡献权重仅 0.10~0.20，总增益 0.0012 Brier；"
                    f"本候选用于验证 {mw:.2f}/{kw:.2f} 组合在本地账本上是否更优。"
                ),
                patch={"cfg": {"model_weight": mw, "market_weight": kw}},
                prior="neutral",
                files=["config/prediction.json"],
            ))
    return out


# ---------------------------------------------------------------- 组合候选

def combo_candidates(base_post: dict, base_cfg: dict) -> list[Candidate]:
    """P0 组合：一次性关掉全部"熵减器"步骤。

    单步收益可能都不显著（t≈1），但它们叠加时误差可能累积。
    单步检验功效不足，因此需要组合层面的候选。
    """
    out = []
    off = {k: False for k in sorted(_ENTROPY_ONLY)}
    out.append(Candidate(
        cid="combo_disable_entropy_steps",
        family="combo",
        title="关闭全部 5 个无增量信息的后处理步骤",
        rationale=(
            "same_odds_bias / combo_boost / league_draw_baseline / market_draw_pull / "
            "league_draw_anchor 五步或 delta 恒为 0（账本未持久化输入，实际空转），"
            "或无条件作用于 argmax（熵减而非信号）。单独关可能不显著，"
            "但需检验叠加效应是否可测量。"
        ),
        patch={"post": dict(off)},
        prior="likely_good",
        files=["config/prediction.json"],
    ))

    # 锚定权重扫描
    for w in [0.0, 0.05, 0.10, 0.20]:
        out.append(Candidate(
            cid=f"combo_anchor_w_{w:.2f}",
            family="combo",
            title=f"联赛平局锚定权重降至 {w:.2f}",
            rationale=(
                "锚定步以 w=0.3 把平局概率向硬编码锚点拉伸。已核实该锚点表存在事实错误"
                "（美职联锚点 0.55 vs 该联赛真实平局率 0.234；巴甲 draw_baseline 0.6 实为"
                "\"被判平场次占比\"而非真实平局率 0.316）。降低 w 可限制该误差的传导。"
            ),
            patch={"cfg": {"draw_anchor_w": w}},
            prior="likely_good",
            files=["config/prediction.json"],
        ))
    return out


# ---------------------------------------------------------------- 代码级候选

def code_candidates() -> list[Candidate]:
    """需要改代码的候选（harness 只验证效果，代码由 PR 承载）。

    这些通过 cfg 里的开关位注入到 fusion_core 的扩展点。
    """
    return [
        Candidate(
            cid="fix_dc_attack_defense_log",
            family="code_fix",
            title="修复 Dixon-Coles 的 attack/defense 量纲与符号错误",
            rationale=(
                "team_ratings.json 中 attack/defense 是以 1.0 为中心的比例因子（中位数 0.84）。"
                "dixon_coles.py 将其直接相加，而 monte_carlo.py 正确使用 math.log。"
                "后果有二：(1) 量纲错误（加 +0.84 而非 log(0.84)≈−0.17）；"
                "(2) 符号错误 —— 把「防守好」（值<1）当作「防守差」，方向相反。"
                "实测 DC 总进球均值 3.22 vs MC 2.42，真实约 2.8；"
                "这也解释了 MC 中为何需要挂 xg_calibration=0.75 的补丁。"
            ),
            patch={"code": "fix_dc_log"},
            prior="likely_good",
            touches_code=True,
            files=["engine/prediction/dixon_coles.py"],
        ),
        Candidate(
            cid="fix_synthetic_odds_market_anchor",
            family="code_fix",
            title="合成赔率场次补回市场锚",
            rationale=(
                "近 12 天 116 场中有 8 场 odds_synthetic=true 且 market_fair=None，"
                "其融合 trace 仅为 base_model→combo_boost，完全不含市场信息，"
                "概率 100% 来自高估的裸 DC。应从 home/draw/away_odds 反推公允概率。"
            ),
            patch={"code": "fix_synthetic_odds"},
            prior="likely_good",
            touches_code=True,
            files=["engine/main.py", "engine/sources/manager.py"],
        ),
        Candidate(
            cid="gate_draw_baseline_on_sample_size",
            family="code_fix",
            title="联赛平局基线加样本量门槛",
            rationale=(
                "league_params.json 中葡超 draw_baseline=0.25/0 hits、芬超 0.14/0 hits，"
                "而门控条件仅要求 draw_baseline>=0.35 且 draw_strength>=0.3，"
                "在小样本（3~4 场）上形成的估计会直接驱动概率重分配。"
                "应要求 draw_predictions >= 30 方可触发。"
            ),
            patch={"code": "gate_draw_baseline"},
            prior="likely_good",
            touches_code=True,
            files=["engine/prediction/fusion.py", "engine/learning/league_params.py"],
        ),
    ]


# ---------------------------------------------------------------- 采样

def issue_candidates(base_cfg: dict, base_post: dict, rejected_ids: set[str] | None = None,
                    include_non_auto: bool = True) -> list[Candidate]:
    """把 known_issues 注册表转成候选。

    这是闭环能发现**语义缺陷**的唯一入口 —— 配置网格永远试不出
    「DC 把比例因子当加法项」这种问题。

    rejected_ids: 已被主线否决过的 issue id（本轮不再提）。
    include_non_auto: 是否包含 auto_mergeable=False 的条目。
        False 时只提事实性错误 —— 那些不该等统计显著性的修复。
    """
    from . import known_issues as KI
    rejected_ids = rejected_ids or set()
    out = []
    for issue in KI.KNOWN_ISSUES:
        if issue["id"] in rejected_ids:
            continue
        if not include_non_auto and not issue.get("auto_mergeable"):
            continue
        spec = issue["candidate"]
        patch = {}
        if spec.get("type") == "config":
            patch = dict(spec.get("changes", {}))
        elif spec.get("type") == "code_fix":
            # 代码级修复用 sentinel 表达，由 verifier 路由到对应通道
            patch = {"__code_fix__": issue["id"]}
        elif spec.get("type") == "data":
            patch = {"__data_fix__": issue["id"]}
        elif spec.get("type") == "process":
            patch = {"__process_fix__": issue["id"]}
        if not patch:
            continue
        out.append(Candidate(
            cid=issue["id"],
            title=issue["title"],
            family=f"issue/{issue['severity']}",
            patch=patch,
            rationale=issue["detail"],
            evidence=issue.get("evidence", {}),
            detect_by=issue.get("detect_by", "audit"),
            auto_mergeable=bool(issue.get("auto_mergeable")),
            verify=issue.get("verify", ""),
            side_effects=issue.get("side_effects", []),
            invariant_id=issue.get("invariant_id", ""),
        ))
    return out


def fine_weight_candidates(base_cfg: dict, rng: random.Random) -> list[Candidate]:
    """细粒度权重网格（破解候选池穷尽）。

    首轮网格步长太粗（m ∈ {0,0.05,...,0.30} × k ∈ {0.6,0.75,0.9,1.0} = 24 个），
    跑完就穷尽了。而真实最优点很可能落在网格点之间 —— 实测基线附近
    的最优 m 在 0.08-0.12、k 在 0.82-0.88，都不在原网格上。

    本族用更细的步长 + 基线附近的局部扰动，专门搜「网格缝隙」。
    """
    out = []
    m0 = float(base_cfg.get("model_weight", 0.35))
    k0 = float(base_cfg.get("market_weight", 0.85))
    for dm in (-0.03, -0.015, 0.015, 0.03):
        for dk in (-0.06, -0.03, 0.03, 0.06):
            m = round(min(0.95, max(0.0, m0 + dm)), 4)
            k = round(min(1.0, max(0.0, k0 + dk)), 4)
            out.append(Candidate(
                cid=f"fine_m{m:.3f}_k{k:.3f}",
                family="fusion_weight_fine",
                title=f"细网格：model_weight={m:.3f} market_weight={k:.3f}",
                rationale=(
                    f"首轮网格步长太粗，最优点可能落在网格缝隙里。当前基线 "
                    f"m={m0:.3f} k={k0:.3f}，本次做 ±0.015/±0.03 的局部扰动，"
                    f"专门搜粗网格覆盖不到的区域。"),
                patch={"cfg": {"model_weight": m, "market_weight": k}},
                prior="neutral",
            ))
    # 更细的单轴扫描：固定一个，只精调另一个
    for m in [round(m0 + d, 4) for d in (-0.06, -0.045, 0.045, 0.06)]:
        m = min(0.95, max(0.0, m))
        out.append(Candidate(
            cid=f"scan_m{m:.3f}",
            family="fusion_weight_fine",
            title=f"单轴精调：model_weight={m:.3f}",
            rationale=f"固定 market_weight，只精调 model_weight 到 {m:.3f}。",
            patch={"cfg": {"model_weight": m}},
            prior="neutral",
        ))
    for k in [round(k0 + d, 4) for d in (-0.12, -0.08, 0.08, 0.12)]:
        k = min(1.0, max(0.0, k))
        out.append(Candidate(
            cid=f"scan_k{k:.3f}",
            family="fusion_weight_fine",
            title=f"单轴精调：market_weight={k:.3f}",
            rationale=f"固定 model_weight，只精调 market_weight 到 {k:.3f}。",
            patch={"cfg": {"market_weight": k}},
            prior="neutral",
        ))
    return out


def calibration_candidates(base_cfg: dict) -> list[Candidate]:
    """xG 校准系数候选。

    修好 DC 量纲 bug 后，per-league xg_calibration 需要重新校准 —— 它原本是
    在给 DC 的高估打补丁。本族提供以 1.0 为中心的重新校准路径，
    由数据而非人工拍脑袋决定。
    """
    out = []
    for c in (0.70, 0.85, 1.0, 1.15, 1.30):
        out.append(Candidate(
            cid=f"recal_xg_cal_{c:.2f}",
            family="recalibration",
            title=f"全局 xG 校准重置为 {c:.2f}",
            rationale=(
                "dixon_coles 的 attack/defense 量纲 bug 已修（改用 log 形式），"
                "MC 里的 xg_calibration=0.75 补丁与 per-league 系数原本是在给"
                "该 bug 打补丁。现在 DC 不再系统性高估，这些系数需重新校准。"
                f"本候选把全局系数置为 {c:.2f}，由 Brier 裁决新水位。"),
            patch={"cfg": {"xg_calibration": c}},
            prior="neutral",
            reversible=True,
        ))
    return out


def temperature_candidates(base_cfg: dict) -> list[Candidate]:
    """概率温度（概率平滑）候选。

    模型若过度自信（overconfident），温度 <1 会改善 Brier/LogLoss；
    欠自信则 >1。这是一条**单调、可解释**的一维旋钮，
    比在两个权重上联合搜索更不容易过拟合。
    """
    out = []
    for t in (0.90, 0.95, 1.05, 1.10):
        out.append(Candidate(
            cid=f"temp_{t:.2f}",
            family="temperature",
            title=f"概率温度 T={t:.2f}",
            rationale=(
                "概率温度平滑：T<1 降低过度自信，T>1 提升。一维单调旋钮，"
                "比双权重联合搜索更不易过拟合 val 段。"
                f"本候选 T={t:.2f}。"),
            patch={"cfg": {"probability_temperature": t}},
            prior="neutral",
        ))
    return out


def generate(base_cfg: dict, base_post: dict, max_candidates: int = 60,
             seed: int = 20261009, round_idx: int = 0,
             previous: list[str] | None = None,
             rejected_issue_ids: set[str] | None = None,
             include_issues: bool = True,
             adaptive: bool = True) -> list[Candidate]:
    """生成一轮候选并去重。

    round_idx > 0 时**故意与上一轮拉开距离**：优先给出上一轮未尝试过的候选，
    并对权重网格做局部扰动。若每轮生成同一集合，进化就退化为重复采样，
    浪费每日运行且不会发现新东西。

    adaptive=True 时，若粗网格已穷尽（候选池不足 max_candidates），
    自动补充细网格 / 校准 / 温度族 —— 否则第 2 轮起就会开始重复采样。
    """
    rng = random.Random(seed + round_idx * 7919)
    pool = (toggle_candidates(base_post)
            + weight_candidates(base_cfg)
            + combo_candidates(base_post, base_cfg)
            + code_candidates())
    if include_issues:
        # known_issues 优先（它们是已审计的确定缺陷），放在池首
        pool = issue_candidates(base_cfg, base_post, rejected_issue_ids) + pool

    # 自适应扩充：候选池不够时补充新族（破解第 2 轮起的穷尽）
    if adaptive and len(pool) < max_candidates:
        pool = (pool + fine_weight_candidates(base_cfg, rng)
                + calibration_candidates(base_cfg)
                + temperature_candidates(base_cfg))

    seen, uniq = set(), []
    for c in pool:
        fp = repr(sorted(c.patch.items(), key=lambda kv: kv[0]))
        if fp in seen:
            continue
        seen.add(fp)
        uniq.append(c)

    if previous:
        tried = set(previous)
        fresh = [c for c in uniq if c.cid not in tried]
        stale = [c for c in uniq if c.cid in tried]
        # 至少一半给新候选，保证探索
        keep_fresh = max(len(fresh), min(len(fresh), max_candidates - len(stale) // 2))
        uniq = fresh[:keep_fresh] + stale

    if len(uniq) > max_candidates:
        rng.shuffle(uniq)
        uniq = uniq[:max_candidates]
    return uniq
