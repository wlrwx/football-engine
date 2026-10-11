"""串关结算的幂等性与缺腿处理回归测试（2026-10-11）

这两个 bug 是我在 PR #7 里自己引入的，由本 PR 修掉：

**① 无幂等守卫。** 串关结算块遍历 `ticket_plan` 而非 `new_items`
（单注循环走的是 `new_items`，只处理新结算）。结果：每次重跑结算都会把
同一批串关重复计入 `breaker.record_result` 与 `running_bankroll`。

**② 缺腿被当成输。** 原实现里腿未结算时 `all_hit = False`，
但紧接着 `parlay_n += 1` 且 `_p = -_stake` —— 注释写"不下结论"，
代码却判了输。这会**凭空造出亏损**。

对照：`engine/review/reconcile.py` 里同一语义是正确的（缺腿 return / skip），
测试同时锁住两处必须一致。
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAIN = (ROOT / "engine" / "main.py").read_text(encoding="utf-8")


def _parlay_block() -> str:
    m = re.search(r"# 7b\) 串关结算.*?breaker\.record_result\(won=_won", MAIN, re.S)
    assert m, "未找到串关结算块"
    return m.group(0)


# ---------- ① 幂等 ----------

def test_idempotence_guard_present():
    """必须有"本轮至少有一腿是新结算"的守卫，否则重跑会重复计入。"""
    blk = _parlay_block()
    assert "_new_mids" in blk, "缺少本轮新结算场次集合"
    assert "if not any(lg.get(\"match\") in _new_mids" in blk, \
        "缺少幂等守卫：无新腿的串关必须跳过"
    assert "parlay_skipped_already" in blk, "需要记录已结算跳过的数量"


def test_singles_loop_still_uses_new_items():
    """单注循环必须保持走 new_items（对照：串关曾遗漏了这一点）。"""
    assert "for n in new_items:" in MAIN


def test_idempotence_guard_runs_before_pnl_assignment():
    """守卫必须在 pnl 计算之前，不能算完再跳。"""
    blk = _parlay_block()
    i_guard = blk.index('if not any(lg.get("match") in _new_mids')
    i_pnl = blk.index("_p = _stake * (_tot - 1.0)")
    assert i_guard < i_pnl, "幂等守卫必须早于盈亏计算"


# ---------- ② 缺腿不判负 ----------

def test_missing_leg_skips_instead_of_counting_as_loss():
    """腿未全部出赛果时必须 continue，不能计入 parlay_n、更不能产生 -stake。"""
    blk = _parlay_block()
    i_break = blk.index("if len(_scored) != len(legs):")
    i_skip = blk.index("parlay_skipped_unresolved += 1")
    i_cont = blk.index("continue", i_skip)
    i_incr = blk.index("parlay_n += 1")
    assert i_break < i_cont < i_incr, \
        "缺腿分支必须在 parlay_n += 1 之前 continue"


def test_no_bare_all_hit_false_on_missing():
    """旧的 `all_hit = False; break`（注释说不下结论、代码却判输）已不存在。"""
    blk = _parlay_block()
    assert "该腿还没结算" not in blk, "旧的误判分支仍在"
    # 缺腿分支不应再直接置 all_hit
    m = re.search(r"if _sc is None:\s*\n\s*all_hit = False", blk)
    assert m is None, "缺腿时不得置 all_hit=False（会判成输）"


# ---------- 与 reconcile 口径一致 ----------

def test_missing_leg_semantics_match_reconcile():
    """两处缺腿语义必须一致：都不判定、不计盈亏。"""
    from engine.review import reconcile as rc
    entries = [{
        "legs": [{"match": "m1", "sel": "home", "odds": 1.8},
                 {"match": "mX", "sel": "home", "odds": 1.5}],
        "total_odds": 2.7, "stake": 10.0,
    }]
    scores = {"m1": {"home_score": 1, "away_score": 0,
                     "half_home_score": None, "half_away_score": None}}
    pnl, won, n, unres = rc.parlay_pnl(entries, scores)
    assert n == 0 and pnl == pytest.approx(0.0), \
        "reconcile 侧缺腿不得计入（main.py 曾犯过此错）"


def test_parlay_all_hit_math():
    """全中赢 stake*(total_odds-1)，未中 -stake —— 两处都应如此。"""
    from engine.review import reconcile as rc
    entries = [{
        "legs": [{"match": "m1", "sel": "home", "odds": 1.8},
                 {"match": "m2", "sel": "home", "odds": 1.5}],
        "total_odds": 2.7, "stake": 10.0,
    }]
    sc = {"m1": {"home_score": 1, "away_score": 0,
                 "half_home_score": None, "half_away_score": None},
          "m2": {"home_score": 2, "away_score": 1,
                 "half_home_score": None, "half_away_score": None}}
    pnl, won, n, _ = rc.parlay_pnl(entries, sc)
    assert (won, n) == (1, 1)
    assert pnl == pytest.approx(17.0)


# ---------- 跳过计数会被打印（可观测） ----------

def test_skip_counts_reported():
    """跳过的数量必须打印出来，否则"静默跳过"又变成新的不可见故障。"""
    assert "腿未齐跳过" in MAIN and "已结算跳过" in MAIN
