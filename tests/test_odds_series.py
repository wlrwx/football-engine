"""水位序列跨周污染过滤测试（2026-08-30）

load_series 的快照窗口过滤: 快照捕获日期必须落在 [销售日-3天, 销售日+1天]，
上周同编号场次的旧快照与无效时间戳快照一律剔除。
"""

from __future__ import annotations

import json

import pytest

import engine.odds_series as osd


def _snap(ts, match_no="周日002", match_time="2026-08-30", euro=(2.0, 3.2, 3.5)):
    return {"ts": ts, "match_no": match_no, "match_time": match_time,
            "euro": {"home": euro[0], "draw": euro[1], "away": euro[2]}}


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


def test_union_merged_lines_are_read_in_time_order(series_dir):
    """并发追加 + git `merge=union` 后，落盘顺序可能时间倒挂（2026-11-28 ci-sync #201）。

    series_features 取的是 seq[0]/[-1]/[-2]，倒挂会把 overall/recent 算反、
    span_min 甚至算成负数。读侧按 ts 归一化后，方向必须等于"按真实时间追加"的那份序列。
    """
    _write_series(series_dir, "3632619.jsonl", [
        _snap("2026-08-29T10:00:00", euro=(2.00, 3.2, 3.5)),   # 首
        _snap("2026-08-30T14:00:00", euro=(2.20, 3.1, 3.4)),   # 末（真实最新）
        _snap("2026-08-30T12:00:00", euro=(2.10, 3.15, 3.45)),  # 上游先推、其实写得更早
    ])
    seq = osd.load_series("2026-08-30_周日002")
    assert [s["ts"] for s in seq] == ["2026-08-29T10:00:00", "2026-08-30T12:00:00",
                                      "2026-08-30T14:00:00"], "读侧必须给出时间正序"

    feat = osd.series_features("2026-08-30_周日002")
    assert feat["points"] == 3
    assert feat["overall_home"] == round((2.20 - 2.00) / 2.00 * 100, 2)   # +10.0
    assert feat["recent_home"] == round((2.20 - 2.10) / 2.10 * 100, 2)    # +4.76
    assert feat["span_min"] == 1680.0  # 8/29 10:00 → 8/30 14:00，28 小时
