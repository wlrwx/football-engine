#!/usr/bin/env python3
"""odds_series 采集覆盖率体检（2026-10-11 新增）

## 为什么需要这个

盘中/收盘实验的可行性完全取决于 `data/state/odds_series/` 的覆盖质量，
但这个质量此前**没有任何指标**，只能靠肉眼看快照数。

2026-10-11 实测 899 场：

| 指标 | 值 |
|---|---|
| ≥2 个快照（可判开收盘） | 719（80.0%） |
| 相邻快照间隔（中位） | 3.92h |
| 最后快照距开赛（中位） | **9.5h** |
| 最后快照距开赛 ≤2 小时 | **仅 8.1%** |

结论：**只有 8% 的场次有开赛前 2 小时内的快照 —— 我们记的"收盘价"
大多不是收盘价。** 任何基于这批数据的 CLV 都只是"相对最后一次观测"。

去重逻辑（赔率未变只更新时间戳、不追加）本身是正确的，
3.92h 的间隔接近赔率真实变动频率。真正的问题是采集窗口右端太早。

本脚本把上述指标变成可监控的状态文件，让改善可验证、退化可发现。

用法：
    python -m engine.odds_series_coverage              # 打印 + 落盘
    python -m engine.odds_series_coverage --json       # 只输出 JSON
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERIES_DIR = ROOT / "data" / "state" / "odds_series"
OUT = ROOT / "data" / "state" / "odds_coverage.json"

# 判定门槛：开赛前 2 小时内有快照才算"拿到收盘价"
CLOSE_WINDOW_H = 2.0
NEAR_WINDOW_H = 6.0


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(len(s) * q / 100))], 2)


def _weighted_median(dist: Counter) -> int:
    """按场次展开后的中位数。dist: {快照数: 场次数}"""
    total = sum(dist.values())
    if not total:
        return 0
    acc = 0
    for k in sorted(dist):
        acc += dist[k]
        if acc >= total / 2:
            return k
    return max(dist)


def measure() -> dict:
    files = sorted(glob.glob(str(SERIES_DIR / "*.jsonl")))
    n_snap_dist: Counter = Counter()
    lead_last: list[float] = []
    lead_first: list[float] = []
    gaps: list[float] = []
    moved = 0
    flat = 0
    last_day_offset: Counter = Counter()
    by_kickoff_hour: dict[int, list[float]] = defaultdict(list)
    no_kickoff = 0

    for f in files:
        try:
            rows = [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
        except Exception:
            continue
        if not rows:
            continue
        n_snap_dist[len(rows)] += 1

        e0 = rows[0].get("euro", {}) or {}
        e1 = rows[-1].get("euro", {}) or {}
        if (e0.get("home"), e0.get("draw"), e0.get("away")) != \
           (e1.get("home"), e1.get("draw"), e1.get("away")):
            moved += 1
        else:
            flat += 1

        if len(rows) >= 2:
            try:
                for i in range(1, len(rows)):
                    gaps.append((datetime.fromisoformat(rows[i]["ts"])
                                 - datetime.fromisoformat(rows[i - 1]["ts"])).total_seconds() / 3600)
            except Exception:
                pass

        mt = rows[0].get("match_time")
        if not mt:
            no_kickoff += 1
            continue
        try:
            ko = datetime.fromisoformat(mt)
            t0 = datetime.fromisoformat(rows[0]["ts"])
            t1 = datetime.fromisoformat(rows[-1]["ts"])
        except Exception:
            no_kickoff += 1
            continue
        lead_last.append((ko - t1).total_seconds() / 3600)
        lead_first.append((ko - t0).total_seconds() / 3600)
        last_day_offset[(ko.date() - t1.date()).days] += 1
        by_kickoff_hour[ko.hour].append((ko - t1).total_seconds() / 3600)

    total = sum(n_snap_dist.values())
    within_close = sum(1 for g in lead_last if 0 <= g <= CLOSE_WINDOW_H)
    within_near = sum(1 for g in lead_last if 0 <= g <= NEAR_WINDOW_H)
    # 已开赛的（lead<0）不算"拿到收盘"，它们本来就是事后的
    not_started = sum(1 for g in lead_last if g > 0)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_matches": total,
        "no_kickoff_time": no_kickoff,
        "snapshots": {
            "mean": round(sum(k * v for k, v in n_snap_dist.items()) / max(total, 1), 2),
            # 中位数要按场次展开后取，不能取分布键的中位
            "median": _weighted_median(n_snap_dist),
            "max": max(n_snap_dist) if n_snap_dist else 0,
            "dist": {str(k): v for k, v in sorted(n_snap_dist.items())},
        },
        "multi_snapshot": {
            "n": sum(v for k, v in n_snap_dist.items() if k >= 2),
            "ratio": round(sum(v for k, v in n_snap_dist.items() if k >= 2) / max(total, 1), 4),
        },
        "odds_actually_moved": moved,
        "odds_flat": flat,
        "gap_hours": {
            "median": round(st.median(gaps), 2) if gaps else None,
            "mean": round(st.mean(gaps), 2) if gaps else None,
            "p90": _pct(gaps, 90),
        },
        "lead_to_kickoff_hours": {
            "median_last": round(st.median(lead_last), 2) if lead_last else None,
            "p10_last": _pct(lead_last, 10),
            "p90_last": _pct(lead_last, 90),
            "median_first": round(st.median(lead_first), 2) if lead_first else None,
        },
        "closing_coverage": {
            "window_h": CLOSE_WINDOW_H,
            "n": within_close,
            "ratio": round(within_close / max(total, 1), 4),
            "not_started_yet": not_started,
        },
        "near_close_coverage": {
            "window_h": NEAR_WINDOW_H,
            "n": within_near,
            "ratio": round(within_near / max(total, 1), 4),
        },
        "last_collection_day_offset": {
            str(k): v for k, v in sorted(last_day_offset.items())
        },
        "by_kickoff_hour": {
            str(h): {"n": len(v), "median_lead": round(st.median(v), 2)}
            for h, v in sorted(by_kickoff_hour.items())
        },
        "healthy": bool(total and within_close / max(total, 1) >= 0.5),
        "note": (
            "healthy 判据：开赛前 %.0f 小时内有快照的场次 ≥ 50%%（且该场尚未开赛）。"
            "2026-10-11 基线 3.6%% —— CLV 类实验在此之前不可用。"
            % CLOSE_WINDOW_H
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--warn-only", action="store_true",
                    help="不健康时只发 GitHub warning 注解，不返回非零退出码")
    ap.add_argument("--strict", action="store_true",
                    help="不健康时返回非零退出码（可作 CI 门禁）")
    args = ap.parse_args()

    rep = measure()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    print("=" * 70)
    print("odds_series 采集覆盖率体检")
    print("=" * 70)
    print(f"  场次总数            : {rep['total_matches']}")
    print(f"  无开赛时间          : {rep['no_kickoff_time']}")
    s = rep["snapshots"]
    print(f"  每场快照数          : 均值 {s['mean']} / 中位 {s['median']} / 最大 {s['max']}")
    print(f"  ≥2 个快照           : {rep['multi_snapshot']['n']} "
          f"({rep['multi_snapshot']['ratio']*100:.1f}%)")
    print(f"  赔率确有变动        : {rep['odds_actually_moved']}（未变 {rep['odds_flat']}）")
    g = rep["gap_hours"]
    print(f"  相邻快照间隔(小时)  : 中位 {g['median']} / 均值 {g['mean']} / P90 {g['p90']}")
    lt = rep["lead_to_kickoff_hours"]
    print(f"  首次快照提前(小时)  : 中位 {lt['median_first']}")
    print(f"  末次快照提前(小时)  : 中位 {lt['median_last']} "
          f"(P10 {lt['p10_last']} / P90 {lt['p90_last']})")
    c = rep["closing_coverage"]
    print(f"\n  ★ 开赛前 {c['window_h']}h 内有快照: {c['n']} ({c['ratio']*100:.1f}%)"
          f"   ← CLV 可用性的关键指标")
    nc = rep["near_close_coverage"]
    print(f"  ★ 开赛前 {nc['window_h']}h 内有快照: {nc['n']} ({nc['ratio']*100:.1f}%)")
    print(f"  健康判定            : {'✓ 健康' if rep['healthy'] else '✗ 不健康'}")
    print(f"\n  {rep['note']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 已写入 {OUT.relative_to(ROOT)}")

    if not rep["healthy"]:
        msg = (f"赔率时序覆盖率不健康：开赛前 {CLOSE_WINDOW_H}h 内快照仅 "
               f"{rep['closing_coverage']['ratio']*100:.1f}%（门槛 50%）")
        if args.strict:
            print(f"::error::{msg}")
            return 1
        if args.warn_only:
            print(f"::warning::{msg} —— CLV 类实验当前不可用")
            return 0
        print(f"⚠ {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
