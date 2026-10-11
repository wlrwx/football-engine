"""赔率分层口径统一 + 下注决策记录测试（2026-10-11）

## 背景 1：三套分层口径不一致

修复前账本分 1.5-2.0 / 2.0-3.0，ev_report 分 L1-L5，闸门边界 1.8。
账本的 1.5-2.0 桶**横跨**闸门边界 → "odds 1.8-2.2 系统性亏损"
这类结论在账本里结构性不可见。

## 背景 2：闸门判定必须按**实际注**算

实测 2026-09-01 周二007：1X2 主推主胜赔率 1.46（L1，应禁投），
但实际出的是让球客胜 2.28（L4，应放行）。若按 1X2 主推判闸门，
会把一笔**已放行**的注记成"禁投"。
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.odds_bands import (
    LAYER_TOP,
    gate_blocked_layer_names,
    layer_of,
    layer_short,
)
from engine.review.post_match import PostMatchReviewer, ReviewLedger

DAY = "2026-09-01"


class TestLayerBuckets:
    def test_edges_match_gate_boundary(self):
        """分层边界必须与 favorite_band 的 1.8 对齐。"""
        assert layer_of(1.49) == "L1 大热(<1.5)"
        assert layer_of(1.50) == "L2 热(1.5-1.8)"
        assert layer_of(1.79) == "L2 热(1.5-1.8)"
        assert layer_of(1.80) == "L3 中(1.8-2.2)"
        assert layer_of(2.19) == "L3 中(1.8-2.2)"
        assert layer_of(2.20) == "L4 冷(2.2-3.0)"
        assert layer_of(3.00) == LAYER_TOP

    def test_l3_no_longer_straddles_gate(self):
        """回归防护：L2 全部 <1.8，L3 全部 >=1.8，不得再有跨边界的桶。"""
        for o in (1.5, 1.6, 1.7, 1.79):
            assert layer_short(o) == "L2"
        for o in (1.8, 1.9, 2.0, 2.1, 2.19):
            assert layer_short(o) == "L3"

    def test_invalid_odds(self):
        assert layer_of(None) == "unknown"
        assert layer_of(0) == "unknown"
        assert layer_of(1.0) == "unknown"

    def test_gate_blocked_layers_from_config(self):
        """闸门禁投层由配置推导，不硬编码。"""
        assert gate_blocked_layer_names({"enabled": True, "odds_below": 1.8}) == {
            "L1 大热(<1.5)", "L2 热(1.5-1.8)",
        }
        assert gate_blocked_layer_names({"enabled": True, "odds_below": 2.2}) == {
            "L1 大热(<1.5)", "L2 热(1.5-1.8)", "L3 中(1.8-2.2)",
        }
        assert gate_blocked_layer_names({"enabled": False}) == set()


def _seed(tmp_path: Path, plan: dict | None) -> None:
    """造一份最小可结算数据：1X2 主推主胜 1.46（L1）。"""
    d = tmp_path / "daily" / DAY
    d.mkdir(parents=True, exist_ok=True)
    pred = {
        "match_id": "周二001",
        "home_team": "主",
        "away_team": "客",
        "competition": "英超",
        "home_win_prob": 0.55,
        "draw_prob": 0.25,
        "away_win_prob": 0.20,
        "home_odds": 1.46,
        "draw_odds": 3.8,
        "away_odds": 5.5,
        "market_fair": [0.55, 0.25, 0.20],
    }
    (d / "predictions.json").write_text(
        json.dumps([pred], ensure_ascii=False), encoding="utf-8")
    (d / "results.json").write_text(json.dumps([{
        "match_id": "周二001",
        "home_team": "主",
        "away_team": "客",
        "home_score": 1,
        "away_score": 0,
    }], ensure_ascii=False), encoding="utf-8")
    if plan is not None:
        (d / "ticket_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8")


class TestBetDecisionRecord:
    def test_no_bet_records_counterfactual_layer(self, tmp_path):
        """未出注 → 记录 1X2 主推方向所在层，注额 0。"""
        _seed(tmp_path, plan=None)
        PostMatchReviewer(tmp_path).review_day(DAY)
        r = ReviewLedger(tmp_path / "state" / "review_ledger.jsonl").load_window()[0]

        assert r.stake_planned == 0.0
        assert r.odds_layer == "L1 大热(<1.5)"
        assert r.bet_sel == "home"
        assert r.gate_blocked is True       # 1.46 < 1.8 → 禁投

    def test_actual_bet_overrides_1x2_argmax(self, tmp_path):
        """关键回归：1X2 主推 1.46(L1)，但实际下注让球 2.28(L4)。
        闸门与分层必须按**实际注**记，不能记成"禁投"。"""
        _seed(tmp_path, plan={
            "value": [{
                "match": "周二001",
                "sel": "hcap_away",
                "odds": 2.28,
                "stake": 150.0,
            }],
        })
        PostMatchReviewer(tmp_path).review_day(DAY)
        r = ReviewLedger(tmp_path / "state" / "review_ledger.jsonl").load_window()[0]

        assert r.odds_used == 2.28
        assert r.odds_layer == "L4 冷(2.2-3.0)"
        assert r.bet_sel == "hcap_away"
        assert r.stake_planned == 150.0
        assert r.gate_blocked is False      # 2.28 >= 1.8 → 放行

    def test_parlay_leg_stake_is_amortized(self, tmp_path):
        """串关注额按腿数摊销到每条腿，账本层面口径一致。"""
        _seed(tmp_path, plan={
            "parlay": [{
                "stake": 30.0,
                "total_odds": 4.0,
                "legs": [
                    {"match": "周二001", "sel": "away", "odds": 2.0},
                    {"match": "周二999", "sel": "home", "odds": 2.0},
                ],
            }],
        })
        PostMatchReviewer(tmp_path).review_day(DAY)
        r = ReviewLedger(tmp_path / "state" / "review_ledger.jsonl").load_window()[0]

        assert r.stake_planned == 15.0       # 30 / 2 腿
        assert r.odds_layer == "L3 中(1.8-2.2)"

    def test_missing_ticket_plan_is_safe(self, tmp_path):
        """ticket_plan.json 缺失 → 结算照常，注额 0。"""
        _seed(tmp_path, plan=None)
        report = PostMatchReviewer(tmp_path).review_day(DAY)

        assert report["n_matches"] == 1
        r = ReviewLedger(tmp_path / "state" / "review_ledger.jsonl").load_window()[0]
        assert r.stake_planned == 0.0


