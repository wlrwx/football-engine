"""回归测试：串关池上限（评审 P2#18）+ 决策包代码指纹（P1#11）。"""
from __future__ import annotations

from types import SimpleNamespace

from engine.integrity.decision_bundle import DecisionBundle, compute_model_code_hashes
from engine.strategy.parlay import allocate_parlay_stakes


def _ticket(odds: float = 3.0, roi: float = 0.1) -> SimpleNamespace:
    return SimpleNamespace(
        stake=0.0, total_odds=odds, cal_roi=roi, potential=0.0, cal_ev=0.0
    )


def test_pool_cap_not_exceeded_with_many_tickets() -> None:
    """多票叠加时总注额不得突破串关池（原实现 0.6+0.1 模式可达 70%+）。"""
    recs = [_ticket() for _ in range(5)]
    weights = [0.5, 0.2, 0.15, 0.1, 0.05]
    pool = 30.0
    allocate_parlay_stakes(recs, weights, pool)
    assert sum(t.stake for t in recs) <= pool + 1e-9


def test_single_ticket_capped_at_60pct_of_pool() -> None:
    recs = [_ticket()]
    allocate_parlay_stakes(recs, [1.0], 100.0)
    assert recs[0].stake <= 60.0 + 1e-9


def test_ev_fields_recomputed_from_actual_stake() -> None:
    """potential/cal_ev 必须按最终 stake 重算（否则回到 2026-08-08 的老 bug）。"""
    t = _ticket(odds=3.5, roi=0.2)
    allocate_parlay_stakes([t], [1.0], 100.0)
    assert t.stake > 0
    assert abs(t.potential - round(3.5 * t.stake, 2)) < 1e-9
    assert abs(t.cal_ev - round(t.stake * 0.2, 2)) < 1e-9


def test_tiny_pool_does_not_overspend() -> None:
    """池子小于单注最小额时，_total 仍不得超池。"""
    recs = [_ticket() for _ in range(3)]
    allocate_parlay_stakes(recs, [1.0, 1.0, 1.0], 1.0)
    assert sum(t.stake for t in recs) <= 1.0 + 1e-9


def test_model_code_hashes_present_and_stable() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    h1 = compute_model_code_hashes(root)
    h2 = compute_model_code_hashes(root)
    assert h1, "决策包代码指纹不能为空"
    assert h1 == h2
    assert all(len(v) == 64 for v in h1.values())
    assert "engine/prediction/fusion.py" in h1


def test_bundle_records_code_hashes(tmp_path) -> None:
    mgr = DecisionBundle(tmp_path)
    bundle = mgr.create(
        date_str="2026-10-23",
        import_manifest={},
        predictions=[{"match_id": "m1"}],
        betting_plan={"total_stake": 0.0},
        config_prediction={},
        config_strategy={},
        model_code_hashes={"engine/main.py": "abc123"},
    )
    assert bundle["inputs"]["model_code_hashes"]["engine/main.py"] == "abc123"
    ok, msg = mgr.verify("2026-10-23")
    assert ok, msg
