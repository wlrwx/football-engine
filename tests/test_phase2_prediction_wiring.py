"""Phase 2 预测接线与数据质量修复回归测试。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.prediction.league_stats import DEFAULT_AVG_GOALS, DEFAULT_HOME_WIN_RATE, league_stats  # noqa: E402
from engine.prediction.shrinkage_dc import (  # noqa: E402
    LeagueShrinkageDCModel,
    ShrinkageDCConfig,
    fit_league_shrinkage_dc,
    load_model,
    save_model,
)
from engine.sources import njstats_side as side_mod  # noqa: E402
from engine.sources.base import MatchResult  # noqa: E402
from scripts import ablation_replay as abl  # noqa: E402
from scripts.rebuild_team_ratings import rebuild_ratings  # noqa: E402


def test_league_stats_alias_chain_and_missing_home_win_pct(tmp_path: Path):
    matrix = tmp_path / "league_matrix.json"
    matrix.write_text(json.dumps({
        "leagues": [
            {"name_zh": "韩K联", "short_zh": "韩K", "avg_goals": 2.34, "home_win_pct": 26.7},
            {"name_zh": "日职联", "short_zh": "日职", "avg_goals": 3.60},  # 无 home_win_pct
            {"name_zh": "沙特联", "short_zh": "沙特", "avg_goals": 3.01, "home_win_pct": 44.1},
            {"name_zh": "丹超", "short_zh": "丹超", "avg_goals": 2.25, "home_win_pct": 50.0},
            {"name_zh": "瑞超", "short_zh": "瑞超", "avg_goals": 3.83, "home_win_pct": 50.0},
            {"name_zh": "瑞典超", "short_zh": "瑞典", "avg_goals": 3.08, "home_win_pct": 40.8},
        ]
    }, ensure_ascii=False), encoding="utf-8")

    assert league_stats("韩职", path=matrix) == pytest.approx((2.34, 0.267))
    assert league_stats("K1联赛", path=matrix) == pytest.approx((2.34, 0.267))
    assert league_stats("日职", path=matrix) == pytest.approx((3.60, DEFAULT_HOME_WIN_RATE))
    assert league_stats("沙职", path=matrix) == pytest.approx((3.01, 0.441))
    assert league_stats("丹麦超", path=matrix) == pytest.approx((2.25, 0.50))
    assert league_stats("瑞士超", path=matrix) == pytest.approx((3.83, 0.50))
    assert league_stats("瑞超", path=matrix) == pytest.approx((3.08, 0.408))
    assert league_stats("未知杯赛", path=matrix) == (DEFAULT_AVG_GOALS, DEFAULT_HOME_WIN_RATE)


def test_njstats_side_parse_ts_supports_iso8601_and_tz():
    dt_plain = side_mod._parse_ts("2026-10-06 17:26:39")
    dt_iso_bj = side_mod._parse_ts("2026-10-06T17:26:39+08:00")
    dt_iso_utc = side_mod._parse_ts("2026-10-06T09:26:39Z")
    assert dt_plain is not None
    assert dt_iso_bj == dt_plain
    assert dt_iso_utc == dt_plain
    assert len(side_mod.key("甲", "乙")) == 2

    sig = side_mod.SideSignal(
        home="A", away="B", kickoff="2026-10-07 00:00",
        snapshot_at="2026-10-06 18:00:00",
        open_fair=(0.5, 0.3, 0.2), home_ppg_home=2.0, away_ppg_away=1.0,
    )
    # 预测时刻早于快照 → 阻断；晚于快照 → 放行
    assert sig.features_as_of("2026-10-06T17:00:00+08:00") is None
    assert sig.features_as_of("2026-10-06T19:00:00+08:00") is not None


def test_league_shrinkage_dc_properties_and_roundtrip(tmp_path: Path):
    matches = []
    for i in range(40):
        h, a = ("A", "B") if i % 2 == 0 else ("C", "D")
        matches.append(MatchResult(
            match_id=f"m{i}",
            match_date=f"2026-09-{(i % 28) + 1:02d}",
            home_team=h, away_team=a,
            home_score=2 if i % 3 == 0 else 1,
            away_score=1,
            competition="英超",
        ))
    model = fit_league_shrinkage_dc(matches, ShrinkageDCConfig(min_matches=10))
    assert model.n_matches == 40
    assert isinstance(model.rho, float)
    assert model.fitted_at == "2026-09-28"
    out = tmp_path / "sdc.json"
    save_model(model, out)
    loaded = load_model(out)
    assert isinstance(loaded, LeagueShrinkageDCModel)
    assert loaded.n_matches == 40
    assert loaded.predict_probs("A", "B", competition="英超") == pytest.approx(
        model.predict_probs("A", "B", competition="英超")
    )


def test_rebuild_team_ratings_merges_csv_daily_and_db_deduplicated(tmp_path: Path):
    db_path = tmp_path / "match.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE match_history (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, "
        "league TEXT, home_team TEXT, away_team TEXT, score_home INTEGER, score_away INTEGER, "
        "actual_home_xg REAL, actual_away_xg REAL)"
    )
    conn.execute(
        "INSERT INTO match_history (date, league, home_team, away_team, score_home, score_away, "
        "actual_home_xg, actual_away_xg) VALUES ('2026-10-01', '英超', '阿森纳', '切尔西', 2, 1, 1.8, 0.9)"
    )
    conn.commit()
    conn.close()

    csv_path = tmp_path / "matches.csv"
    csv_path.write_text(
        "date,competition,home_team,away_team,home_score,away_score\n"
        "2026-09-01,西甲,皇马,巴萨,2,2\n"
        "2026-10-01,英超,阿森纳,切尔西,2,1\n",
        encoding="utf-8",
    )

    daily_root = tmp_path / "daily"
    day_dir = daily_root / "2026-10-05"
    day_dir.mkdir(parents=True)
    (day_dir / "predictions.json").write_text(
        json.dumps([{"match_id": "2026-10-05_周一001", "competition": "德甲", "home_team": "拜仁", "away_team": "多特"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (day_dir / "results.json").write_text(
        json.dumps([{"match_id": "2026-10-05_周一001", "home_team": "拜仁", "away_team": "多特", "home_score": 3, "away_score": 1}], ensure_ascii=False),
        encoding="utf-8",
    )

    out = tmp_path / "team_ratings.json"
    summary = rebuild_ratings(
        db_path, out, apply=True, historical_csv=csv_path, daily_root=daily_root,
    )
    # 2026-10-01 在 csv 和 db 重复，必须去重为 3 场、6 支球队
    assert summary["matches_replayed"] == 3
    assert summary["after"]["count"] == 6
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert {"阿森纳", "切尔西", "皇家马德里", "巴塞罗那", "拜仁慕尼黑", "多特蒙德"} <= set(saved)


def test_ablation_replay_oos_logit_and_temperature_helpers():
    recs = [
        {
            "match_id": f"m{i}",
            "date": f"2026-10-{(i % 28) + 1:02d}",
            "model_raw": [0.55, 0.25, 0.20] if i % 3 == 0 else [0.30, 0.30, 0.40],
            "market_fair": [0.60, 0.22, 0.18] if i % 3 == 0 else [0.25, 0.28, 0.47],
            "final_prob": [0.58, 0.24, 0.18] if i % 3 == 0 else [0.27, 0.28, 0.45],
            "actual_idx": 0 if i % 3 == 0 else 2,
            "league": "英超",
        }
        for i in range(80)
    ]
    cfg = {"model_weight": 0.05, "market_weight": 0.75, "djyy_weight": 0.20}
    sw = {k: False for k in abl.DEFAULT_POST_FUSION}
    fm = abl.evaluate_fusion_modes(recs, {}, cfg, sw, recs[48:])
    assert fm["full"]["n"] == 80 and fm["val"]["n"] == 32
    assert "delta_brier" in fm["val"] and "delta_logloss" in fm["val"]

    ts = abl.evaluate_temperature_scaling(recs[:48], recs[48:])
    assert ts["fitted"] is True and ts["n_val"] == 32
    assert "temperature" in ts and "delta_brier" in ts
