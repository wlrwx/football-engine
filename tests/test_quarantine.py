"""污染样本隔离测试（2026-10-11）

背景：2026-08-30 ~ 10-10，league_draw_baseline 用错误的"判平精度"
（巴甲 0.60，真实平局率 0.266）把平局概率抬到 0.5025。这 21 场已落盘
样本里 20 场进了 v2 洁净链，会喂给 calibration_auto / lgbm_shadow /
upgrade_tracker / ablation_replay。

修法是"标记 + 过滤"而非升 chain 版本——污染只占 3.6%，
不该让 538 条干净样本陪葬。
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.integrity.quarantine import (
    BAD_STEPS,
    build_quarantine,
    clean_records,
    contaminated_ids,
    detect_from_trace,
    load_quarantine,
)
from engine.review.post_match import PostMatchReviewer, ReviewLedger

DAY = "2026-10-07"


def _trace(step: str, draw: float) -> list[dict]:
    return [{"step": step, "after": [0.3, draw, 0.2]}]


class TestDetectFromTrace:
    def test_flags_out_of_range_draw_baseline(self):
        assert detect_from_trace(_trace("league_draw_baseline", 0.51)) is True

    def test_flags_out_of_range_anchor(self):
        assert detect_from_trace(_trace("league_draw_anchor", 0.4675)) is True

    def test_normal_trace_not_flagged(self):
        """真实联赛平局率 0.20~0.33，抬到 0.40 以下不算污染。"""
        assert detect_from_trace(_trace("league_draw_baseline", 0.33)) is False
        assert detect_from_trace(_trace("league_draw_anchor", 0.30)) is False

    def test_unrelated_steps_ignored(self):
        assert detect_from_trace(_trace("combo_boost", 0.99)) is False

    def test_empty_and_malformed(self):
        assert detect_from_trace(None) is False
        assert detect_from_trace([]) is False
        assert detect_from_trace([{"step": "league_draw_baseline"}]) is False
        assert detect_from_trace(
            [{"step": "league_draw_baseline", "after": [0.3, "x", 0.2]}]
        ) is False

    def test_caps_above_realistic_range(self):
        """上限必须高于任何真实联赛平局率，否则会误杀干净样本。"""
        for _, cap in BAD_STEPS:
            assert cap > 0.33, f"上限 {cap} 低于真实平局率上界"


class TestBuildAndFilter:
    def _seed_day(self, root: Path, traces: list) -> None:
        d = root / "data" / "daily" / DAY
        d.mkdir(parents=True, exist_ok=True)
        preds = []
        for i, tr in enumerate(traces):
            preds.append({
                "match_id": f"周三{i:03d}",
                "home_team": f"主{i}", "away_team": f"客{i}",
                "competition": "巴甲",
                "home_win_prob": 0.38, "draw_prob": 0.5025,
                "away_win_prob": 0.115,
                "home_odds": 1.46, "draw_odds": 3.6, "away_odds": 8.0,
                "market_fair": [0.58, 0.25, 0.17],
                "fusion_trace": tr,
            })
        (d / "predictions.json").write_text(
            json.dumps(preds, ensure_ascii=False), encoding="utf-8")

    def test_build_quarantine_finds_only_bad(self, tmp_path):
        self._seed_day(tmp_path, [
            _trace("league_draw_baseline", 0.51),
            None,
            _trace("league_draw_baseline", 0.33),
        ])
        out = build_quarantine(tmp_path)
        assert out["count"] == 1
        assert "周三000" in out["match_ids"]
        assert "周三002" not in out["match_ids"]

    def test_quarantine_file_roundtrip(self, tmp_path):
        self._seed_day(tmp_path, [_trace("league_draw_baseline", 0.51)])
        build_quarantine(tmp_path)
        assert load_quarantine(tmp_path) == {"周三000"}

    def test_missing_quarantine_file_is_not_fatal(self, tmp_path):
        """隔离名单不存在时返回空集，不能阻断主流程。"""
        assert load_quarantine(tmp_path) == set()

    def test_clean_records_filters_both_sources(self, tmp_path):
        self._seed_day(tmp_path, [
            _trace("league_draw_baseline", 0.51), None, None,
        ])
        build_quarantine(tmp_path)
        recs = [
            {"match_id": "周三000"},
            {"match_id": "周三001", "contaminated": True},
            {"match_id": "周三002"},
        ]
        assert len(clean_records(recs, tmp_path)) == 1
        assert contaminated_ids(recs, tmp_path) == {"周三000", "周三001"}


class TestSettlementMarksContaminated:
    def _seed(self, tmp_path: Path) -> None:
        d = tmp_path / "daily" / DAY
        d.mkdir(parents=True, exist_ok=True)
        preds = [
            {
                "match_id": "周三000", "home_team": "主", "away_team": "客",
                "competition": "巴甲",
                "home_win_prob": 0.38, "draw_prob": 0.5025,
                "away_win_prob": 0.115,
                "home_odds": 1.46, "draw_odds": 3.6, "away_odds": 8.0,
                "market_fair": [0.58, 0.25, 0.17],
                "fusion_trace": _trace("league_draw_baseline", 0.51),
            },
            {
                "match_id": "周三001", "home_team": "主2", "away_team": "客2",
                "competition": "英超",
                "home_win_prob": 0.55, "draw_prob": 0.25, "away_win_prob": 0.20,
                "home_odds": 1.80, "draw_odds": 3.5, "away_odds": 4.5,
                "market_fair": [0.55, 0.25, 0.20],
                "fusion_trace": _trace("league_draw_baseline", 0.33),
            },
        ]
        results = [
            {"match_id": "周三000", "home_team": "主", "away_team": "客",
             "home_score": 1, "away_score": 0},
            {"match_id": "周三001", "home_team": "主2", "away_team": "客2",
             "home_score": 2, "away_score": 1},
        ]
        (d / "predictions.json").write_text(
            json.dumps(preds, ensure_ascii=False), encoding="utf-8")
        (d / "results.json").write_text(
            json.dumps(results, ensure_ascii=False), encoding="utf-8")

    def test_settlement_flags_contaminated_match(self, tmp_path):
        """结算时从 fusion_trace 判定并写 contaminated 字段。"""
        self._seed(tmp_path)
        PostMatchReviewer(tmp_path).review_day(DAY)
        recs = ReviewLedger(tmp_path / "state" / "review_ledger.jsonl").load_window()
        by_id = {r.match_id: r for r in recs}

        assert by_id["周三000"].contaminated is True
        assert "0.51" in by_id["周三000"].contaminated_reason
        assert by_id["周三001"].contaminated is False
        assert by_id["周三001"].contaminated_reason == ""

    def test_learning_side_filters_flagged_match(self, tmp_path):
        """学习侧只拿到干净样本。"""
        self._seed(tmp_path)
        PostMatchReviewer(tmp_path).review_day(DAY)
        recs = ReviewLedger(tmp_path / "state" / "review_ledger.jsonl").load_window()
        clean = clean_records(
            [{"match_id": r.match_id, "contaminated": r.contaminated} for r in recs],
            tmp_path,
        )
        assert [r["match_id"] for r in clean] == ["周三001"]


