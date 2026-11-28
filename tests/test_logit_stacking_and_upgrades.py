"""PR 3 回归测试：Logit Stacking、共线性感知融合优化器、分联赛/xG ShrinkageDC、温度自动校准。"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.learning.calibration_auto import decide_calibration  # noqa: E402
from engine.learning.fusion_optimizer import FusionOptimizer, FusionWeights  # noqa: E402
from engine.prediction.fusion import DEFAULT_POST_FUSION, FusionInput, fuse_probabilities  # noqa: E402
from engine.prediction.logit_stacking import LogitStacker, logit_pool  # noqa: E402
from engine.prediction.shrinkage_dc import (  # noqa: E402
    LeagueShrinkageDCModel,
    ShrinkageDCConfig,
    fit_league_shrinkage_dc,
    fit_shrinkage_dc,
    load_model,
    save_model,
)
from engine.review.post_match import ReviewLedger  # noqa: E402


def test_logit_pool_preserves_extremes_and_handles_stacker_missing_source(tmp_path: Path):
    # 极端概率下 log-odds 池化保持锐度，且当模型和市场同向强看好主胜时比线性平均更坚决
    p_lin = 0.2 * 0.85 + 0.8 * 0.80
    p_log = logit_pool([(0.85, 0.10, 0.05), (0.80, 0.12, 0.08)], [0.2, 0.8])
    assert sum(p_log) == pytest.approx(1.0, abs=1e-9)
    assert p_log[0] > p_lin

    rng = np.random.default_rng(11)
    sources = []
    actuals = []
    for _ in range(250):
        # market 为真概率源，model 为高噪声源
        mh = float(rng.uniform(0.2, 0.7))
        md = 0.25
        ma = 1.0 - mh - md
        mkt = (mh, md, ma)
        mod = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
        y = int(rng.choice([0, 1, 2], p=mkt))
        sources.append({"model": mod, "market": mkt, "djyy": None})
        actuals.append(y)

    stacker = LogitStacker(l2_reg=0.5).fit(sources, actuals)
    assert stacker.fitted is True
    assert stacker.weights[1] > stacker.weights[0]

    out_path = tmp_path / "stacker.json"
    stacker.save(out_path)
    loaded = LogitStacker.load(out_path)
    assert loaded.predict_probs((0.4, 0.3, 0.3), market_probs=None) == pytest.approx(
        stacker.predict_probs((0.4, 0.3, 0.3), market_probs=None)
    )


def test_fusion_logit_mode_vs_linear_default():
    off = {k: False for k in DEFAULT_POST_FUSION}
    lin = fuse_probabilities(FusionInput(
        model_probs=(0.60, 0.25, 0.15),
        market_probs=(0.80, 0.12, 0.08),
        djyy_probs={"home": 0.75, "draw": 0.15, "away": 0.10},
        cfg={"model_weight": 0.1, "market_weight": 0.7, "djyy_weight": 0.2, "fusion_mode": "linear"},
        post_fusion=off,
    ))
    log = fuse_probabilities(FusionInput(
        model_probs=(0.60, 0.25, 0.15),
        market_probs=(0.80, 0.12, 0.08),
        djyy_probs={"home": 0.75, "draw": 0.15, "away": 0.10},
        cfg={"model_weight": 0.1, "market_weight": 0.7, "djyy_weight": 0.2, "fusion_mode": "logit"},
        post_fusion=off,
    ))
    assert sum(log.probs) == pytest.approx(1.0, abs=1e-9)
    assert log.probs != pytest.approx(lin.probs, abs=1e-4)
    assert log.trace[-1].get("mode") == "logit"


def test_fusion_optimizer_logit_nll_and_cooldown(tmp_path: Path):
    ledger_path = tmp_path / "ledger.jsonl"
    ledger = ReviewLedger(ledger_path)
    rng = random.Random(77)
    records = []
    for i in range(120):
        mh = 0.55 if i % 2 == 0 else 0.25
        md = 0.25
        ma = 1.0 - mh - md
        y = 0 if rng.random() < mh else (1 if rng.random() < md / (1.0 - mh) else 2)
        records.append({
            "match_id": f"m_{i}",
            "date": f"2026-10-{(i % 28) + 1:02d}",
            "actual_idx": y,
            "model_raw": [0.34, 0.33, 0.33],
            "market_fair": [mh, md, ma],
            "djyy_prob": [0.34, 0.33, 0.33],
            "brier_model": 0.68,
            "brier_market": 0.55,
            "brier_djyy": 0.68,
        })
    import json
    ledger_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    ledger = ReviewLedger(ledger_path)

    opt = FusionOptimizer(
        tmp_path / "fusion_weights.json",
        ledger,
        config={"method": "logit_nll", "fusion_mode": "logit", "min_new_matches": 10, "val_matches": 30, "min_samples": 30},
    )
    train_reviews, _ = ledger.split_train_val(30)
    prop = opt._propose(train_reviews, FusionWeights(0.3, 0.4, 0.3))
    assert prop.market > prop.model

    d1 = opt.step()
    assert d1.action in ("promote", "hold")
    # 立即再跑一次：新增场次 0 < min_new_matches=10 → 触发冷却跳过重叠检验
    d2 = opt.step()
    assert d2.action == "hold"
    assert "cooldown_min_new_matches" in d2.guard_rails_applied


def test_fusion_optimizer_rejects_logit_objective_for_linear_production(tmp_path: Path):
    ledger = ReviewLedger(tmp_path / "ledger.jsonl")
    with pytest.raises(ValueError, match="必须与 fusion_mode=logit 配套"):
        FusionOptimizer(
            tmp_path / "fusion_weights.json",
            ledger,
            config={"method": "logit_nll", "fusion_mode": "linear"},
        )


def test_shrinkage_dc_xg_blend_and_league_hierarchy(tmp_path: Path):
    class _Row:
        def __init__(self, h, a, hs, as_, d, comp, hxg, axg):
            self.home_team, self.away_team = h, a
            self.home_score, self.away_score = hs, as_
            self.match_date = d
            self.competition = comp
            self.home_xg, self.away_xg = hxg, axg

    rng = random.Random(9)
    teams_a = [f"A{i}" for i in range(6)]
    teams_b = [f"B{i}" for i in range(6)]
    rows = []
    # 联赛 A 低进球（~1.6 球/场），联赛 B 高进球（~3.4 球/场）
    for idx in range(120):
        ha, aa = rng.sample(teams_a, 2)
        rows.append(_Row(ha, aa, 1 if idx % 3 == 0 else 0, 1 if idx % 4 == 0 else 0,
                         f"2026-09-{(idx % 28) + 1:02d}", "低球联", 0.8, 0.7))
        hb, ab = rng.sample(teams_b, 2)
        rows.append(_Row(hb, ab, 2, 2 if idx % 2 == 0 else 1,
                         f"2026-09-{(idx % 28) + 1:02d}", "高球联", 2.0, 1.5))

    m_score = fit_shrinkage_dc(rows, ShrinkageDCConfig(xg_blend_weight=0.0, max_iter=80))
    m_xg = fit_shrinkage_dc(rows, ShrinkageDCConfig(xg_blend_weight=0.65, max_iter=80))
    assert math.isfinite(m_score.rho) and math.isfinite(m_xg.rho)

    l_model = fit_league_shrinkage_dc(rows, ShrinkageDCConfig(xg_blend_weight=0.5, max_iter=80), league_prior_matches=20.0)
    xg_low = sum(l_model.expected_goals("A0", "A1", competition="低球联"))
    xg_high = sum(l_model.expected_goals("B0", "B1", competition="高球联"))
    assert xg_high > xg_low + 0.5

    model_path = tmp_path / "shrinkage_dc.json"
    save_model(l_model, model_path)
    reloaded = load_model(model_path)
    assert isinstance(reloaded, LeagueShrinkageDCModel)
    assert reloaded.predict_probs("A0", "A1", competition="低球联") == pytest.approx(
        l_model.predict_probs("A0", "A1", competition="低球联")
    )


def test_calibration_auto_temperature_mode():
    rng = random.Random(21)
    records = []
    for i in range(250):
        qh = 0.35 + 0.30 * rng.random()
        qd = 0.25
        qa = 1.0 - qh - qd
        u = rng.random()
        y = 0 if u < qh else (1 if u < qh + qd else 2)
        # 故意构造过自信概率：p = normalize(q^3)
        raw = [qh ** 3.0, qd ** 3.0, qa ** 3.0]
        s = sum(raw)
        records.append({
            "match_id": f"c_{i}",
            "date": f"2026-10-{1 + i // 15:02d}",
            "final_prob": [x / s for x in raw],
            "actual_idx": y,
        })
    status = decide_calibration(
        records,
        current_enabled=False,
        config={"method": "temperature", "min_samples": 100, "val_frac": 0.4, "min_improvement": 0.002, "min_z": 1.96},
    )
    assert status["method"] == "temperature"
    assert status["enabled"] is True
    assert status["val_delta_brier"] > 0.002
