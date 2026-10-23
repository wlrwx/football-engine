"""回归测试：市场概率不双重计入（P0#1）+ djyy-only 置信门槛（P1#7）
+ 优化器反事实与生产融合一致（P1#8）。"""
from __future__ import annotations

import json
from pathlib import Path

from engine.learning.fusion_optimizer import FusionOptimizer, FusionWeights
from engine.prediction.fusion import (
    DEFAULT_POST_FUSION,
    FusionInput,
    fuse_probabilities,
)
from engine.review.post_match import MatchReview, ReviewLedger

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- P0#1

def test_market_is_counted_once() -> None:
    """市场信息只能进入预测链一次：DC 内混与 fusion 市场权重不能同时开启。

    双重计入时的实际市场暴露 = fusion.market_weight + model_weight ×
    market_blend_weight（例：0.75 + 0.05×0.28 ≈ 0.764，高于配置意图）。
    """
    cfg = json.loads((ROOT / "config" / "prediction.json").read_text(encoding="utf-8"))
    blend = float(cfg["prediction"].get("market_blend_weight", 0.0))
    fusion_market = float(cfg["fusion"].get("market_weight", 0.0))
    assert blend == 0.0 or fusion_market == 0.0, (
        f"市场概率被双重计入：DC 内混 {blend} 且 fusion 市场权重 {fusion_market}"
    )


# ---------------------------------------------------------------- P1#7

def _djyy_only_inp(conf_home: float) -> FusionInput:
    return FusionInput(
        model_probs=(0.40, 0.30, 0.30),
        market_probs=None,
        djyy_probs={"home": conf_home, "draw": 0.25, "away": 0.75 - conf_home},
        cfg={"model_weight": 0.10, "market_weight": 0.60, "djyy_weight": 0.30,
             "djyy_min_confidence": 0.50},
        post_fusion={k: False for k in DEFAULT_POST_FUSION},
    )


def test_djyy_only_low_confidence_falls_back_to_model() -> None:
    res = fuse_probabilities(_djyy_only_inp(0.42))
    assert res.source == "fuse_model_only"
    assert res.probs[0] == 0.40


def test_djyy_only_high_confidence_uses_djyy() -> None:
    res = fuse_probabilities(_djyy_only_inp(0.60))
    assert res.source == "fuse_djyy_only"


# ---------------------------------------------------------------- P1#8

def _review(**kw) -> MatchReview:
    base = dict(
        match_id="2026-10-23_周一001", date="2026-10-23", league="英超",
        actual_idx=0, model_raw=[0.50, 0.28, 0.22], market_fair=[0.48, 0.29, 0.23],
        djyy_prob=[0.52, 0.26, 0.22], final_prob=[0.50, 0.28, 0.22],
        confidence_tier="mid", odds_band="1.5-2.0", best_selection=0,
        hit=True, pnl=1.0, brier_model=0.4, brier_market=0.4, brier_djyy=None,
        brier_final=0.4, total_goals_actual=2,
    )
    base.update(kw)
    return MatchReview(**base)


def _optimizer(tmp_path) -> FusionOptimizer:
    return FusionOptimizer(tmp_path / "opt_state.json", ReviewLedger(tmp_path / "l.jsonl"), {})


def test_counterfactual_matches_production_fusion(tmp_path) -> None:
    """反事实必须等于生产 fuse_probabilities（后处理全关）的输出。"""
    w = FusionWeights(model=0.20, market=0.60, djyy=0.20)
    rec = _review()
    got = _optimizer(tmp_path)._fused_probs(rec, w)

    prod = fuse_probabilities(FusionInput(
        model_probs=tuple(rec.model_raw),
        market_probs=tuple(rec.market_fair),
        djyy_probs={"home": 0.52, "draw": 0.26, "away": 0.22},
        cfg={"model_weight": w.model, "market_weight": w.market, "djyy_weight": w.djyy},
        post_fusion={k: False for k in DEFAULT_POST_FUSION},
    )).probs
    assert got == list(prod), f"反事实与生产融合不一致: {got} vs {prod}"


def test_counterfactual_none_without_model_raw(tmp_path) -> None:
    rec = _review(model_raw=[])
    assert _optimizer(tmp_path)._fused_probs(rec, FusionWeights()) is None


def test_counterfactual_ignores_absent_sources(tmp_path) -> None:
    rec = _review(market_fair=None, djyy_prob=None)
    got = _optimizer(tmp_path)._fused_probs(rec, FusionWeights(model=0.2, market=0.6, djyy=0.2))
    assert got == [0.50, 0.28, 0.22], "单源时应退化为原始模型概率"
