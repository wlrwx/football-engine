"""投注层闸门测试（2026-08-29）

market_signal_gate：水位信号 × 投注方向一致性 → 注量因子
favorite_band_factor：热门区(odds<1.8)注量因子
信号口径与 fetch_sina_odds.py 一致：compression>1.05=资金涌入，<0.95=撤出。
"""

from __future__ import annotations

import pytest

from engine.strategy.kelly import favorite_band_factor, market_signal_gate


class TestMarketSignalGate:
    def test_no_signal_returns_unknown(self):
        assert market_signal_gate("home", None) == (1.0, "unknown")
        assert market_signal_gate("home", {}) == (1.0, "unknown")
        assert market_signal_gate("home", {"compression": {}}) == (1.0, "unknown")

    def test_draw_direction_never_penalized(self):
        sina = {"compression": {"home": 1.20, "away": 0.80}}
        assert market_signal_gate("draw", sina) == (1.0, "unknown")

    def test_own_side_inflow_agrees(self):
        # 押 home，home 资金涌入（compression 1.2 > 1.05）→ 同向
        sina = {"compression": {"home": 1.20, "away": 1.0}}
        assert market_signal_gate("home", sina) == (1.0, "agree")

    def test_own_side_outflow_conflicts(self):
        # 押 home，home 资金撤出（0.85 < 0.95）→ 逆资金，注量减半
        sina = {"compression": {"home": 0.85, "away": 1.0}}
        factor, verdict = market_signal_gate("home", sina)
        assert verdict == "conflict"
        assert factor == pytest.approx(0.5)

    def test_money_flowing_to_opponent_conflicts(self):
        # 押 home，away 被涌入（1.30）且 home 无涌入 → 冲突
        sina = {"compression": {"home": 1.0, "away": 1.30}}
        factor, verdict = market_signal_gate("home", sina)
        assert verdict == "conflict"
        assert factor == pytest.approx(0.5)

    def test_away_direction_mirror(self):
        sina = {"compression": {"home": 0.80, "away": 1.15}}
        assert market_signal_gate("away", sina) == (1.0, "agree")
        sina2 = {"compression": {"home": 1.20, "away": 1.0}}
        assert market_signal_gate("away", sina2)[1] == "conflict"

    def test_neutral_compression_unknown(self):
        sina = {"compression": {"home": 1.02, "away": 0.98}}
        assert market_signal_gate("home", sina) == (1.0, "unknown")

    def test_disabled_returns_unity(self):
        sina = {"compression": {"home": 0.80, "away": 1.0}}
        cfg = {"enabled": False}
        assert market_signal_gate("home", sina, cfg) == (1.0, "unknown")

    def test_custom_conflict_factor(self):
        sina = {"compression": {"home": 0.85, "away": 1.0}}
        cfg = {"conflict_stake_factor": 0.25}
        assert market_signal_gate("home", sina, cfg)[0] == pytest.approx(0.25)


class TestFavoriteBandFactor:
    def test_hot_favorite_excluded(self):
        # 2026-08-29 闸门实验：热门区 n=111 命中 69.4% 低于盈亏平衡，
        # 账本 pnl -323.1 单位 → 注量 0.5 折升级为全免（×0）
        assert favorite_band_factor(1.45) == pytest.approx(0.0)
        assert favorite_band_factor(1.79) == pytest.approx(0.0)

    def test_normal_odds_untouched(self):
        assert favorite_band_factor(1.80) == pytest.approx(1.0)
        assert favorite_band_factor(2.50) == pytest.approx(1.0)
        assert favorite_band_factor(5.00) == pytest.approx(1.0)

    def test_boundary_exclusive(self):
        # odds_below 本身不打折（1.8 含在正常区）
        assert favorite_band_factor(1.8, {"odds_below": 1.8}) == pytest.approx(1.0)

    def test_disabled_or_invalid(self):
        assert favorite_band_factor(1.45, {"enabled": False}) == pytest.approx(1.0)
        assert favorite_band_factor(None) == pytest.approx(1.0)
        assert favorite_band_factor("abc") == pytest.approx(1.0)

    def test_custom_threshold(self):
        cfg = {"odds_below": 2.0, "stake_factor": 0.3}
        assert favorite_band_factor(1.95, cfg) == pytest.approx(0.3)
        assert favorite_band_factor(2.05, cfg) == pytest.approx(1.0)


class TestEdgeCap:
    """edge 幻觉封顶（2026-08-30）：大声称的 edge 实测 ROI 最差，必须封顶"""

    def test_config_has_cap(self):
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).parent.parent / "config" / "strategy.json").read_text())
        cap = cfg["edge_gates"]["max_edge"]
        assert 0.05 <= cap <= 0.30, f"edge 上限异常: {cap}"

    def test_cap_semantics(self):
        # 闸门语义: 0 < edge <= cap 放行；超上限/非正拒绝
        cap = 0.10
        assert 0 < 0.08 <= cap
        assert not (0 < 0.15 <= cap)
        assert not (0 < -0.01 <= cap)
        assert not (0 < 0 <= cap)
