#!/usr/bin/env python3
"""账本深度分段分析（2026-08-29）

只读 review_ledger.jsonl，输出命中率损失定位报告。
不写任何状态文件，可随时重复运行。

用法: python3 scripts/ledger_deepdive.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "state" / "review_ledger.jsonl"

DIRS = ("home", "draw", "away")


def load() -> list[dict]:
    return [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]


def argmax(p: list[float]) -> int:
    return max(range(3), key=lambda i: p[i])


def hit_of(pick: int, actual: int) -> bool:
    return pick == actual


def seg(name: str, rows: list[dict], key_fn) -> None:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        buckets[str(k)].append(r)
    print(f"\n--- {name} ---")
    print(f"{'segment':22s} {'n':>4s} {'final命中':>8s} {'市场命中':>8s} {'ROI':>8s} {'Brier最终':>9s} {'Brier市场':>9s}")
    for k in sorted(buckets, key=lambda x: -len(buckets[x])):
        rs = buckets[k]
        n = len(rs)
        fh = sum(1 for r in rs if r["hit"]) / n
        mh = [r for r in rs if r.get("brier_market") is not None]
        mk = sum(1 for r in mh if argmax(r["market_fair"]) == r["actual_idx"]) / len(mh) if mh else float("nan")
        roi = sum(r.get("pnl", 0) for r in rs)
        bf = sum(r["brier_final"] for r in rs) / n
        bm = sum(r["brier_market"] for r in mh) / len(mh) if mh else float("nan")
        print(f"{k:22s} {n:4d} {fh:8.1%} {mk:8.1%} {roi:8.0f} {bf:9.4f} {bm:9.4f}")


def main() -> int:
    rows = load()
    n = len(rows)
    print(f"账本总场数: {n}")

    # 0. 总体对比（同口径：final 已锁定的 pick vs 市场 argmax 假想）
    hit_f = sum(1 for r in rows if r["hit"]) / n
    mm = [r for r in rows if r.get("market_fair")]
    hit_m = sum(1 for r in mm if argmax(r["market_fair"]) == r["actual_idx"]) / len(mm)
    mod = [r for r in rows if r.get("model_raw")]
    hit_model = sum(1 for r in mod if argmax(r["model_raw"]) == r["actual_idx"]) / len(mod)
    bf = sum(r["brier_final"] for r in rows) / n
    bmrk = sum(r["brier_market"] for r in mm) / len(mm)
    print(f"final   命中 {hit_f:.1%}  Brier {bf:.4f}  (n={n})")
    print(f"市场argmax 命中 {hit_m:.1%}  Brier {bmrk:.4f}  (n={len(mm)})")
    print(f"模型argmax 命中 {hit_model:.1%}  (n={len(mod)})")
    roi_all = sum(r.get("pnl", 0) for r in rows)
    staked = sum(1 for r in rows if r.get("pnl", 0) != 0)
    print(f"记账 pnl 合计 {roi_all:.0f}（非零结算 {staked} 场）")

    # 1. 跟随市场与否分解：final pick == 市场 argmax？
    diff = [r for r in mm if argmax(r["final_prob"]) != argmax(r["market_fair"])]
    same = [r for r in mm if argmax(r["final_prob"]) == argmax(r["market_fair"])]
    for label, rs in (("与市场同向", same), ("与市场反向", diff)):
        if not rs:
            continue
        h = sum(1 for r in rs if r["hit"]) / len(rs)
        print(f"{label}: n={len(rs)} 命中 {h:.1%}")

    # 2. 平局覆盖
    actual_draw = sum(1 for r in rows if r["actual_idx"] == 1)
    pick_draw = [r for r in rows if r["best_selection"] == 1]
    pick_draw_hit = sum(1 for r in pick_draw if r["hit"])
    md = [r for r in mm if argmax(r["market_fair"]) == 1]
    print(f"\n实际平局率 {actual_draw/n:.1%}；final 选平局 {len(pick_draw)} 场 "
          f"({len(pick_draw)/n:.1%})，命中 {pick_draw_hit}/{len(pick_draw)}；"
          f"市场 argmax=平局 {len(md)} 场")

    # 3. 分段
    seg("按赔率档", rows, lambda r: r.get("odds_band"))
    seg("按概率档", rows, lambda r: r.get("prob_band"))
    seg("按信心层", rows, lambda r: r.get("confidence_tier"))
    seg("按选向", rows, lambda r: DIRS[r["best_selection"]])
    seg("按联赛(n≥15才看)", rows, lambda r: r.get("league"))

    # 4. 校准：final 概率分桶 vs 实际频率
    print("\n--- 校准（final 最高概率分桶 → 实际命中率）---")
    buckets = defaultdict(list)
    for r in rows:
        p = r["final_prob"][r["best_selection"]]
        buckets[round(min(p, 0.79), 1)].append(r)
    for k in sorted(buckets):
        rs = buckets[k]
        h = sum(1 for r in rs if r["hit"]) / len(rs)
        print(f"预测 {k:.1f}档: n={len(rs):3d} 实际 {h:.1%}  偏差 {h-k:+.1%}")

    # 5. 时间趋势（按周）
    weekly = defaultdict(list)
    for r in rows:
        d = r.get("date", "")
        if d:
            weekly[d[:7] + "-" + (d[8:10])].append(r)
    seg("按日期(前两月折叠)", rows, lambda r: r["date"][:7])

    # 6. 水位信号验证
    sig = [r for r in rows if r.get("market_signal_hit") is not None]
    if sig:
        good = sum(1 for r in sig if r["market_signal_hit"])
        print(f"\n水位信号可判场次 {len(sig)}，信号命中 {good/len(sig):.1%}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
