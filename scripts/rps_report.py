#!/usr/bin/env python3
"""账本 RPS 报告（2026-08-29）

RPS（Ranked Probability Score）＝胜平负有序口径的概率分，比 Brier 更贴 1X2：
「主胜预测成客胜」罚得比「主胜预测成平局」重。0=完美，越小越好。

历史账本无 rps_* 字段 → 对 final_prob/market_fair/model_raw 即时计算，
不动不可变账本。用法: python3 scripts/rps_report.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.review.post_match import rps_score  # noqa: E402

LEDGER = ROOT / "data" / "state" / "review_ledger.jsonl"


def main() -> int:
    rows = [json.loads(l) for l in open(LEDGER, encoding="utf-8") if l.strip()]
    srcs = {"final": "final_prob", "market": "market_fair", "model": "model_raw", "djyy": "djyy_prob"}

    def rps_of(r, key):
        p = r.get(srcs[key])
        if not p:
            return None
        if isinstance(p, dict):
            p = [p.get("home"), p.get("draw"), p.get("away")]
        if not p or len(p) < 3:
            return None
        return rps_score(p, r["actual_idx"])

    print(f"账本 {len(rows)} 场\n")
    print(f"{'来源':<10}{'RPS':>8}{'n':>6}   （vs final，Δ<0 = 该来源更准）")
    base = None
    for key in srcs:
        vals = [v for r in rows if (v := rps_of(r, key)) is not None]
        m = sum(vals) / len(vals) if vals else float("nan")
        if key == "final":
            base = m
        print(f"{key:<10}{m:>8.4f}{len(vals):>6}   "
              f"{'' if base is None or key == 'final' else f'Δ={m-base:+.4f}'}")

    # 按月趋势：final 是否在向市场收敛
    monthly: dict[str, list] = defaultdict(list)
    for r in rows:
        v = rps_of(r, "final")
        if v is not None:
            monthly[str(r.get("date", ""))[:7]].append(v)
    print("\nfinal RPS 按月（n<10 的月份略）:")
    for mth in sorted(monthly):
        vals = monthly[mth]
        if len(vals) >= 10:
            print(f"  {mth}: {sum(vals)/len(vals):.4f}  (n={len(vals)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
