"""水位序列跨周污染过滤测试（2026-08-30）

load_series 的快照窗口过滤: 快照捕获日期必须落在 [销售日-3天, 销售日+1天]，
上周同编号场次的旧快照与无效时间戳快照一律剔除。
"""

from __future__ import annotations

import json

import pytest

import engine.odds_series as osd


def _snap(ts, match_no="周日002", match_time="2026-08-30"):
    return {"ts": ts, "match_no": match_no, "match_time": match_time,
            "euro": {"home": 2.0, "draw": 3.2, "away": 3.5}}


def _write_series(tmp_path, name, snaps):
    (tmp_path / name).write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in snaps), encoding="utf-8")


@pytest.fixture
def series_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(osd, "SERIES_DIR", tmp_path)
    return tmp_path


def test_cross_week_snapshots_filtered(series_dir):
    # 文件首行 match_time=本周(8/30), 但混入上周(8/22-23)的快照 → 上周点必须剔除
    _write_series(series_dir, "3632615.jsonl", [
        _snap("2026-08-22T23:35:00"),
        _snap("2026-08-23T06:15:00"),
        _snap("2026-08-29T10:00:00"),
        _snap("2026-08-30T04:43:00"),
    ])
    seq = osd.load_series("2026-08-30_周日002")
    assert [s["ts"][:10] for s in seq] == ["2026-08-29", "2026-08-30"]


def test_invalid_timestamp_dropped(series_dir):
    _write_series(series_dir, "3632616.jsonl", [
        _snap(""),
        _snap("not-a-time"),
        _snap("2026-08-30T04:43:00"),
    ])
    seq = osd.load_series("2026-08-30_周日002")
    assert len(seq) == 1 and seq[0]["ts"].startswith("2026-08-30")


def test_day_before_kickoff_boundary_kept(series_dir):
    # 凌晨场: 开赛在销售日+1, 快照落在 +1 当天也应保留
    _write_series(series_dir, "3632617.jsonl", [
        _snap("2026-08-30T10:00:00"),
        _snap("2026-08-31T01:00:00"),
    ])
    seq = osd.load_series("2026-08-30_周日002")
    assert len(seq) == 2


def test_exact_match_id_file_preferred(series_dir, tmp_path):
    _write_series(series_dir, "2026-08-30_周日002.jsonl", [
        _snap("2026-08-29T10:00:00"),
        _snap("2026-08-30T04:00:00"),
    ])
    seq = osd.load_series("2026-08-30_周日002")
    assert len(seq) == 2


def test_series_features_short_returns_points_only(series_dir):
    _write_series(series_dir, "3632618.jsonl", [_snap("2026-08-30T04:43:00")])
    feat = osd.series_features("2026-08-30_周日002")
    assert feat == {"points": 1}
