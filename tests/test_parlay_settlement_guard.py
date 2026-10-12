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
    # 2026-10-11：盈亏计算改为委派 settle_ticket 后 PnL = 回报 − 投入，
    # 锚点随之变化。守卫「早于盈亏计算」这个不变式本身没变。
    i_pnl = blk.index('_p = float(_settled.get("return") or 0.0) - _stake')
    assert i_guard < i_pnl, "幂等守卫必须早于盈亏计算"


def test_no_all_hit_in_settlement_block():
    """3串4 容错票错 1 场仍中 1 注 2串1，用 all_hit 判会把盈利记成亏损。

    实测 19 张 3串4 里 11 张被错记：投入 51.22 / 正确回报 65.79，
    正确 PnL +14.57 被记成 −51.22，直接影响熔断器与资金池。
    现在**容错票**委派 settle_ticket，组合运算不可能再分叉。

    注意：普通票**仍用 all_hit** 是正确的——2串1/3串1 就是全中才赢。
    所以这里要锁的是"容错票分支必须委派"，不是"块里不能出现 all_hit"。
    """
    blk = _parlay_block()
    assert '"串4" in str(_e.get("type") or "") and len(legs) == 3' in blk, \
        "必须识别出 3串4 容错票"
    assert "settle_ticket(_e, _res_map_for_tickets)" in blk, \
        "容错票必须委派 settle_ticket，不得用 all_hit 判输赢"
    i_fault = blk.index('_is_fault = "串4"')
    i_settle = blk.index("settle_ticket(_e, _res_map_for_tickets)")
    i_allhit = blk.index("_all_hit = True")
    assert i_fault < i_settle < i_allhit, \
        "容错票分支必须在 all_hit 之前分流"
    assert "settle_ticket" in blk, "应委派 settle_parlays.settle_ticket"


def test_reconcile_also_delegates():
    """reconcile.parlay_pnl 曾是第三份独立实现，同样漏了容错票。"""
    from engine.review import reconcile as rc
    import inspect
    assert "settle_ticket" in inspect.getsource(rc.parlay_pnl), \
        "reconcile.parlay_pnl 必须委派 settle_ticket，不得自带 all_hit 判输赢"


def test_fault_ticket_pnl_not_negative_when_it_won():
    """核心回归：2/3 腿中的 3串4 必须**有回血**，不能整注算成 −stake。

    真实数据形态（2026-08-11 那张）：legs=[True,True,False]，3串4 错1场
    仍中 1 注 2串1。旧代码按 all_hit 判成输，丢掉整笔回血。

    注意：低赔率时回血可能仍小于投入（仍亏，但亏得少）；高赔率时才是净赚。
    所以这里锁的不变式是"**严格优于 −stake**"，不是"必然盈利"。
    """
    from engine.review.settle_parlays import settle_ticket
    t = {
        "type": "3串4", "stake": 8.0, "n_bets": 4,
        "legs": [
            {"match": "m1", "sel": "home", "odds": 1.8},
            {"match": "m2", "sel": "home", "odds": 1.9},
            {"match": "m3", "sel": "home", "odds": 2.0},
        ],
    }
    res = {
        "m1": {"home_score": 1, "away_score": 0},   # 中
        "m2": {"home_score": 2, "away_score": 1},   # 中
        "m3": {"home_score": 0, "away_score": 1},   # 不中 → 1/4注中
    }
    got = settle_ticket(t, res)
    assert got["won"] is True
    assert got["return"] > 0
    # 关键：这注赢了，PnL 必须严格优于"全损"
    assert got["return"] - 8.0 > -8.0, "赢了的容错票不能按 −stake 记账"
    # 实际回血额 = stake/4 × 两腿赔率乘积
    assert got["return"] == pytest.approx(2.0 * 1.8 * 1.9)


def test_fault_ticket_can_be_net_profitable_at_higher_odds():
    """真实数据形态：赔率够高时 1/4注中就是净赚（对应那 11 张被错记的票）。"""
    from engine.review.settle_parlays import settle_ticket
    t = {
        "type": "3串4", "stake": 4.0, "n_bets": 4,
        "legs": [
            {"match": "m1", "sel": "home", "odds": 3.0},
            {"match": "m2", "sel": "home", "odds": 4.0},
            {"match": "m3", "sel": "home", "odds": 5.0},
        ],
    }
    res = {
        "m1": {"home_score": 1, "away_score": 0},
        "m2": {"home_score": 2, "away_score": 1},
        "m3": {"home_score": 0, "away_score": 1},
    }
    got = settle_ticket(t, res)
    assert got["won"] is True
    # stake/4 × 3.0 × 4.0 = 12 > 投入 4 → 净赚 8
    assert got["return"] == pytest.approx(12.0)
    assert got["return"] - 4.0 > 0, "高赔率容错票必须记为净盈利"


def test_all_three_legs_hit_keeps_full_return():
    """3/3 全中时 4 注全赢，回报应显著大于单注 2串1。"""
    from engine.review.settle_parlays import settle_ticket
    t = {
        "type": "3串4", "stake": 8.0, "n_bets": 4,
        "legs": [
            {"match": "m1", "sel": "home", "odds": 2.0},
            {"match": "m2", "sel": "home", "odds": 2.0},
            {"match": "m3", "sel": "home", "odds": 2.0},
        ],
    }
    res = {m: {"home_score": 1, "away_score": 0} for m in ("m1", "m2", "m3")}
    got = settle_ticket(t, res)
    assert got["won"] is True
    assert got["note"] == "4/4注中"


def test_zero_legs_hit_is_loss():
    from engine.review.settle_parlays import settle_ticket
    t = {
        "type": "3串4", "stake": 8.0, "n_bets": 4,
        "legs": [{"match": m, "sel": "home", "odds": 2.0} for m in ("m1", "m2", "m3")],
    }
    res = {m: {"home_score": 0, "away_score": 1} for m in ("m1", "m2", "m3")}
    got = settle_ticket(t, res)
    assert got["won"] is False
    assert got["return"] == 0.0


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
