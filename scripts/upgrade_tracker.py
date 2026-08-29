#!/usr/bin/env python3
"""升级效果追踪器（2026-08-30）

回答一个问题：2026-08-29 两轮升级之后，系统是否真的在变好？

口径：
  - 洁净组 = 账本中 chain=="v2" 的记录（升级后融合链产物）
  - 基线组 = 其余记录（升级前污染链产物），冻结于账本，永不重写
  - 参照 = data/state/ablation_report.json 里冻结的升级前全量指标
    （Brier 0.6172 / 命中率 51.6%）与纯市场（0.5834 / 54.7%）
  - 洁净组 ≥30 场后给配对显著性判定：IMPROVED / NEUTRAL / REGRESSED

用法: python3 scripts/upgrade_tracker.py   （周回测 workflow 自动调用）
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "data" / "state" / "review_ledger.jsonl"
ABLATION = ROOT / "data" / "state" / "ablation_report.json"
OUT = ROOT / "data" / "state" / "upgrade_tracker.json"


def brier(p, a):
    return sum((x - (1.0 if i == a else 0.0)) ** 2 for i, x in enumerate(p))


def argmax3(p):
    return max(range(3), key=lambda i: p[i])


def load_records():
    recs = []
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("final_prob") and r.get("actual_idx") is not None:
            recs.append(r)
    recs.sort(key=lambda r: (r.get("date", ""), r.get("match_id", "")))
    return recs


def summarize(recs):
    if not recs:
        return None
    b = [brier(r["final_prob"], r["actual_idx"]) for r in recs]
    hits = sum(argmax3(r["final_prob"]) == r["actual_idx"] for r in recs)
    return {
        "n": len(recs),
        "brier": round(sum(b) / len(b), 4),
        "hit_rate": round(hits / len(recs), 4),
        "_briers": b,
    }


def market_summary(recs):
    sub = [r for r in recs if r.get("market_fair")]
    if not sub:
        return None
    b = [brier(r["market_fair"], r["actual_idx"]) for r in sub]
    hits = sum(argmax3(r["market_fair"]) == r["actual_idx"] for r in sub)
    return {"n": len(sub), "brier": round(sum(b) / len(b), 4),
            "hit_rate": round(hits / len(sub), 4)}


def paired_vs(v2, legacy):
    """配对：同 date+match_id 交集不存在（两组互斥），用组间差 + Welch 近似 z。"""
    if not v2 or not legacy:
        return None
    m2, ml = v2["brier"], legacy["brier"]
    diff = ml - m2  # >0 = v2 更好
    se = math.sqrt(v2["_briers"].__len__() and
                   (sum((x - sum(v2["_briers"]) / len(v2["_briers"])) ** 2 for x in v2["_briers"]) / len(v2["_briers"])) / len(v2["_briers"]) +
                   (sum((x - sum(legacy["_briers"]) / len(legacy["_briers"])) ** 2 for x in legacy["_briers"]) / len(legacy["_briers"])) / len(legacy["_briers"]))
    z = diff / se if se > 0 else 0.0
    return {"delta_brier": round(diff, 4), "z": round(z, 2)}


def main():
    recs = load_records()
    v2 = [r for r in recs if r.get("chain") == "v2"]
    legacy = [r for r in recs if r.get("chain") != "v2"]

    v2s = summarize(v2)
    lgs = summarize(legacy)
    mkt = market_summary(v2)

    ref = {}
    if ABLATION.exists():
        try:
            ref = json.loads(ABLATION.read_text()).get("reference", {})
        except Exception:
            pass

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_pre_upgrade": {
            "brier": ref.get("stored_final_brier"), "hit_rate": ref.get("stored_final_hit"),
            "n": ref and 426,
        },
        "market_reference": {"brier": ref.get("market_brier"), "hit_rate": ref.get("market_hit")},
        "legacy_unchanged": {k: lgs[k] for k in ("n", "brier", "hit_rate")} if lgs else None,
        "v2_clean": {k: v2s[k] for k in ("n", "brier", "hit_rate")} if v2s else {"n": 0},
        "v2_vs_market_same_period": mkt,
    }

    print("=" * 62)
    print("升级效果追踪（2026-08-29 两轮升级 → chain=v2 洁净组）")
    print("=" * 62)
    print(f"升级前冻结基线:   Brier {ref.get('stored_final_brier', 0):.4f} / 命中 {ref.get('stored_final_hit', 0):.1%} (n=426)")
    print(f"纯市场参照:       Brier {ref.get('market_brier', 0):.4f} / 命中 {ref.get('market_hit', 0):.1%}")
    if lgs:
        print(f"遗留组(未升级链): Brier {lgs['brier']:.4f} / 命中 {lgs['hit_rate']:.1%} (n={lgs['n']})")
    if not v2s or v2s["n"] == 0:
        print("洁净组 v2: 暂无样本（升级后场次随结算自动累积，约 1 周后成组）")
        report["verdict"] = "WAITING_DATA"
    elif v2s["n"] < 30:
        print(f"洁净组 v2: Brier {v2s['brier']:.4f} / 命中 {v2s['hit_rate']:.1%} (n={v2s['n']}，<30 暂不判定)")
        report["verdict"] = "WARMING_UP"
    else:
        pv = paired_vs(v2s, lgs) if lgs else None
        base_b = ref.get("stored_final_brier")
        better_than_frozen = base_b is not None and v2s["brier"] < base_b - 0.005
        verdict = "IMPROVED" if (better_than_frozen and (pv is None or pv["z"] > 1.5 or pv["delta_brier"] > 0.005)) \
            else ("REGRESSED" if v2s["brier"] > base_b + 0.005 else "NEUTRAL")
        print(f"洁净组 v2: Brier {v2s['brier']:.4f} / 命中 {v2s['hit_rate']:.1%} (n={v2s['n']})")
        if mkt:
            print(f"同期纯市场: Brier {mkt['brier']:.4f} / 命中 {mkt['hit_rate']:.1%} (n={mkt['n']})")
        if pv:
            print(f"vs 遗留组: ΔBrier {pv['delta_brier']:+.4f} (z={pv['z']})")
        print(f"判定: {verdict}")
        report["verdict"] = verdict
        report["paired_vs_legacy"] = pv
    print("=" * 62)

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"✓ 已写入 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
