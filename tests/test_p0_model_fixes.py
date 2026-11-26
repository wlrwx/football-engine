"""PR 1 回归测试：Elo 攻防均值回归、MC 默认评级隔离、LGBM 缺失语义与三段切分。"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.learning.elo_updater import EloConfig, EloUpdater  # noqa: E402
from engine.learning.lgbm_shadow import shadow_train  # noqa: E402
from engine.prediction.base import TeamRating  # noqa: E402
from engine.prediction.lgbm_model import (  # noqa: E402
    FEATURE_NAMES,
    LGBMConfig,
    LGBMModel,
    build_features,
    feature_dict_to_vector,
)
from engine.prediction.monte_carlo import MonteCarloConfig, MonteCarloModel  # noqa: E402
from scripts.rebuild_team_ratings import rebuild_ratings  # noqa: E402


def test_elo_updater_attack_defense_mean_reverts_around_one(tmp_path: Path):
    """连续进行均势 1.35 球级比赛时，攻防均值应稳定在 ~1.0 而非塌缩到 ~0.487。"""
    updater = EloUpdater(tmp_path / "ratings.json", EloConfig())
    for idx in range(500):
        # 交替 1-1 与 2-1，场均 ~1.35 球
        hg = 1 if idx % 3 != 0 else 2
        ag = 1 if idx % 2 == 0 else 2
        updater.update("主队A", "客队B", hg, ag)
    ra = updater.get_rating("主队A")
    rb = updater.get_rating("客队B")
    assert 0.85 <= ra.attack <= 1.25
    assert 0.85 <= ra.defense <= 1.25
    assert 0.85 <= rb.attack <= 1.25
    assert 0.85 <= rb.defense <= 1.25


def test_elo_updater_supports_league_baseline_and_xg_blend(tmp_path: Path):
    u_score = EloUpdater(tmp_path / "r1.json", EloConfig())
    u_xg = EloUpdater(tmp_path / "r2.json", EloConfig())
    # 0-0 但实际 xG 很高 (2.4 vs 0.2)
    u_score.update("主队", "客队", 0, 0, league_avg_goals=3.0)
    u_xg.update("主队", "客队", 0, 0, league_avg_goals=3.0,
                home_xg=2.4, away_xg=0.2, xg_blend_weight=0.65)
    assert u_xg.get_rating("主队").attack > u_score.get_rating("主队").attack
    assert u_xg.get_rating("客队").defense > u_score.get_rating("客队").defense


def test_rebuild_team_ratings_dry_run_and_apply(tmp_path: Path):
    db_path = tmp_path / "match.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE match_history (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, "
        "league TEXT, home_team TEXT, away_team TEXT, score_home INTEGER, score_away INTEGER, "
        "actual_home_xg REAL, actual_away_xg REAL)"
    )
    conn.execute("CREATE TABLE league_baselines (league_name TEXT PRIMARY KEY, avg_goals REAL)")
    conn.execute("INSERT INTO league_baselines VALUES ('英超', 2.8)")
    for i in range(40):
        conn.execute(
            "INSERT INTO match_history (date, league, home_team, away_team, score_home, score_away, "
            "actual_home_xg, actual_away_xg) VALUES (?, '英超', '阿森纳', '切尔西', ?, ?, 1.5, 1.3)",
            (f"2026-09-{(i % 28) + 1:02d}", 2 if i % 2 == 0 else 1, 1),
        )
    conn.commit()
    conn.close()

    out = tmp_path / "team_ratings.json"
    out.write_text(json.dumps({"阿森纳": {"attack": 0.45, "defense": 0.50}}), encoding="utf-8")

    dry = rebuild_ratings(db_path, out, apply=False)
    assert dry["applied"] is False
    assert dry["matches_replayed"] == 40
    assert dry["after"]["mean_attack"] > 0.9
    # dry-run 不修改原文件
    assert json.loads(out.read_text(encoding="utf-8"))["阿森纳"]["attack"] == 0.45

    applied = rebuild_ratings(db_path, out, apply=True)
    assert applied["applied"] is True
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["阿森纳"]["attack"] > 0.9


def test_monte_carlo_blocks_odds_fallback_by_default():
    default_h = TeamRating(name="新主队")
    default_a = TeamRating(name="新客队")
    mc_off = MonteCarloModel(MonteCarloConfig(simulations=2000, allow_odds_xg_fallback=False))
    base = mc_off.predict(default_h, default_a, market_odds=None)
    with_odds = mc_off.predict(default_h, default_a, market_odds=(1.25, 5.5, 11.0))
    assert (with_odds.home_win_prob, with_odds.draw_prob, with_odds.away_win_prob) == (
        base.home_win_prob, base.draw_prob, base.away_win_prob
    )

    mc_on = MonteCarloModel(MonteCarloConfig(simulations=2000, allow_odds_xg_fallback=True))
    opted = mc_on.predict(default_h, default_a, market_odds=(1.25, 5.5, 11.0))
    assert opted.home_win_prob > base.home_win_prob + 0.1


def test_lgbm_missing_indicators_and_nan_vector():
    # dict 本身保持纯 float（无 NaN），避免破坏相等性与 JSON 落盘
    f_missing = build_features(elo_home=1500.0, elo_away=1500.0, handicap=None)
    assert f_missing["handicap"] == 0.0
    assert f_missing["handicap_missing"] == 1.0
    assert f_missing["xg_missing"] == 1.0
    assert f_missing["djyy_missing"] == 1.0
    assert f_missing["rank_missing"] == 1.0
    assert f_missing == build_features(elo_home=1500.0, elo_away=1500.0, handicap=None)

    vec = feature_dict_to_vector(f_missing, FEATURE_NAMES, missing_as_nan=True)
    idx_hdp = FEATURE_NAMES.index("handicap")
    idx_xg = FEATURE_NAMES.index("xg_home")
    assert math.isnan(vec[idx_hdp])
    assert math.isnan(vec[idx_xg])

    # 显式传 0.0 让球（平手盘）时不应被当作缺失
    f_level = build_features(
        elo_home=1500.0, elo_away=1500.0, handicap=0.0,
        xg_home=1.3, xg_away=1.1, rank_home=2, rank_away=5,
        djyy_probs={"home": 0.45, "draw": 0.30, "away": 0.25},
    )
    assert f_level["handicap_missing"] == 0.0
    vec_level = feature_dict_to_vector(f_level, FEATURE_NAMES, missing_as_nan=True)
    assert vec_level[idx_hdp] == 0.0
    assert vec_level[idx_xg] == pytest.approx(1.3)


def test_lgbm_shadow_three_way_split_prevents_holdout_early_stop_leak(tmp_path: Path):
    """验证 early stopping 验证集严格来自 holdout 之前的 pre_hold 段，不与 holdout 重叠。"""
    recs = []
    for d_idx in range(25):
        date = f"2026-09-{d_idx + 1:02d}"
        dd = tmp_path / "daily" / date
        dd.mkdir(parents=True)
        preds = []
        for m in range(16):
            mid = f"{date}_M{m:03d}"
            # 用 elo_home 唯一编码全局序号，验证 eval_features 与 holdout 完全不交叠
            seq = d_idx * 16 + m
            preds.append({
                "match_id": mid,
                "elo_home": 1000.0 + seq,
                "elo_away": 1500.0,
                "handicap": 0.0,
                "home_xg": 1.4,
                "away_xg": 1.1,
            })
            recs.append({
                "match_id": mid,
                "date": date,
                "actual_idx": seq % 3,
                "final_prob": [0.4, 0.3, 0.3],
                "market_fair": [0.4, 0.3, 0.3],
                "chain": "v2",
            })
        (dd / "predictions.json").write_text(json.dumps(preds), encoding="utf-8")

    calls: list[dict] = []

    class _SpyTrainer:
        def __init__(self):
            self.best_iteration_ = 42

        def train(self, X, y, eval_features=None, eval_labels=None, n_estimators_override=None):
            calls.append({
                "n_train": len(X),
                "eval_elos": set(eval_features[:, 1].tolist()) if eval_features is not None else set(),
                "n_estimators_override": n_estimators_override,
            })

        def predict_single(self, feats):
            return (0.34, 0.33, 0.33)

        def save(self):
            pass

    spy = _SpyTrainer()
    status = shadow_train(
        recs, recs, tmp_path / "daily", tmp_path / "model.txt",
        lgbm_cfg=LGBMConfig(min_train_samples=10),
        trainer=spy,
        config={"min_train_samples": 300, "holdout_frac": 0.3, "early_stop_val_frac": 0.2},
    )
    assert status["trained"] is True
    assert len(calls) == 2
    # 400 条中 holdout = 后 120 条 (seq 280..399, elo 1280..1399)
    holdout_elos = {1000.0 + s for s in range(280, 400)}
    assert calls[0]["eval_elos"], "pre_hold ≥ 100 时应启用内部验证集做 early stopping"
    assert calls[0]["eval_elos"].isdisjoint(holdout_elos), "early stopping 验证集不得与 holdout 重叠"
    assert calls[1]["n_train"] == 400
    assert calls[1]["n_estimators_override"] == 42
    # LGBMModel 默认配置开启 missing_as_nan
    assert LGBMConfig().missing_as_nan is True
    assert isinstance(np.nan, float)
    assert LGBMModel(tmp_path / "missing.txt").is_available is False
