"""PR 2 回归测试：ECE 类均值修复、滚动窗口评估、ParamOptimizer 概率目标、Kelly 诊断字段。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.backtest.walk_forward import WalkForwardEvaluator  # noqa: E402
from engine.learning.param_optimizer import ParamOptimizer  # noqa: E402
from engine.prediction.temperature_scaling import TemperatureScaler  # noqa: E402
from engine.strategy.kelly import KellyStrategy  # noqa: E402


def test_compute_ece_classwise_average_and_upper_bound():
    # 极端错误预测：每条都给错类概率 1.0 → 3 类平均 ECE 应在 [0, 1] 且为 2/3
    probs = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    actuals = np.array([1, 2, 0])
    ece_wf = WalkForwardEvaluator._compute_ece(probs, actuals, n_bins=10)
    ece_ts = TemperatureScaler._compute_ece(probs, actuals, n_bins=10)
    assert 0.0 <= ece_wf <= 1.0
    assert ece_wf == pytest.approx(2.0 / 3.0, abs=1e-6)
    assert ece_ts == pytest.approx(ece_wf, abs=1e-6)


def test_walk_forward_rolling_windows_and_draw_gap(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    ledger = state_dir / "review_ledger.jsonl"
    rows = []
    for day_offset in range(60):
        m_date = f"2026-09-{(day_offset % 30) + 1:02d}" if day_offset < 30 else f"2026-10-{(day_offset - 30) + 1:02d}"
        rows.append(json.dumps({
            "date": m_date,
            "match_id": f"m_{day_offset}",
            "final_prob": [0.50, 0.25, 0.25],
            "market_fair": [0.48, 0.26, 0.26],
            "actual_idx": 1 if day_offset % 4 == 0 else 0,
            "best_selection": 0,
            "hit": day_offset % 4 != 0,
            "league": "英超",
        }))
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")

    ev = WalkForwardEvaluator(tmp_path)
    report = ev.evaluate()
    assert 0.0 <= report["metrics"]["ece"] <= 1.0
    assert report["draw_analysis"]["draw_calibration_gap"] == pytest.approx(0.0, abs=1e-4)

    windows = ev.evaluate_rolling_windows(window_days=30, step_days=30, min_matches=10)
    assert len(windows) >= 2
    assert all("rps" in w and "log_loss" in w and 0.0 <= w["ece"] <= 1.0 for w in windows)


def test_param_optimizer_supports_probabilistic_objectives_and_val_fn():
    space = {"alpha": [0.1, 0.5, 1.0]}

    def train_bt(params):
        a = params["alpha"]
        # alpha=0.5 对数损失最优，但故意给 alpha=1.0 虚高样本内 ROI
        return {
            "log_loss": 0.90 + (a - 0.5) ** 2,
            "rps": 0.18 + (a - 0.5) ** 2,
            "roi": 0.50 if a == 1.0 else 0.05,
            "match_count": 200,
            "avg_odds": 2.1,
        }

    def val_bt(params):
        a = params["alpha"]
        return {"log_loss": 0.92 + (a - 0.5) ** 2, "roi": 0.04}

    opt = ParamOptimizer(train_bt, space, objective="neg_log_loss", val_backtest_fn=val_bt)
    res = opt.optimize(min_matches=50, min_avg_odds=1.5, max_iterations=50)
    assert res.best_params["alpha"] == 0.5
    assert res.objective == "neg_log_loss"
    assert res.best_score == pytest.approx(-0.90)
    assert res.val_score == pytest.approx(-0.92)

    # 兼容旧版只关注 ROI 的调用方
    opt_roi = ParamOptimizer(train_bt, space, objective="roi")
    res_roi = opt_roi.optimize(min_matches=50, min_avg_odds=1.5, max_iterations=50)
    assert res_roi.best_params["alpha"] == 1.0
    assert res_roi.best_roi == pytest.approx(0.50)


def test_kelly_records_fair_edge_diagnostic_without_relaxing_gate(monkeypatch):
    strat = KellyStrategy()
    monkeypatch.setattr(strat, "_load_value_zones", lambda: {})
    preds = [
        # edge vs raw 1/2.0 = 0.04 >= 0.03（通过生产闸门），同时记录 fair_edge
        {
            "match_id": "m_pass",
            "direction": "home",
            "home_win_prob": 0.54,
            "draw_prob": 0.26,
            "away_win_prob": 0.20,
            "home_odds": 2.00,
            "market_fair": [0.51, 0.27, 0.22],
        },
        # edge vs raw 1/1.9 = 0.54 - 0.5263 = 0.0137 < 0.03，即使相对 fair 0.50 有 +0.04 仍保持拦截
        {
            "match_id": "m_blocked",
            "direction": "home",
            "home_win_prob": 0.54,
            "draw_prob": 0.26,
            "away_win_prob": 0.20,
            "home_odds": 1.90,
            "market_fair": [0.50, 0.27, 0.23],
        },
    ]
    plan = strat.evaluate_candidates(preds)
    assert [c.match_id for c in plan.singles] == ["m_pass"]
    c = plan.singles[0]
    assert c.fair_market_prob == pytest.approx(0.51)
    assert c.fair_edge == pytest.approx(0.03)
