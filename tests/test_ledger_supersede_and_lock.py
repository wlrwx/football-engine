"""回归测试：账本改判 supersede（P0#3）+ 计划锁锁对对象（P0#2）。"""
from __future__ import annotations

import json

import pytest

from engine.integrity.plan_lock import PlanLock, compute_plan_hash
from engine.review.post_match import MatchReview, ReviewLedger


def _review(**kw) -> MatchReview:
    base = dict(
        match_id="2026-10-23_周一001",
        date="2026-10-23",
        league="英超",
        actual_idx=0,
        model_raw=[0.45, 0.28, 0.27],
        market_fair=[0.46, 0.27, 0.27],
        djyy_prob=None,
        final_prob=[0.45, 0.28, 0.27],
        confidence_tier="mid",
        odds_band="1.5-2.0",
        best_selection=0,
        hit=True,
        pnl=1.0,
        brier_model=0.4,
        brier_market=0.4,
        brier_djyy=None,
        brier_final=0.4,
        total_goals_actual=2,
    )
    base.update(kw)
    return MatchReview(**base)


def test_same_settlement_is_idempotent(tmp_path) -> None:
    led = ReviewLedger(tmp_path / "ledger.jsonl")
    led.append([_review()])
    led.append([_review()])
    lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1


def test_correction_is_appended_with_new_version(tmp_path) -> None:
    """改判必须入账（原实现静默丢弃 → stale actual 永久污染学习样本）。"""
    p = tmp_path / "ledger.jsonl"
    led = ReviewLedger(p)
    led.append([_review(actual_idx=0, total_goals_actual=2, hit=True, pnl=1.0)])
    # 赛果修正：主胜改判客胜
    led.append([_review(actual_idx=2, total_goals_actual=2, hit=False, pnl=-2.0)])

    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").strip().split("\n")]
    assert len(lines) == 2, "改判应追加新记录，而不是被丢弃"
    assert lines[0]["result_version"] == 0
    assert lines[1]["result_version"] == 1
    assert lines[1]["actual_idx"] == 2


def test_load_returns_latest_version_only(tmp_path) -> None:
    led = ReviewLedger(tmp_path / "ledger.jsonl")
    led.append([_review(actual_idx=0)])
    led.append([_review(actual_idx=2)])
    loaded = led.load_window()
    assert len(loaded) == 1, "同键多版本读取时应只保留最新版本"
    assert loaded[0].actual_idx == 2
    assert loaded[0].result_version == 1


def test_correction_keeps_position_in_window(tmp_path) -> None:
    """改判覆盖在**原位置**，不改变时间序列顺序（否则 train/val 切分会错位）。"""
    led = ReviewLedger(tmp_path / "ledger.jsonl")
    led.append([_review(match_id="2026-10-20_周一001", date="2026-10-20", actual_idx=0)])
    led.append([_review(match_id="2026-10-21_周一002", date="2026-10-21", actual_idx=1)])
    led.append([_review(match_id="2026-10-20_周一001", date="2026-10-20", actual_idx=2)])
    loaded = led.load_window()
    assert [r.date for r in loaded] == ["2026-10-20", "2026-10-21"]
    assert loaded[0].actual_idx == 2


# ---------------------------------------------------------------- 计划锁对象

def _plan(**kw) -> dict:
    base = {
        "singles": [{"match_id": "m1", "selection": "home", "stake": 10.0, "odds": 2.0}],
        "three_ticket": {"total": 10.0},
        "parlay": [],
        "score_parlay": [],
        "total_stake": 10.0,
        "parlay_stake": 0.0,
        "total_exposure": 10.0,
    }
    base.update(kw)
    return base


def test_plan_hash_covers_parlays() -> None:
    """核心回归：串关变化必须改变计划哈希。

    原实现只哈希 plan.singles —— 串关/三票改动完全不可见，
    锁住的承诺与实际出票脱节（评审 P0#2）。
    """
    a = _plan()
    b = _plan(parlay=[{"parlay_type": "2串1", "stake": 4.0}], parlay_stake=4.0,
              total_exposure=14.0)
    assert a["singles"] == b["singles"], "构造前提：单关完全一致"
    assert compute_plan_hash(a) != compute_plan_hash(b), "串关变化必须反映到计划哈希"


def test_plan_hash_stable_for_same_content() -> None:
    assert compute_plan_hash(_plan()) == compute_plan_hash(_plan())


def test_lock_rejects_rewrite_with_different_plan(tmp_path) -> None:
    mgr = PlanLock(tmp_path)
    h1 = compute_plan_hash(_plan())
    mgr.lock("2026-10-23", h1, "bundle-a")
    with pytest.raises(ValueError):
        mgr.lock("2026-10-23", compute_plan_hash(_plan(parlay=[{"x": 1}])), "bundle-b")
    ok, msg = mgr.verify_lock("2026-10-23", h1, "bundle-a")
    assert ok, msg


def test_lock_same_plan_is_idempotent(tmp_path) -> None:
    mgr = PlanLock(tmp_path)
    h = compute_plan_hash(_plan())
    mgr.lock("2026-10-23", h, "bundle-a")
    mgr.lock("2026-10-23", h, "bundle-a")  # 不应抛
    ok, _ = mgr.verify_lock("2026-10-23", h, "bundle-a")
    assert ok
