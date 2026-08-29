"""流水线纯工具回归测试（engine.pipeline.helpers / sina_odds）。

锁定 2026-08-14 重构：从 main.py 抽出的纯函数行为不变（名字、边界、容错）。
"""
from __future__ import annotations

import json


from engine.pipeline.helpers import (
    _canon_league,
    _extract_features,
    _odds_band,
    _pick_direction,
    _prob_band,
    load_config,
)
from engine.pipeline.sina_odds import load_sina_odds_map


# ---------- helpers ----------

def test_load_config_reads_repo_config():
    cfg = load_config("prediction")
    assert cfg.get("prediction", {}).get("base_goals") == 1.35
    assert load_config("nonexistent_config_xyz") == {}


def test_canon_league_aliases():
    assert _canon_league("瑞超") == "瑞典超"
    assert _canon_league("韩职") == "K1联赛"
    assert _canon_league("英超") == "英超"
    assert _canon_league(None) == ""
    assert _canon_league("") == ""


def test_pick_direction_argmax():
    assert _pick_direction(0.4, 0.35, 0.25) == "home"
    assert _pick_direction(0.25, 0.4, 0.35) == "draw"
    assert _pick_direction(0.25, 0.35, 0.4) == "away"


def test_pick_direction_pure_argmax_alert_display_only():
    # 2026-08-17 停用 R1（league_draw）改判：实盘证伪，8 场改判 0 中、
    # 5 场把正确 argmax 改错（见 engine/pipeline/helpers.py 文档）。
    # draw_alert 仅作页面展示标记，方向一律纯 argmax
    assert _pick_direction(0.45, 0.3, 0.25, draw_alert="league_draw") == "home"
    assert _pick_direction(0.45, 0.3, 0.25, draw_alert="balanced_draw") == "home"


def test_prob_band_boundaries():
    assert _prob_band(0.65) == "high"
    assert _prob_band(0.64) == "mid"
    assert _prob_band(0.45) == "mid"
    assert _prob_band(0.44) == "low"


def test_odds_band_boundaries():
    assert _odds_band(1.49) == "1.0-1.5"
    assert _odds_band(1.5) == "1.5-2.0"
    assert _odds_band(2.0) == "2.0-3.0"
    assert _odds_band(3.0) == "3.0-5.0"
    assert _odds_band(5.0) == "5.0+"


def test_extract_features_duck_typed():
    class F:
        competition = "英超"
        home_odds = 2.1
        handicap = 0.5

    class P:
        home_win_prob = 0.5
        draw_prob = 0.3
        away_win_prob = 0.2

    f = _extract_features(F(), P())
    assert f["league"] == "英超"
    assert f["prob_band"] == "mid"   # max=0.5
    assert f["odds_band"] == "2.0-3.0"
    assert f["handicap"] == "0.5"


# ---------- sina_odds ----------

def test_load_sina_odds_map(tmp_path):
    day = tmp_path / "data" / "daily" / "2026-08-14"
    day.mkdir(parents=True)
    (day / "odds_sina.json").write_text(json.dumps([
        {"match_no": "周五001", "home_team": "A", "away_team": "B"},
        {"match_no": "", "home_team": "C", "away_team": "D"},
    ]), encoding="utf-8")

    m, by_no, n = load_sina_odds_map("2026-08-14", root=tmp_path)
    assert n == 2
    assert m[("A", "B")]["match_no"] == "周五001"
    assert by_no == {"周五001": m[("A", "B")]}
    # 无编号的场次只进队名索引
    assert ("C", "D") in m


def test_load_sina_odds_map_missing_file(tmp_path):
    m, by_no, n = load_sina_odds_map("2099-01-01", root=tmp_path)
    assert m == {} and by_no == {} and n == 0


def test_load_sina_odds_map_bad_json(tmp_path):
    day = tmp_path / "data" / "daily" / "2026-08-14"
    day.mkdir(parents=True)
    (day / "odds_sina.json").write_text("{broken json", encoding="utf-8")
    m, by_no, n = load_sina_odds_map("2026-08-14", root=tmp_path)
    assert m == {} and by_no == {} and n == 0
