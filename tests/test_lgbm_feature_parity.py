"""LGBM 特征接线与训练/服务对齐（2026-10-22）。

修的是同一类病：数据在、旋钮在，模型却永远读到默认值／两端读法不同。
  1. TeamRating.form 一直在更新，但 build_features 的 home_form/away_form
     从未收到过 → home_form_pts/away_form_pts/form_diff 恒为 0
  2. data/league_matrix.json 33 个联赛，但 league_avg_goals /
     league_home_win_rate 从未传 → 恒为 2.5 / 0.45
  3. 服务端用 fixture._xg_home（DJYY，403 时为空），训练端用落盘的
     home_xg（模型 xG）→ 同一特征两端不同源
  4. 排名只在服务端接线，训练端没传 → 训练 rank_diff 恒 0、服务端非 0

修法：服务端取一次输入快照随预测落盘（lgbm_inputs），训练端优先回放
这份冻结值，旧样本才退回历史列/默认值——不做「用今天的联赛表回填
历史」这种事后诸葛。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.learning.lgbm_shadow import build_training_rows  # noqa: E402
from engine.prediction.league_stats import (  # noqa: E402
    DEFAULT_AVG_GOALS,
    DEFAULT_HOME_WIN_RATE,
    league_stats,
)


def test_league_stats_known_league_matches_matrix():
    raw = json.loads((ROOT / "data" / "league_matrix.json").read_text(encoding="utf-8"))
    lg = next(x for x in raw["leagues"] if x["name_zh"] == "巴西甲")
    assert league_stats("巴甲") == (lg["avg_goals"], lg["home_win_pct"] / 100.0)
    # 归一化后同名
    assert league_stats("巴西甲") == league_stats("巴甲")


def test_league_stats_unknown_and_missing_file_fall_back():
    assert league_stats("不存在的联赛") == (DEFAULT_AVG_GOALS, DEFAULT_HOME_WIN_RATE)
    assert league_stats(None) == (DEFAULT_AVG_GOALS, DEFAULT_HOME_WIN_RATE)
    missing = Path("/nonexistent/league_matrix.json")
    assert league_stats("英超", path=missing) == (DEFAULT_AVG_GOALS, DEFAULT_HOME_WIN_RATE)


def test_league_matrix_rows_without_home_win_fallback_default_rate():
    # 日职联的 home_win_pct 为 null，不能当 0% 用（那会把主胜率压到地板），保留真实 avg_goals 并回退默认主胜率
    goals, hw = league_stats("日职联")
    assert goals == pytest.approx(3.6)
    assert hw == DEFAULT_HOME_WIN_RATE
    assert hw > 0.0


def test_main_passes_and_persists_frozen_inputs():
    src = (ROOT / "engine" / "main.py").read_text(encoding="utf-8")
    assert "league_stats(fixture.competition)" in src
    assert '"lgbm_inputs": _lgbm_inputs' in src
    for kw, key in (("home_form", "form_home"), ("away_form", "form_away"),
                    ("league_avg_goals", "league_avg_goals"),
                    ("league_home_win_rate", "league_home_win_rate"),
                    ("rank_home", "rank_home"), ("rank_away", "rank_away"),
                    ("xg_home", "xg_home")):
        assert f'{kw}=_lgbm_inputs["{key}"]' in src, f"main.py 没把 {key} 传进 build_features"
    # 服务端 xG 必须与落盘同源（模型 xG），不能再取 fixture 的外部 xG
    assert "getattr(fixture, \"_xg_home\", None)" not in src
    assert "getattr(fixture, \"_xg_away\", None)" not in src


def _write_daily(tmp_path: Path, pred: dict, mid: str = "m1",
                 date: str = "2026-10-20") -> list[dict]:
    dd = tmp_path / "daily" / date
    dd.mkdir(parents=True)
    pred = {"match_id": mid, **pred}
    (dd / "predictions.json").write_text(json.dumps([pred], ensure_ascii=False),
                                         encoding="utf-8")
    return [{"match_id": mid, "date": date, "actual_idx": 0,
             "final_prob": [0.4, 0.3, 0.3], "market_fair": [0.42, 0.28, 0.30]}]


def test_training_replays_frozen_inputs_over_legacy_columns(tmp_path):
    recs = _write_daily(tmp_path, {
        "elo_home": 1500, "elo_away": 1500,
        "home_xg": 1.9, "away_xg": 0.8,          # 落盘的（可能被 SSR 覆盖）
        "form_home": 0.5, "form_away": -0.5,     # 落盘历史列
        "lgbm_inputs": {
            "form_home": -0.1, "form_away": 0.6,
            "league_avg_goals": 3.1, "league_home_win_rate": 0.52,
            "rank_home": 2, "rank_away": 9,
            "xg_home": 1.2, "xg_away": 1.4,      # 服务端实际喂给 LGBM 的模型 xG
        },
    })
    f = build_training_rows(recs, tmp_path / "daily")[0]["features"]
    assert f["home_form_pts"] == -0.1 and f["away_form_pts"] == 0.6
    assert f["league_avg_goals"] == 3.1
    assert f["league_home_win_rate"] == 0.52
    assert f["rank_diff"] == 7.0
    assert f["xg_home"] == 1.2 and f["xg_away"] == 1.4
    assert f["xg_diff"] == pytest.approx(-0.2)


def test_training_legacy_rows_use_historical_columns_not_today_state(tmp_path):
    recs = _write_daily(tmp_path, {
        "elo_home": 1500, "elo_away": 1500,
        "home_xg": 1.9, "away_xg": 0.8,
        "form_home": 0.5, "form_away": -0.5,
    })
    f = build_training_rows(recs, tmp_path / "daily")[0]["features"]
    assert f["home_form_pts"] == 0.5 and f["away_form_pts"] == -0.5
    assert f["xg_home"] == 1.9 and f["xg_away"] == 0.8
    # 旧样本无联赛先验/排名可追溯 → 保持默认值，不用今天的表回填
    assert f["league_avg_goals"] == DEFAULT_AVG_GOALS
    assert f["league_home_win_rate"] == DEFAULT_HOME_WIN_RATE
    assert f["rank_diff"] == 0.0
