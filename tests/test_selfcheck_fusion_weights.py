"""selfcheck 融合权重告警测试（2026-10-11）

DJYY 断供两个月期间，config 一直挂着 djyy_weight=0.20，融合层靠重新
归一化兜住（实际贡献 0），日志/配置/账本三处都看不出异常。

selfcheck 新增第 6 项检查：把逐场落盘的 fusion_effective_weights 变成
自动告警（名义权重非零但实际贡献 0 = 死源）。
"""

from __future__ import annotations

import json
from pathlib import Path

from engine import selfcheck

DAY = "2026-10-11"


def _mk_preds(n: int, weights: dict, source: str) -> list[dict]:
    return [
        {
            "match_id": f"周六{i:03d}",
            "home_team": "主",
            "away_team": "客",
            "competition": "英超",
            "home_win_prob": 0.44,
            "draw_prob": 0.29,
            "away_win_prob": 0.27,
            "market_fair": [0.45, 0.28, 0.27],
            "fusion_effective_weights": dict(weights),
            "fusion_source": source,
        }
        for i in range(1, n + 1)
    ]


def _write(tmp_path: Path, preds: list[dict]) -> None:
    d = tmp_path / "daily" / DAY
    d.mkdir(parents=True, exist_ok=True)
    (d / "predictions.json").write_text(
        json.dumps(preds, ensure_ascii=False), encoding="utf-8"
    )


class TestFusionWeightAlerts:
    def test_dead_djyy_source_raises_alert(self, tmp_path, monkeypatch):
        monkeypatch.setattr(selfcheck, "DAILY", tmp_path / "daily")
        # DJYY 全场缺失：走两路融合，djyy 实际贡献 0
        _write(tmp_path, _mk_preds(
            20, {"model": 0.0625, "market": 0.9375, "djyy": 0.0}, "fuse_two_way"
        ))

        alerts = selfcheck.check_day(DAY)

        assert any("DJYY 是死源" in a for a in alerts)

    def test_live_djyy_source_no_dead_alert(self, tmp_path, monkeypatch):
        monkeypatch.setattr(selfcheck, "DAILY", tmp_path / "daily")
        _write(tmp_path, _mk_preds(
            20, {"model": 0.05, "market": 0.75, "djyy": 0.20}, "fuse_three_way"
        ))

        alerts = selfcheck.check_day(DAY)

        assert not any("死源" in a for a in alerts)

    def test_missing_field_raises_audit_alert(self, tmp_path, monkeypatch):
        monkeypatch.setattr(selfcheck, "DAILY", tmp_path / "daily")
        preds = _mk_preds(5, {}, "fuse_two_way")
        for p in preds:
            p.pop("fusion_effective_weights")
        _write(tmp_path, preds)

        alerts = selfcheck.check_day(DAY)

        assert any("fusion_effective_weights" in a for a in alerts)

    def test_low_model_weight_is_visible(self, tmp_path, monkeypatch):
        """模型有效权重 6.25% → 提醒"系统实质接近纯市场基准"。"""
        monkeypatch.setattr(selfcheck, "DAILY", tmp_path / "daily")
        _write(tmp_path, _mk_preds(
            20, {"model": 0.0625, "market": 0.9375, "djyy": 0.0}, "fuse_two_way"
        ))

        alerts = selfcheck.check_day(DAY)

        assert any("实质接近纯市场基准" in a for a in alerts)

    def test_partial_source_coverage_flagged(self, tmp_path, monkeypatch):
        """DJYY 只在少数场次生效 → 覆盖率告警而非"死源"。"""
        monkeypatch.setattr(selfcheck, "DAILY", tmp_path / "daily")
        preds = _mk_preds(
            10, {"model": 0.0625, "market": 0.9375, "djyy": 0.0}, "fuse_two_way"
        )
        preds[0]["fusion_effective_weights"] = {
            "model": 0.05, "market": 0.75, "djyy": 0.20,
        }
        preds[0]["fusion_source"] = "fuse_three_way"
        _write(tmp_path, preds)

        alerts = selfcheck.check_day(DAY)

        assert any("覆盖率偏低" in a for a in alerts)
        assert not any("DJYY 是死源" in a for a in alerts)

    def test_healthy_full_stack_has_no_weight_alerts(self, tmp_path, monkeypatch):
        """三路全活 + 模型权重不低 → 不产生任何权重类告警。"""
        monkeypatch.setattr(selfcheck, "DAILY", tmp_path / "daily")
        _write(tmp_path, _mk_preds(
            20, {"model": 0.30, "market": 0.50, "djyy": 0.20}, "fuse_three_way"
        ))

        alerts = selfcheck.check_day(DAY)

        weight_alerts = [
            a for a in alerts
            if "死源" in a or "覆盖率偏低" in a
            or "实质接近纯市场基准" in a or "fusion_effective_weights" in a
        ]
        assert weight_alerts == []

