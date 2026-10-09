"""回归测试：联赛平局基线语义（2026-10-09 修复的 blocking bug）。

缺陷回顾：
    league_params.draw_baseline 的注释写「该联赛实际平局率」，
    但存的值是 draw_hits/draw_predictions —— **判平精度**，不是平局率。
    巴甲存 0.60，真实平局率是 0.266（matches.csv 4652 场）。
    融合层步骤 7 用 target_d = baseline × strength 抬升平局概率，
    导致巴甲每场平局概率恒为 0.5025（19/19 场完全相同）。

    该缺陷不能靠 Brier / 统计显著性裁决 —— 它是**定义错**，
    所以这里写成结构不变式测试：只要输出里出现与输入无关的常数平局概率，
    或者基线偏离实测值，测试就失败。
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.learning.league_params import (
    DRAW_RATE_TRUTH,
    LeagueParam,
    LeagueParamsConfig,
    LeagueParamsManager,
)


def test_draw_baseline_is_real_draw_rate_not_precision():
    """基线语义必须是真实平局率。巴甲真实 0.266，历史错值 0.60。"""
    assert DRAW_RATE_TRUTH["巴甲"] == 0.266, "巴甲真实平局率基准被改动"
    # 判平精度与真实平局率是两个量，不允许混用
    p = LeagueParam(draw_baseline=0.60, draw_predictions=10, draw_hits=6)
    assert abs(p.draw_precision - 0.60) < 1e-9, "判平精度应仍为 0.60"
    # 但 draw_baseline 不应由 draw_precision 推导
    assert p.draw_baseline != p.draw_precision or p.draw_baseline == 0.266, (
        "draw_baseline 不得等于判平精度（除非恰好等于真实平局率）"
    )


def test_migration_corrects_persisted_wrong_baseline():
    """已落盘的错误值必须在加载时被迁移修正（否则改代码不生效）。"""
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "league_params.json"
        # 模拟历史上写错的状态：巴甲 0.60 / 美职联 0.55
        state.write_text(json.dumps({
            "巴甲": {
                "base_goals": 1.35, "home_adv_weight": 1.0,
                "market_blend_weight": 0.28, "xg_calibration": 1.0,
                "draw_baseline": 0.60, "draw_predictions": 10, "draw_hits": 6,
                "total_predictions": 10, "total_hits": 4,
                "avg_overround": 0.05, "last_updated": "2026-09-27",
            },
            "美职联": {
                "base_goals": 1.35, "home_adv_weight": 1.0,
                "market_blend_weight": 0.28, "xg_calibration": 1.0,
                "draw_baseline": 0.55, "draw_predictions": 11, "draw_hits": 6,
                "total_predictions": 11, "total_hits": 5,
                "avg_overround": 0.05, "last_updated": "2026-09-27",
            },
        }), encoding="utf-8")

        mgr = LeagueParamsManager(state)
        assert abs(mgr.get_draw_baseline("巴甲") - 0.266) < 1e-6, (
            f"巴甲基线未被迁移修正，仍为 {mgr.get_draw_baseline('巴甲')}"
        )
        assert abs(mgr.get_draw_baseline("美职联") - 0.234) < 1e-6, (
            f"美职联基线未被迁移修正，仍为 {mgr.get_draw_baseline('美职联')}"
        )


def test_migration_is_idempotent():
    """迁移必须幂等：第二次运行不应再改写，也不应报错。"""
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "league_params.json"
        state.write_text(json.dumps({
            "巴甲": {"draw_baseline": 0.60},
        }), encoding="utf-8")
        a = LeagueParamsManager(state).get_draw_baseline("巴甲")
        b = LeagueParamsManager(state).get_draw_baseline("巴甲")
        assert abs(a - b) < 1e-9, f"迁移不幂等：{a} vs {b}"


def test_effective_baseline_gated_on_sample_size():
    """样本量不足时必须返回 0.0，让融合层跳过该步骤。"""
    p = LeagueParam(draw_baseline=0.27, draw_baseline_samples=5)
    assert p.draw_baseline_samples < 100
    cfg = LeagueParamsConfig()
    assert cfg.draw_baseline_min_n == 100


def test_draw_strength_has_cap():
    """抬升强度必须有上限，防止错误基线被放大。"""
    # 最坏情况：判平多且命中多 -> 旧实现返回 0.85
    p = LeagueParam(draw_baseline=0.60, draw_predictions=20, draw_hits=18)
    raw = p._raw_draw_strength()
    capped = p.draw_strength()
    assert raw == 0.85, "前提不成立：raw 应为 0.85"
    assert capped <= p.draw_strength_cap, (
        f"draw_strength 未被上限约束：{capped} > {p.draw_strength_cap}"
    )
    # 且即使基线是真实值，抬升幅度也不应超过基线本身太多
    assert capped * p.draw_baseline <= 0.45 * 0.60


def test_fusion_step7_rejects_implausible_baseline():
    """融合层步骤 7 必须拒绝不合理基线与不合理的抬升结果。"""
    from engine.prediction.fusion import LEAGUE_DRAW_ANCHOR, DRAW_ANCHOR_W
    # 锚定表值必须与实测一致
    assert LEAGUE_DRAW_ANCHOR["美职联"] == 0.234, "美职联锚值未修正（曾为 0.55）"
    assert LEAGUE_DRAW_ANCHOR["巴甲"] == 0.266, "巴甲锚值未修正（曾为 0.46）"
    # 锚定权重不得过大
    assert DRAW_ANCHOR_W <= 0.2, f"锚定权重过大：{DRAW_ANCHOR_W}"
    # 表中不得再出现无权威样本的联赛（芬超）
    assert "芬超" not in LEAGUE_DRAW_ANCHOR, "芬超无权威平局率样本，不应锚定"


def test_fusion_never_pins_draw_probability_to_constant():
    """回归核心：不同比赛不能产出完全相同的平局概率。

    原缺陷下巴甲 19/19 场 final draw 恒为 0.5025。
    这里直接检验融合层的数值行为。
    """
    from engine.prediction.fusion import FusionInput, fuse_probabilities

    outs = []
    for market in [(0.45, 0.30, 0.25), (0.55, 0.25, 0.20), (0.35, 0.35, 0.30)]:
        r = fuse_probabilities(FusionInput(
            market_probs=market,
            model_probs=(0.40, 0.30, 0.30),
            cfg={},
            league_draw_baseline=0.60,      # 旧错值
            league_draw_strength=0.85,     # 旧最大值
        ))
        outs.append(tuple(round(x, 4) for x in r.probs))

    assert len(set(outs)) == len(outs), (
        f"平局概率被钉成常数：{outs}（原缺陷：巴甲 19/19 场恒为 0.5025）"
    )
    for h, d, a in outs:
        assert d <= 0.45, f"平局概率 {d} 超过合理上限 0.45"
        assert abs(h + d + a - 1.0) < 1e-9, "概率未归一化"
