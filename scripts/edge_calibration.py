#!/usr/bin/env python3
"""EV 校准体检（2026-08-30）

投注系统的命门问题：我们自以为找到的"价值优势"（edge = p×odds−1）是真的吗？

方法：扫全部已结算的 predictions，按预测时的 kelly_edge 分桶，
对比「需要的命中率 1/odds」和「实际命中率」——
  - 实际 > 需要 → 该 edge 桶真有价值（长期正 EV）
  - 实际 ≈ 需要 → 白忙（付了抽水）
  - 实际 < 需要 → 该桶是负 EV，应禁投/收紧门槛
同时按方向概率分桶做可靠性曲线（概率校准）。

用法: python3 scripts/edge_calibration.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "state" / "edge_calibration.json"

EDGE_BUCKETS = [(0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 10.0)]
PROB_BUCKETS = [(0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 1.01)]


def load_settled():
    rows = []
    for pf in sorted((ROOT / "data" / "daily").glob("*/predictions.json")):
        try:
            preds = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in preds:
            if p.get("actual_result") is None or not p.get("direction"):
                continue
            d = p.get("direction")
            odds = p.get(f"{d}_odds")
            prob = p.get(f"{d}_win_prob") or p.get({"home": "home_win_prob",
                                                    "draw": "draw_prob",
                                                    "away": "away_win_prob"}[d])
            if not odds or odds <= 1.0 or prob is None:
                continue
            rows.append({
                "date": pf.parent.name,
                "edge": p.get("kelly_edge") or (prob * odds - 1),
                "prob": prob,
                "odds": odds,
                "need": 1.0 / odds,
                "hit": bool(p.get("direction_correct")),
                "synthetic": bool(p.get("odds_synthetic")),
            })
    return [r for r in rows if not r["synthetic"]]


def bucket(rows, key, buckets):
    out = []
    for lo, hi in buckets:
        sub = [r for r in rows if lo <= r[key] < hi]
        if not sub:
            out.append({"bucket": f"[{lo:.2f},{hi:.2f})", "n": 0})
            continue
        n = len(sub)
        hit = sum(r["hit"] for r in sub) / n
        need = sum(r["need"] for r in sub) / n
        out.append({
            "bucket": f"[{lo:.2f},{hi:.2f})", "n": n,
            "actual_hit_rate": round(hit, 4), "required_hit_rate": round(need, 4),
            "ev_gap": round(hit - need, 4),
            "roi_proxy": round(hit / need - 1, 4),
        })
    return out


def main():
    rows = load_settled()
    print("=" * 66)
    print("EV 校准体检（预测时 edge/概率 vs 实际命中）")
    print("=" * 66)
    print(f"已结算真实赔率场次: {len(rows)}")

    edge_rows = [r for r in rows if r["edge"] is not None and r["edge"] > 0]
    print(f"\n-- 按 kelly_edge 分桶（{len(edge_rows)} 场正 edge）--")
    print(f"{'edge 桶':<14}{'n':>5}{'实际命中':>9}{'需要命中':>9}{'EV差':>8}{'ROI代理':>9}")
    edge_out = bucket(edge_rows, "edge", EDGE_BUCKETS)
    for b in edge_out:
        if b["n"] == 0:
            print(f"{b['bucket']:<14}{0:>5}")
        else:
            print(f"{b['bucket']:<14}{b['n']:>5}{b['actual_hit_rate']:>9.1%}"
                  f"{b['required_hit_rate']:>9.1%}{b['ev_gap']:>+8.1%}{b['roi_proxy']:>+9.1%}")

    print(f"\n-- 按方向概率分桶（可靠性曲线，全部 {len(rows)} 场）--")
    print(f"{'概率桶':<14}{'n':>5}{'实际命中':>9}{'平均概率':>9}{'偏差':>8}")
    prob_out = bucket(rows, "prob", PROB_BUCKETS)
    for b in prob_out:
        if b["n"] == 0:
            print(f"{b['bucket']:<14}{0:>5}")
        else:
            sub = [r["prob"] for lo, hi in PROB_BUCKETS if f"[{lo:.2f},{hi:.2f})" == b["bucket"]
                   for r in rows if lo <= r["prob"] < hi]
            avgp = sum(sub) / len(sub)
            print(f"{b['bucket']:<14}{b['n']:>5}{b['actual_hit_rate']:>9.1%}"
                  f"{avgp:>9.1%}{b['actual_hit_rate'] - avgp:>+8.1%}")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_settled": len(rows), "n_positive_edge": len(edge_rows),
        "edge_buckets": edge_out, "prob_buckets": prob_out,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n✓ 已写入 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
