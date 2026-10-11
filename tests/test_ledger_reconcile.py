"""账本对账与回填的回归测试（2026-10-11）

账本曾把 2026-07-22~08-03 的 33 行盈亏全部记成 0，
导致累计亏损被低估 45 倍（−1,129.2 vs 真实 −51,220.6）。
根因是 `pred['pnl']` 从未写回（0bbab3123 于 08-05 修复），
且当时只回填了 `data/daily/*/review.json`（现已不存在），账本从未回填。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.review import reconcile as rc  # noqa: E402


def _r(hs, as_):
    return {"home_score": hs, "away_score": as_,
            "half_home_score": None, "half_away_score": None}


# ---------- 命中判定与结算口径 ----------

def test_plain_selection_hit():
    assert rc._sel_hit("home", _r(2, 1)) is True
    assert rc._sel_hit("away", _r(2, 1)) is False
    assert rc._sel_hit("draw", _r(1, 1)) is True


def test_handicap_prefix():
    # 与 engine/main.py 结算同口径：hcap_xxx 直接和原始胜负比对
    assert rc._sel_hit("hcap_home", _r(2, 1)) is True
    assert rc._sel_hit("hcap_away", _r(2, 1)) is False


def test_total_goals_prefix():
    assert rc._sel_hit("ttg_3", _r(2, 1)) is True     # 2+1=3
    assert rc._sel_hit("ttg_4", _r(2, 1)) is False
    assert rc._sel_hit("ttg_7", _r(6, 4)) is True     # 10 球封顶为 7
    assert rc._sel_hit("ttg_2", _r(1, 1)) is True


def test_correct_score_prefix():
    assert rc._sel_hit("crs_2_1", _r(2, 1)) is True
    assert rc._sel_hit("crs_1_2", _r(2, 1)) is False


def test_hafu_prefix():
    sc = {"home_score": 2, "away_score": 1,
          "half_home_score": 1, "half_away_score": 0}
    assert rc._sel_hit("hafu_HH", sc) is True
    assert rc._sel_hit("hafu_HA", sc) is False


def test_partial_score_returns_none_not_false():
    """只有 actual_idx + total_goals 时，crs_/hafu_ 无法判定，必须返回 None 而非判负。

    这是本次修复的关键语义：把"赛果不足"误判成"未中"会凭空造出亏损。
    """
    partial = {"home_score": None, "away_score": None,
               "total_goals": 3, "actual_idx": 0, "partial": True}
    assert rc._sel_hit("home", partial) is True          # actual_idx 够判 sel
    assert rc._sel_hit("ttg_3", partial) is True          # total_goals 够判 ttg_
    assert rc._sel_hit("crs_2_1", partial) is None       # 判不了
    assert rc._sel_hit("hafu_HH", partial) is None       # 判不了


def test_singles_pnl_skips_undeterminable():
    """无法判定的腿不计入，不产生盈亏。"""
    entries = [
        {"sel": "home", "stake": 100.0, "odds": 2.0},      # 中 → +100
        {"sel": "crs_9_9", "stake": 50.0, "odds": 50.0},   # 判不了 → 不计
    ]
    partial = {"home_score": None, "away_score": None,
               "half_home_score": None, "half_away_score": None,
               "actual_idx": 0, "total_goals": 3, "partial": True}
    pnl, hit, n = rc.singles_pnl(entries, partial)
    assert n == 1, "无法判定的腿不应计入期数"
    assert pnl == pytest.approx(100.0)


def test_singles_pnl_loss_and_win():
    entries = [{"sel": "home", "stake": 100.0, "odds": 2.0}]
    win, hit, n = rc.singles_pnl(entries, _r(2, 1))
    assert (win, hit, n) == (pytest.approx(100.0), 1, 1)
    lose, hit, n = rc.singles_pnl(entries, _r(0, 1))
    assert (lose, hit, n) == (pytest.approx(-100.0), 0, 1)


def test_parlay_all_legs_must_hit():
    entries = [{
        "legs": [{"match": "m1", "sel": "home", "odds": 1.8},
                 {"match": "m2", "sel": "home", "odds": 1.5}],
        "total_odds": 2.7, "stake": 10.0,
    }]
    scores = {"m1": _r(1, 0), "m2": _r(2, 1)}
    pnl, won, n, unres = rc.parlay_pnl(entries, scores)
    assert (won, n, unres) == (1, 1, [])
    assert pnl == pytest.approx(17.0)                  # 10*(2.7-1)

    scores_bad = {"m1": _r(1, 0), "m2": _r(0, 2)}
    pnl, won, n, _ = rc.parlay_pnl(entries, scores_bad)
    assert (won, n) == (0, 1)
    assert pnl == pytest.approx(-10.0)


def test_parlay_missing_leg_is_unresolved_not_loss():
    """任一腿缺赛果 → 整注不判定，不能记成亏损。"""
    entries = [{
        "legs": [{"match": "m1", "sel": "home", "odds": 1.8},
                 {"match": "mX", "sel": "home", "odds": 1.5}],
        "total_odds": 2.7, "stake": 10.0,
    }]
    pnl, won, n, unres = rc.parlay_pnl(entries, {"m1": _r(1, 0)})
    assert n == 0, "缺腿的串关不应计入已结算"
    assert unres, "应记录未判定"
    assert pnl == pytest.approx(0.0), "未判定不得产生盈亏"


def test_parlay_score_legs():
    entries = [{
        "legs": [{"match": "m1", "score": "1-0", "odds": 7.5},
                 {"match": "m2", "score": "1-1", "odds": 7.5}],
        "total_odds": 56.25, "stake": 2.0,
    }]
    pnl, won, n, _ = rc.parlay_pnl(entries, {"m1": _r(1, 0), "m2": _r(1, 1)})
    assert (won, n) == (1, 1)
    assert pnl == pytest.approx(2.0 * 55.25)


# ---------- total_stake 口径 ----------

def test_total_exposure_includes_parlay():
    """ticket_plan 必须同时暴露 parlay_stake 与 total_exposure。

    原 total_stake 只含 stable/value/lottery，串关从未计入，
    实测 60 天漏算、合计 1215.20。
    """
    src = (ROOT / "engine" / "main.py").read_text(encoding="utf-8")
    assert '"total_exposure"' in src, "必须输出 total_exposure"
    assert '"parlay_stake"' in src, "必须输出 parlay_stake"
    # _parlay_stake 必须在使用前定义
    i_def = src.index("_parlay_stake = (")
    i_use = src.index('"total_exposure": round(')
    assert i_def < i_use, "_parlay_stake 定义必须早于使用"


def test_reconcile_module_present():
    assert (ROOT / "engine" / "review" / "reconcile.py").exists()
