"""E3 闸门可达性的公式与口径回归测试。

核心是 `required_weight()`：EV 闸门通过所需的模型权重。写死它的原因与
E2 那条恒等式一样 —— 推导一旦被无声改掉，结论会整体翻转而输出仍然"正常"。

另外锁住 edge 闸门的口径事实：`kelly.py` 用含抽水的 `1.0/odds`，
系统其它模块用去水概率。这个差异是 E3 结论的一部分，不是笔误。
"""
import pytest

pytest.importorskip("numpy")

from engine.backtest.gate_reachability import (  # noqa: E402
    VIG, W_CONFIG, MIN_EDGE, MIN_EV,
    edge_gate_effective, required_weight,
)


class TestRequiredWeight:
    def test_matches_closed_form(self):
        """直接对着 EV 闸门定义验一遍，不依赖实现自身。"""
        p_fair, p_dc, vig, min_ev = 0.30, 0.45, 0.121, 0.03
        odds = 1.0 / (p_fair * (1.0 + vig))
        w = required_weight(p_fair, p_dc, vig, min_ev)
        # 该权重下 p_sys*odds 应恰好等于 1+min_ev
        p_sys = w * p_dc + (1 - w) * p_fair
        assert abs(p_sys * odds - (1 + min_ev)) < 1e-9

    def test_below_required_weight_fails(self):
        p_fair, p_dc = 0.30, 0.45
        odds = 1.0 / (p_fair * (1 + VIG))
        w = required_weight(p_fair, p_dc)
        p_sys = (w - 0.01) * p_dc + (1 - w + 0.01) * p_fair
        assert p_sys * odds < (1 + MIN_EV) + 1e-9

    def test_model_worse_than_market_needs_negative_weight(self):
        """模型不如市场时，必须靠反向跟随模型才可能拉开 EV —— 这不是好信号。"""
        assert required_weight(0.40, 0.30) < 0

    def test_perfect_model_still_needs_positive_weight(self):
        """抽水 > min_ev 时，完美模型也得靠权重去补，所以要求仍是正的。"""
        assert required_weight(0.40, 1.0) > 0

    def test_denominator_zero_returns_inf(self):
        assert required_weight(0.4, 0.4) == float("inf")

    def test_longshots_need_less_weight(self):
        """同一模型偏离下，长赔率的所需权重更低。"""
        d = 0.20
        assert required_weight(0.10, 0.10 + d) < required_weight(0.40, 0.40 + d)

    def test_config_weight_is_below_realistic_requirement(self):
        """锁住 E3 的核心事实：实测偏离量下配置权重不够。

        若哪天模型真的变好了导致这个断言失败，说明 E3 结论需要重做 ——
        那是好事，但必须显式地失败，不能默默继续沿用旧结论。
        """
        p_dc, p_fair = 0.50, 0.46      # 账本实测的中位偏离量级
        assert required_weight(p_fair, p_dc) > W_CONFIG


class TestEdgeGateConvention:
    def test_effective_threshold_exceeds_nominal(self):
        """用含抽水的 1/odds 比较 → 实际门槛必然高于参数名。"""
        for p_fair in (0.10, 0.20, 0.30, 0.40):
            assert edge_gate_effective(p_fair) > MIN_EDGE

    def test_matches_derivation(self):
        p_fair = 0.40
        assert abs(edge_gate_effective(p_fair) - (MIN_EDGE + p_fair * VIG)) < 1e-12

    def test_ratio_range_matches_documented_1_4_to_2_6(self):
        """文档里写了「热门选项上严 2.6 倍」，这个数字必须对得上。"""
        assert abs(edge_gate_effective(0.40) / MIN_EDGE - 2.61) < 0.01
        assert abs(edge_gate_effective(0.10) / MIN_EDGE - 1.40) < 0.01

    def test_kelly_actually_uses_raw_odds(self):
        """锁住 kelly.py 的现状：market_prob = 1.0 / odds（未去水）。

        这是 E3 结论的事实前提。若有人改成去水口径，这个测试会失败，
        提醒他同步更新 E3 的结论与本文档 —— 而不是让文档悄悄过期。
        """
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "engine" / "strategy" / "kelly.py").read_text(encoding="utf-8")
        code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
        assert "market_prob = 1.0 / odds" in code
        assert "edge = model_prob - market_prob" in code
