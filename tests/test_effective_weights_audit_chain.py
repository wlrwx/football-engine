"""有效权重审计链端到端测试（2026-10-11）

验证 predictions.json → 账本 → 读回 的完整闭环，确保"某条预测当时模型
实际占多少权重"可以逐场审计，而不是只能靠外部推算名义值。

背景：名义 champion 权重 0.05/0.75/0.20，在 DJYY 断供时实际参与运算的
比例是 0.0625/0.9375/0。不落盘这个差异，账本就分不清"融合链在跑"和
"某个源已经死了"。
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.review.post_match import PostMatchReviewer, ReviewLedger

DATE = "2026-10-11"


def _write_daily(data_dir: Path, pred_extra: dict) -> None:
    """造一份最小可结算的当日数据。"""
    daily = data_dir / "daily" / DATE
    daily.mkdir(parents=True, exist_ok=True)

    pred = {
        "match_id": "周六001",
        "home_team": "主队",
        "away_team": "客队",
        "competition": "英超",
        "home_win_prob": 0.44,
        "draw_prob": 0.29,
        "away_win_prob": 0.27,
        "model_raw": {"home": 0.40, "draw": 0.30, "away": 0.30},
        "market_fair": [0.45, 0.28, 0.27],
        "home_odds": 2.1,
        "draw_odds": 3.3,
        "away_odds": 3.5,
    }
    pred.update(pred_extra)
    (daily / "predictions.json").write_text(
        json.dumps([pred], ensure_ascii=False), encoding="utf-8"
    )
    (daily / "results.json").write_text(
        json.dumps([
            {
                "match_id": "周六001",
                "home_team": "主队",
                "away_team": "客队",
                "home_score": 1,
                "away_score": 0,
            }
        ], ensure_ascii=False),
        encoding="utf-8",
    )


class TestEffectiveWeightsAuditChain:
    def test_weights_flow_into_ledger(self, tmp_path: Path):
        w = {"model": 0.0625, "market": 0.9375, "djyy": 0.0}
        _write_daily(tmp_path, {
            "fusion_effective_weights": w,
            "fusion_source": "fuse_two_way",
        })

        PostMatchReviewer(tmp_path).review_day(DATE)

        recs = ReviewLedger(tmp_path / "state" / "review_ledger.jsonl").load_window()
        assert len(recs) == 1
        rec = recs[0]
        assert rec.fusion_effective_weights == w
        assert rec.fusion_source == "fuse_two_way"

    def test_missing_field_does_not_crash_settlement(self, tmp_path: Path):
        """老预测文件无新字段 → 结算必须照常跑，字段为 None。"""
        _write_daily(tmp_path, {})

        report = PostMatchReviewer(tmp_path).review_day(DATE)

        assert report["n_matches"] == 1
        recs = ReviewLedger(tmp_path / "state" / "review_ledger.jsonl").load_window()
        assert recs[0].fusion_effective_weights is None
        assert recs[0].fusion_source == ""

    def test_weights_survive_jsonl_roundtrip(self, tmp_path: Path):
        """账本读回后仍是 dict（asdict 序列化 → MatchReview(**json) 重建）。"""
        w = {"model": 0.0625, "market": 0.9375, "djyy": 0.0}
        _write_daily(tmp_path, {
            "fusion_effective_weights": w,
            "fusion_source": "fuse_two_way",
        })
        PostMatchReviewer(tmp_path).review_day(DATE)

        # 直接读原始 JSONL，确认落盘的就是这个结构
        raw = (tmp_path / "state" / "review_ledger.jsonl").read_text(
            encoding="utf-8"
        ).strip()
        rec = json.loads(raw)
        assert rec["fusion_effective_weights"] == w
        assert rec["fusion_source"] == "fuse_two_way"

    def test_djyy_dead_source_is_visible_per_match(self, tmp_path: Path):
        """DJYY 断供时每条记录 djyy=0，可被统计脚本直接识别。"""
        _write_daily(tmp_path, {
            "fusion_effective_weights": {
                "model": 0.0625, "market": 0.9375, "djyy": 0.0,
            },
            "fusion_source": "fuse_two_way",
        })
        PostMatchReviewer(tmp_path).review_day(DATE)

        recs = ReviewLedger(tmp_path / "state" / "review_ledger.jsonl").load_window()
        assert recs[0].fusion_effective_weights["djyy"] == 0.0
        assert recs[0].fusion_source != "fuse_three_way"
