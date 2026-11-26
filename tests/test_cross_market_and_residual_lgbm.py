"""PR 4 回归测试：Pinnacle 亚盘+大小球反推、盘口位移、LGBM 残差学习与市场隔离验证。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.prediction.cross_market import (  # noqa: E402
    CROSS_MARKET_FEATURE_NAMES,
    _asian_cover_prob,
    _dc_score_matrix,
    _totals_over_prob,
    devig_two_way,
    extract_cross_market_features,
    implied_xg_from_ah_totals,
)
from engine.prediction.lgbm_model import (  # noqa: E402
    FEATURE_NAMES,
    HAS_LGBM,
    LGBMConfig,
    LGBMModel,
    build_features,
    probs_to_init_score,
)
from engine.prediction.market_isolation import REFERENCE_MARKET_FEATURES, audit_features  # noqa: E402


def test_implied_xg_low_vs_high_totals_draw_probability():
    # 平手盘 (AH=0.0) 下，2.0 球低赔小盘的平局概率应明显高于 3.5 球大球盘
    low_tot = implied_xg_from_ah_totals(
        ah_line=0.0, ah_home_odds=1.95, ah_away_odds=1.95,
        total_line=2.0, over_odds=1.95, under_odds=1.95,
    )
    high_tot = implied_xg_from_ah_totals(
        ah_line=0.0, ah_home_odds=1.95, ah_away_odds=1.95,
        total_line=3.5, over_odds=1.95, under_odds=1.95,
    )
    assert low_tot is not None and high_tot is not None
    assert high_tot["total_xg"] > low_tot["total_xg"] + 0.8
    assert low_tot["p_draw"] > high_tot["p_draw"] + 0.05


@pytest.mark.parametrize(
    "ah_line,ah_h,ah_a,tot_line,ov,un",
    [
        (-0.5, 1.85, 2.05, 2.5, 1.90, 2.00),
        (-0.75, 1.92, 1.98, 2.75, 1.88, 2.02),
        (-1.0, 2.10, 1.80, 3.0, 2.05, 1.85),
    ],
)
def test_implied_xg_round_trip_quarter_half_whole_lines(ah_line, ah_h, ah_a, tot_line, ov, un):
    inv = implied_xg_from_ah_totals(ah_line, ah_h, ah_a, tot_line, ov, un)
    assert inv is not None
    mat = _dc_score_matrix(inv["home_xg"], inv["away_xg"])
    ah_target = devig_two_way(ah_h, ah_a)[0]
    tot_target = devig_two_way(ov, un)[0]
    assert _asian_cover_prob(mat, ah_line) == pytest.approx(ah_target, abs=5e-3)
    assert _totals_over_prob(mat, tot_line) == pytest.approx(tot_target, abs=5e-3)


def test_extract_cross_market_features_and_market_isolation():
    om = {
        "1x2_fulltime": {
            "bookmaker": "Pinnacle",
            "current": {"home": 1.80, "draw": 3.60, "away": 4.50},
            "prob_shift": {"home": 0.032, "draw": -0.012, "away": -0.020},
        },
        "totals_main": {
            "line": 2.5,
            "current": {"over": 1.88, "under": 2.02},
            "prob_shift": {"over": 0.025},
        },
        "handicap": {
            "line": -0.75,
            "current": {"home": 1.95, "away": 1.95},
            "prob_shift": {"home": 0.018},
        },
    }
    cm = extract_cross_market_features(
        opening_movement=om,
        lineup_info={"home_starting_attackers": 3, "away_starting_attackers": 1},
    )
    assert cm["cross_market_missing"] == 0.0
    assert cm["pin_implied_home"] > cm["pin_implied_away"]
    assert cm["pin_total_xg"] > 2.0
    assert cm["pin_xg_diff"] > 0.3
    assert cm["pin_move_home"] == pytest.approx(0.032)
    assert cm["pin_totals_shift"] == pytest.approx(0.025)
    assert cm["pin_ah_shift"] == pytest.approx(0.018)
    assert cm["lineup_strength_diff"] == pytest.approx(2.0)

    # 市场隔离审计：新特征全部属于 reference，不触发任何 violation，且 target odds 不影响输出
    rep = audit_features(cm, model_name="lgbm_residual")
    assert rep.status == "PASS"
    assert set(CROSS_MARKET_FEATURE_NAMES) <= REFERENCE_MARKET_FEATURES
    assert set(CROSS_MARKET_FEATURE_NAMES) <= set(FEATURE_NAMES)
    f1 = build_features(elo_home=1550.0, elo_away=1480.0, odds=(1.15, 7.5, 16.0),
                        include_market_odds=False, cross_market=cm)
    f2 = build_features(elo_home=1550.0, elo_away=1480.0, odds=(3.20, 3.10, 2.30),
                        include_market_odds=False, cross_market=cm)
    assert f1 == f2


@pytest.mark.skipif(not HAS_LGBM, reason="lightgbm not installed")
def test_lgbm_residual_mode_init_score_and_predict(tmp_path: Path):
    rng = np.random.default_rng(123)
    n = 160
    n_cols = len(FEATURE_NAMES)
    X = rng.normal(0.0, 1.0, size=(n, n_cols))
    # 构造强参考先验，标签几乎全由先验决定 + 第 0 列轻微残差
    base = np.tile(np.array([[0.70, 0.20, 0.10]]), (n, 1))
    y = rng.choice([0, 1, 2], size=n, p=[0.70, 0.20, 0.10])

    cfg = LGBMConfig(
        n_estimators=15,
        max_depth=2,
        num_leaves=4,
        min_child_samples=10,
        min_train_samples=50,
        residual_mode=True,
    )
    model_path = tmp_path / "lgbm_res.txt"
    m = LGBMModel(model_path, config=cfg)
    m.train(
        X[:120], y[:120],
        eval_features=X[120:], eval_labels=y[120:],
        base_probs=base[:120], eval_base_probs=base[120:],
    )
    assert m.is_available is True

    # 重载后在 residual_mode 下推理应受 base_probs 引导（偏高主胜先验 vs 偏高客胜先验）
    reloaded = LGBMModel(model_path, config=cfg)
    f_home_pin = build_features(
        elo_home=1500.0, elo_away=1500.0,
        cross_market={
            "pin_implied_home": 0.75, "pin_implied_draw": 0.15, "pin_implied_away": 0.10,
            "cross_market_missing": 0.0,
        },
    )
    f_away_pin = build_features(
        elo_home=1500.0, elo_away=1500.0,
        cross_market={
            "pin_implied_home": 0.10, "pin_implied_draw": 0.15, "pin_implied_away": 0.75,
            "cross_market_missing": 0.0,
        },
    )
    p_h = reloaded.predict_single(f_home_pin)
    p_a = reloaded.predict_single(f_away_pin)
    assert p_h is not None and p_a is not None
    assert sum(p_h) == pytest.approx(1.0, abs=1e-6)
    assert p_h[0] > p_a[0] + 0.3
    assert p_a[2] > p_h[2] + 0.3

    init = probs_to_init_score(np.array([[0.5, 0.3, 0.2]]))
    assert init.mean() == pytest.approx(0.0, abs=1e-9)
