#!/usr/bin/env python3
"""账本对账与回填（2026-10-11 新增）

## 要解决的问题

实测发现 **PnL 存在三个互不勾稽的地方**：

| 位置 | 覆盖 | 现状 |
|---|---|---|
| `data/state/review_ledger.jsonl` 的 `pnl` | 983 行 | 2026-07-21~08-07 恒为 0 |
| `data/daily/*/review.json` 的 `total_pnl` | 每日 | 文件已不存在（被后续运行覆盖） |
| `data/daily/*/ticket_plan.json` 的 `bankroll` | 每日 | 与前两者无共同口径 |

具体后果：**2026-08-03 那笔 −8,548.81（押反方向 bug 导致的单场全输）
只被写进过 `data/daily/2026-08-03/review.json`（2026-08-05 的提交 0182359a8），
账本从未被回填，那个文件现在也已不存在 —— 这笔亏损在账本里永久丢失。**

本工具把账本重建为**唯一真源**：按 `ticket_plan` + 账本里的赛果，
重新计算每一行的 pnl，并把差异全部列出来。

## 为什么不做增量修补而是全量重建

因为差异不是零星的：根因是 `pred['pnl']` 从未写回（`0bbab3123` 修复），
影响的是修复前的全部数据。逐条打补丁会漏。

## 结算口径与 engine/main.py 保持一致

按玩法前缀判定命中（`hcap_` / `ttg_` / `crs_` / `hafu_` / 无前缀），
命中 `stake*(odds-1)`，否则 `-stake`。串关另算：全中赢
`stake*(total_odds-1)`，否则 `-stake`。

## 用法

    python -m engine.review.reconcile              # 预演，只报告不改
    python -m engine.review.reconcile --apply      # 落盘回填
    python -m engine.review.reconcile --since 2026-07-20
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "data" / "state" / "review_ledger.jsonl"
DAILY = ROOT / "data" / "daily"
OUT_REPORT = ROOT / "data" / "state" / "ledger_reconcile.json"


def _sel_hit(sel: str, r: dict) -> bool | None:
    """按玩法前缀判定命中。与 engine/main.py 结算循环同口径。

    返回 None = 赛果不足，无法判定（而非判负）。partial 记录只有
    actual_idx + total_goals，够判 sel/ttg_ 但不够判 crs_/hafu_。
    """
    if r.get("partial"):
        idx = r.get("actual_idx")
        if idx is None:
            return None
        actual = ["home", "draw", "away"][int(idx)]
        if sel.startswith("ttg_"):
            try:
                return int(sel[4:]) == min(7, int(r.get("total_goals") or 0))
            except ValueError:
                return None
        if sel.startswith(("crs_", "hafu_", "hcap_")):
            return None
        return sel == actual
    hs = int(r.get("home_score") or 0)
    as_ = int(r.get("away_score") or 0)
    actual = "home" if hs > as_ else ("draw" if hs == as_ else "away")
    if sel.startswith("hcap_"):
        return sel[len("hcap_"):] == actual
    if sel.startswith("ttg_"):
        try:
            return int(sel[4:]) == min(7, hs + as_)
        except ValueError:
            return False
    if sel.startswith("crs_"):
        parts = sel[4:].split("_")
        return len(parts) == 2 and parts[0] == str(hs) and parts[1] == str(as_)
    if sel.startswith("hafu_"):
        hh = int(r.get("half_home_score") or 0)
        ha = int(r.get("half_away_score") or 0)
        half = "H" if hh > ha else ("D" if hh == ha else "A")
        full = "H" if hs > ha else ("D" if hs == ha else "A")
        return sel[5:] == half + full
    return sel == actual


def _load_plans() -> dict[str, dict]:
    out = {}
    for f in sorted(glob.glob(str(DAILY / "*" / "ticket_plan.json"))):
        try:
            out[f.split("/")[-2]] = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
    return out


def _load_scores() -> dict[str, dict]:
    """match_id → 比分。来源：data/state/match_history.db 的 match_history 表。

    注意账本 review_ledger.jsonl **只有 actual_idx（胜平负）和总进球**，
    没有比分 —— 所以正确比分（crs_）和半全场（hafu_）无法从账本重建，
    必须走 SQLite。
    """
    out: dict[str, dict] = {}
    db = ROOT / "data" / "state" / "match_history.db"
    if db.exists():
        import sqlite3
        try:
            con = sqlite3.connect(str(db))
            for mid, sh, sa, hh, ha in con.execute(
                    "SELECT match_id, score_home, score_away, ht_home, ht_away FROM match_history"):
                if mid is None or sh is None or sa is None:
                    continue
                out[str(mid)] = {
                    "home_score": int(sh),
                    "away_score": int(sa),
                    "half_home_score": hh,
                    "half_away_score": ha,
                }
            con.close()
        except Exception as e:
            print(f"  ⚠ match_history.db 读取失败: {type(e).__name__}: {e}")

    # 账本里有 total_goals_actual，可补齐"只有总进球没有精确比分"的场景
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        mid = r.get("match_id")
        tg = r.get("total_goals_actual")
        if mid and tg is not None and str(mid) not in out and r.get("actual_idx") is not None:
            # 胜平负已知 + 总进球已知 → 足以判 ttg_ 与 sel，但不足以判 crs_/hafu_
            out[str(mid)] = {
                "home_score": None, "away_score": None,
                "total_goals": int(tg),
                "actual_idx": int(r["actual_idx"]),
                "partial": True,
            }
    return out


def singles_pnl(entries: list[dict], score: dict | None) -> tuple[float, int, int]:
    """返回 (pnl, 命中期数, 期数)。score 为 None 表示赛果缺失，判定为未中。"""
    pnl = 0.0
    hit = 0
    n = 0
    for e in entries:
        stake = float(e.get("stake") or 0.0)
        if stake <= 0:
            continue
        verdict = _sel_hit(str(e.get("sel") or ""), score) if score else None
        if verdict is None:
            continue          # 赛果不足 → 不判定、不计盈亏
        n += 1
        if verdict:
            pnl += stake * (float(e.get("odds") or 0.0) - 1.0)
            hit += 1
        else:
            pnl -= stake
    return pnl, hit, n


def parlay_pnl(entries: list[dict], scores: dict[str, dict]) -> tuple[float, int, int, list]:
    """串关：全腿中才赢。任一腿赛果缺失 → 整注不判定（不计入）。"""
    pnl = 0.0
    won_n = 0
    n = 0
    unresolved: list[str] = []
    for e in entries:
        stake = float(e.get("stake") or 0.0)
        legs = e.get("legs") or []
        if stake <= 0 or not legs:
            continue
        all_hit = True
        missing = False
        for leg in legs:
            sc = scores.get(str(leg.get("match") or ""))
            if sc is None:
                missing = True
                break
            pick = leg.get("sel") or leg.get("score")
            if sc.get("partial"):
                if leg.get("sel") and sc.get("actual_idx") is not None:
                    ok = (pick == ["home", "draw", "away"][int(sc["actual_idx"])])
                else:
                    missing = True   # crs_/hafu_ 需要精确比分
                    break
            elif leg.get("sel"):
                hs, as_ = sc["home_score"], sc["away_score"]
                act = "home" if hs > as_ else ("draw" if hs == as_ else "away")
                ok = (pick == act)
            else:
                ok = (str(pick) == f"{sc['home_score']}-{sc['away_score']}")
            if not ok:
                all_hit = False
                break
        if missing:
            unresolved.append(str(legs[0].get("match")))
            continue
        n += 1
        if all_hit:
            pnl += stake * (float(e.get("total_odds") or 1.0) - 1.0)
            won_n += 1
        else:
            pnl -= stake
    return pnl, won_n, n, unresolved


def reconcile(since: str | None = None, apply: bool = False) -> dict:
    rows = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    plans = _load_plans()
    scores = _load_scores()

    by_mid = defaultdict(list)
    for r in rows:
        by_mid[(r.get("date"), r.get("match_id"))].append(r)

    changed: list[dict] = []
    unchanged = 0
    no_plan = 0
    for r in rows:
        d = r.get("date")
        if since and d < since:
            continue
        plan = plans.get(d)
        if not plan:
            no_plan += 1
            continue
        mid = str(r.get("match_id") or "")
        entries: list[dict] = []
        for b in ("stable", "value", "lottery"):
            for e in (plan.get(b) or []):
                if str(e.get("match", "")).split("#")[0] == mid:
                    entries.append(e)
        if not entries:
            no_plan += 1
            continue
        score = scores.get(mid)
        pnl, hit, n = singles_pnl(entries, score)
        old = r.get("pnl")
        if old is None or abs(float(old) - pnl) > 0.01:
            changed.append({
                "date": d, "match_id": mid,
                "old_pnl": old, "new_pnl": round(pnl, 2),
                "delta": round(pnl - float(old or 0.0), 2),
                "legs": n, "legs_hit": hit, "score_found": score is not None,
            })
            if apply:
                r["pnl"] = round(pnl, 2)
        else:
            unchanged += 1

    # 串关按日汇总（不写回单行，单独记账）
    parlay_rows = []
    for d in sorted(plans):
        plan = plans[d]
        entries = list(plan.get("parlay") or []) + list(plan.get("score_parlay") or [])
        if not entries:
            continue
        pnl, won, n, unres = parlay_pnl(entries, scores)
        if n == 0 and not unres:
            continue
        parlay_rows.append({
            "date": d, "settled": n, "won": won, "pnl": round(pnl, 2),
            "unresolved_legs": len(unres),
            "reported_total_stake": float(plan.get("total_stake") or 0.0),
            "single_stake_sum": round(sum(
                float(e.get("stake") or 0.0)
                for b in ("stable", "value", "lottery")
                for e in (plan.get(b) or [])), 2),
            "parlay_stake_sum": round(sum(
                float(e.get("stake") or 0.0)
                for b in ("parlay", "score_parlay")
                for e in (plan.get(b) or [])), 2),
        })

    # total_stake 漏算核查
    stake_gap = []
    for pr in parlay_rows:
        reported = pr["reported_total_stake"]
        real = pr["single_stake_sum"] + pr["parlay_stake_sum"]
        if abs(reported - real) > 0.01:
            stake_gap.append({
                "date": pr["date"], "reported": reported, "real": round(real, 2),
                "missing": round(real - reported, 2),
            })

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "applied": apply,
        "since": since,
        "ledger_rows": len(rows),
        "rows_recomputed": len(changed) + unchanged,
        "rows_changed": len(changed),
        "rows_unchanged": unchanged,
        "rows_without_plan_or_entries": no_plan,
        "changed_detail": changed[:80],
        "parlay_by_day": parlay_rows,
        "total_stake_gap": stake_gap,
        "total_stake_gap_sum": round(sum(x["missing"] for x in stake_gap), 2),
        "notes": [
            "pnl 差值 = 重建值 − 账本现值。负 = 账本少记了亏损。",
            "total_stake_gap：ticket_plan 报告的 total_stake 只含 stable/value/lottery，"
            "不含 parlay/score_parlay —— 真实暴露被系统性低估。",
            "赛果缺失的注按未中计（保守）；串关任一腿缺赛果则整注不判定。",
        ],
    }

    if apply and changed:
        backup = LEDGER.with_suffix(".jsonl.bak")
        backup.write_text(LEDGER.read_text(encoding="utf-8"), encoding="utf-8")
        with LEDGER.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        report["backup"] = str(backup.relative_to(ROOT))

    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="账本对账与回填")
    ap.add_argument("--apply", action="store_true", help="落盘回填（默认只预演）")
    ap.add_argument("--since", default=None)
    args = ap.parse_args()

    print("=" * 72)
    print("账本对账与回填" + ("（--apply，将修改账本）" if args.apply else "（预演）"))
    print("=" * 72)

    rep = reconcile(since=args.since, apply=args.apply)

    print(f"\n账本总行数            : {rep['ledger_rows']}")
    print(f"本次重算              : {rep['rows_recomputed']}")
    print(f"  pnl 有差异          : {rep['rows_changed']}")
    print(f"  pnl 已正确          : {rep['rows_unchanged']}")
    print(f"  无计划/无对应条目   : {rep['rows_without_plan_or_entries']}")

    if rep["changed_detail"]:
        print("\n── pnl 差异明细（前 15 条）──")
        print(f"{'日期':<12}{'场次':<22}{'原值':>10}{'重建':>12}{'差值':>12}")
        for c in rep["changed_detail"][:15]:
            print(f"{c['date']:<12}{str(c['match_id']):<22}"
                  f"{str(c['old_pnl']):>10}{c['new_pnl']:>12.2f}{c['delta']:>12.2f}")

    if rep["parlay_by_day"]:
        tot_p = sum(p["pnl"] for p in rep["parlay_by_day"])
        tot_n = sum(p["settled"] for p in rep["parlay_by_day"])
        tot_w = sum(p["won"] for p in rep["parlay_by_day"])
        print("\n── 串关（此前完全不在账本口径内）──")
        print(f"  可结算 {tot_n} 注，中 {tot_w} 注，PnL 合计 {tot_p:+.2f}")
        print(f"  腿缺失未判定: {sum(p['unresolved_legs'] for p in rep['parlay_by_day'])}")

    if rep["total_stake_gap"]:
        print("\n── total_stake 漏算串关 ──")
        print(f"  漏算天数: {len(rep['total_stake_gap'])}")
        print(f"  漏算金额: {rep['total_stake_gap_sum']:+.2f}")
        for g in rep["total_stake_gap"][:5]:
            print(f"    {g['date']}  报告 {g['reported']:.1f} → 实际 {g['real']:.1f}")

    OUT_REPORT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 已写入 {OUT_REPORT.relative_to(ROOT)}")
    if rep["changed_detail"] and not args.apply:
        print("\n加 --apply 可落盘回填（会自动备份账本为 .jsonl.bak）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
