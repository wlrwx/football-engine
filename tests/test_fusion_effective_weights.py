"""有效权重落盘测试（2026-10-11）

背景：config / fusion_weights.json 里写的是**名义权重**，但源缺失时
fuse_probabilities 会重新归一化，实际比例与名义值不同。实测 champion
0.05/0.75/0.20 在 djyy 缺失时 → model 6.25% / market 93.75% / djyy 0。

这组测试锁定「有效权重 == 真正参与运算的比例」，防止：
  1. 回归成返回名义权重（账本从此说谎）
  2. 归一化算错（比例加起来不等于 1）
  3. 裸模型分支被误标成有市场权重
"""

from __future__ import annotations

from engine.prediction.fusion import FusionInput, fuse_probabilities

# 生产实际使用的 champion 权重（data/state/fusion_weights.json）
CHAMPION = {"model_weight": 0.05, "market_weight": 0.75, "djyy_weight": 0.20}

MODEL = (0.40, 0.30, 0.30)
MARKET = (0.45, 0.28, 0.27)
DJYY = {"home": 0.50, "draw": 0.25, "away": 0.25}


def _in(**kw) -> FusionInput:
    base = {"model_probs": MODEL, "cfg": dict(CHAMPION)}
    base.update(kw)
    return FusionInput(**base)


class TestEffectiveWeights:
    def test_djyy_missing_renormalizes_to_model_625(self):
        """DJYY 断供 → 实际 model 6.25% / market 93.75%，而非名义 5%/75%。"""
        out = fuse_probabilities(_in(market_probs=MARKET, djyy_probs=None))

        assert out.source == "fuse_two_way"
        assert out.effective_weights == {
            "model": 0.0625,
            "market": 0.9375,
            "djyy": 0.0,
        }

    def test_effective_weights_sum_to_one(self):
        out = fuse_probabilities(_in(market_probs=MARKET, djyy_probs=DJYY))
        total = sum(out.effective_weights.values())
        assert abs(total - 1.0) < 1e-3

    def test_three_way_keeps_djyy_weight(self):
        """三路齐全时有效权重应贴近名义权重（归一化后基本不变）。"""
        out = fuse_probabilities(_in(market_probs=MARKET, djyy_probs=DJYY))

        assert out.source == "fuse_three_way"
        w = out.effective_weights
        assert w["djyy"] > 0
        assert w["model"] < w["market"]

    def test_low_confidence_djyy_zeroes_its_weight(self):
        """DJYY 置信度低于门槛 → 权重归零，退化为两路融合。"""
        weak = {"home": 0.36, "draw": 0.34, "away": 0.30}  # max=0.36 < 0.50
        cfg = dict(CHAMPION)
        cfg["djyy_min_confidence"] = 0.50

        out = fuse_probabilities(
            _in(market_probs=MARKET, djyy_probs=weak, cfg=cfg)
        )

        assert out.source == "fuse_three_way"
        assert out.effective_weights["djyy"] == 0.0
        # 归一化后 model+market = 1
        assert abs(
            out.effective_weights["model"] + out.effective_weights["market"] - 1.0
        ) < 1e-3

    def test_bare_model_branch_has_no_weights(self):
        """无市场无 DJYY → 裸模型，权重为 None（单源无需权重）。"""
        out = fuse_probabilities(_in(market_probs=None, djyy_probs=None))

        assert out.source == "fuse_model_only"
        assert out.effective_weights is None
        # 概率应等于模型原值
        for got, want in zip(out.probs, MODEL):
            assert abs(got - want) < 1e-6

    def test_djyy_only_branch(self):
        out = fuse_probabilities(_in(market_probs=None, djyy_probs=DJYY))

        assert out.source == "fuse_djyy_only"
        w = out.effective_weights
        assert w["market"] == 0.0
        assert w["djyy"] > 0

    def test_backward_compat_defaults(self):
        """老调用方不传新字段时不应崩。"""
        out = fuse_probabilities(_in(market_probs=MARKET))
        assert hasattr(out, "effective_weights")
        assert hasattr(out, "source")
