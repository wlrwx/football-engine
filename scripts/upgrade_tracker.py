#!/usr/bin/env python3
"""升级效果追踪器（2026-08-30 首版 / 2026-10-11 修正记账口径）

回答两个**互相独立**的问题，历史上被混成一个 `verdict`，导致系统性误报：

  Q1 工程回归：新的融合链（chain=="v2"）比旧链（chain!=v2）更好吗？
     → verdict / paired_vs_legacy
     这是"换链有没有搞坏"的回归检查。IMPROVED 只在这里有语义。

  Q2 真实价值：系统比去水市场更好吗？
     → verdict_vs_market / paired_vs_market
     这是"这套系统有没有 edge"的唯一有效检验。
     必须在**同一批记录**上做配对比较，且默认结论是"没有"。

修正记录（2026-10-11，独立复核 review_ledger.jsonl 955 行后）：
  1. 旧版把 market_summary(v2) 填进键 "v2_vs_market_same_period"，
     该键读起来像"v2 的分数"，实际填的是**市场自己的分数**。
     实测：final=0.5667 / market=0.5631，而该键记的是 0.5631。
     → 任何读这个键的人都会得出"v2 赢市场 0.02"的错误结论。
  2. 旧版 "market_reference"(0.5834) 取自 ablation 冻结的 **legacy 364 行子集**，
     与 v2 的 483 行是**不同比赛集合**，两者相减本身无效。
  3. 旧版 "baseline_pre_upgrade.n" 硬编码为 426。
  4. 旧版从未做过 final vs market 的配对检验——即从未回答 Q2。

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
FUSION_W = ROOT / "data" / "state" / "fusion_weights.json"
PRED_CFG = ROOT / "config" / "prediction.json"
OUT = ROOT / "data" / "state" / "upgrade_tracker.json"

SCHEMA_VERSION = 2
MIN_N_VERDICT = 30          # 低于此不做判定
MIN_N_MARKET = 30           # vs 市场判定所需最小配对样本
# DJYY 命中率低于此值即视为"未接线"（名义权重不产生贡献）。
# 实测：CI 侧被 Cloudflare 403 拦截期间为 4/558 = 0.7%；
# 恢复后应回到 50%+。取 0.5 是因为低于一半就谈不上"参与融合"。
DJYY_MIN_HIT_RATE = 0.5


def brier(p, a):
    return sum((x - (1.0 if i == a else 0.0)) ** 2 for i, x in enumerate(p))


def argmax3(p):
    return max(range(3), key=lambda i: p[i])


def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


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
        "brier": round(_mean(b), 4),
        "hit_rate": round(hits / len(recs), 4),
        "_briers": b,
    }


def paired_vs_market(recs):
    """Q2 的核心：同一批记录上 final vs market_fair 的配对检验。

    约定：delta = brier_final - brier_market，**负 = 模型更好**。
    """
    pairs = []
    for r in recs:
        mf = r.get("market_fair")
        if not mf or len(mf) != 3:
            continue
        a = r["actual_idx"]
        pairs.append((brier(r["final_prob"], a), brier(mf, a)))

    n = len(pairs)
    if n < 2:
        return {"n": n, "verdict": "NO_DATA"}

    diffs = [f - m for f, m in pairs]
    md = _mean(diffs)
    sd = _std(diffs)
    se = sd / math.sqrt(n)
    t = md / se if se > 0 else 0.0
    ci = (md - 1.96 * se, md + 1.96 * se)

    wins = sum(1 for d in diffs if d < 0)
    losses = sum(1 for d in diffs if d > 0)

    # 默认答案是"没有 edge"。只有显著优于市场才叫 BEATING。
    if t < -1.96:
        verdict = "BEATING"
    elif t > 1.96:
        verdict = "LOSING"
    else:
        verdict = "TIED"

    return {
        "n": n,
        "brier_final": round(_mean([p[0] for p in pairs]), 4),
        "brier_market": round(_mean([p[1] for p in pairs]), 4),
        "delta_brier": round(md, 4),
        "t": round(t, 2),
        "ci95_delta": [round(ci[0], 4), round(ci[1], 4)],
        "significant": abs(t) >= 1.96,
        "matches_better": wins,
        "matches_worse": losses,
        "verdict": verdict if n >= MIN_N_MARKET else "WARMING_UP",
    }


def paired_vs_legacy(v2s, lgs):
    """Q1：两个互斥分组（非配对），Welch 近似 t。delta > 0 = v2 更好。"""
    if not v2s or not lgs:
        return None
    diff = lgs["brier"] - v2s["brier"]          # >0 = v2 更好
    n2, nl = len(v2s["_briers"]), len(lgs["_briers"])
    se = math.sqrt((_std(v2s["_briers"]) ** 2) / n2 + (_std(lgs["_briers"]) ** 2) / nl)
    t = diff / se if se > 0 else 0.0
    return {"delta_brier": round(diff, 4), "t": round(t, 2), "n_v2": n2, "n_legacy": nl}


def effective_weights(v2_recs):
    """报告**运行时真正生效**的融合权重（归一化后），不是 config 里的名义值。

    背景：fusion.py 在某些源缺失时会重新归一化，所以 champion 权重
    (model 0.05 / market 0.75 / djyy 0.20) 在 djyy 缺席时实际等价于
    model 6.25% / market 93.75%。这个比例才是模型边际贡献的真实上界。

    djyy 是否"活着"由账本实测决定，不看配置文件——实测 v2 链 529 场
    djyy_prob 命中率为 0，说明数据源已停，名义 0.2 的权重是死权重。
    """
    out = {"source": None, "raw": None, "effective": None, "djyy_hit_rate": None, "note": ""}
    try:
        champ = json.loads(FUSION_W.read_text(encoding="utf-8")).get("champion", {})
    except Exception:
        return out

    m, k, d = champ.get("model"), champ.get("market"), champ.get("djyy")
    if m is None:
        return out
    out["source"] = "data/state/fusion_weights.json#champion"
    out["raw"] = {"model": m, "market": k, "djyy": d}

    # 用账本实测 DJYY 在当前链的命中率，而不是相信权重文件里的声明。
    # 2026-10-11 修正：判定阈值原先是 `> 0.0`，于是 4/558 = 0.72% 这种
    # "基本已死"的状态也被当成"完全存活"，有效权重就等于原始权重，
    # 而真实情况是 djyy 名义 0.2 几乎从不生效、权重被摊回 model/market。
    # 改为显式阈值：命中率低于该值即视为未接线。
    n2 = len(v2_recs)
    hits2 = sum(1 for r in v2_recs if r.get("djyy_prob"))
    hit_rate = (hits2 / n2) if n2 else 0.0
    out["djyy_hit_rate"] = round(hit_rate, 4)
    djyy_live = hit_rate >= DJYY_MIN_HIT_RATE

    tw = (m or 0) + (k or 0) + ((d or 0) if djyy_live else 0)
    if tw <= 0:
        return out
    out["effective"] = {
        "model": round((m or 0) / tw, 4),
        "market": round((k or 0) / tw, 4),
        "djyy": round((d or 0) / tw, 4) if djyy_live else 0.0,
    }
    if not djyy_live:
        out["note"] = (f"⚠ DJYY 名义权重 {d} 当前完全不生效（v2 链 {n2} 场中 "
                       f"djyy_prob 命中 {hits2} 场，命中率 0%）。名义权重和为 "
                       f"{round((m or 0)+(k or 0)+(d or 0), 3)}，归一化后真实比例为 "
                       f"model {(m or 0)/tw:.2%} / market {(k or 0)/tw:.2%}")
    return out


def main():
    recs = load_records()
    # 污染样本隔离（2026-10-11）：v2 链里有 20 场 league_draw_baseline 越界样本
    # （巴甲平局概率被钉在 0.5025，真实率 0.266）。它们不是"新融合链的产物"，
    # 混进 Q1/Q2 判定会让升级效果被一个已修 bug 的残留污染。
    # 详见 engine/integrity/quarantine.py
    try:
        from engine.integrity.quarantine import clean_records as _qfilter
        v2 = _qfilter([r for r in recs if r.get("chain") == "v2"])
    except Exception:
        v2 = [r for r in recs if r.get("chain") == "v2"]
    legacy = [r for r in recs if r.get("chain") != "v2"]

    v2s = summarize(v2)
    lgs = summarize(legacy)

    # Q2：配对检验（这是旧版本完全缺失的）
    pvm = paired_vs_market(v2) if v2 else {"n": 0, "verdict": "NO_DATA"}

    ref = {}
    if ABLATION.exists():
        try:
            ref = json.loads(ABLATION.read_text()).get("reference", {})
        except Exception:
            pass

    # 冻结基线 n：优先取 reference 自带字段，否则用 legacy 组 n
    # （实测 ablation 的 stored_final_brier 与 legacy 组 brier 完全一致，
    #   说明冻结基线就是 legacy 组本身，不该硬编码 426）
    frozen_n = None
    try:
        frozen_n = int(ref.get("stored_final_n") or 0) or None
    except Exception:
        pass
    if frozen_n is None and lgs:
        frozen_n = lgs["n"]

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "schema": SCHEMA_VERSION,
        "baseline_pre_upgrade": {
            "brier": ref.get("stored_final_brier"),
            "hit_rate": ref.get("stored_final_hit"),
            "n": frozen_n,
            "note": "ablation_report.json 冻结的升级前全量基线（与下方 market_reference 同子集）",
        },
        "market_reference_frozen": {
            "brier": ref.get("market_brier"),
            "hit_rate": ref.get("market_hit"),
            "n": ref.get("market_n"),
            "note": "⚠ 这是 legacy 子集的市场分，**不可与 v2_clean 直接相减**（比赛集合不同）。"
                    "与市场的有效比较一律以 paired_vs_market 为准。",
        },
        "legacy_unchanged": {k: lgs[k] for k in ("n", "brier", "hit_rate")} if lgs else None,
        "v2_clean": {k: v2s[k] for k in ("n", "brier", "hit_rate")} if v2s else {"n": 0},
        "effective_fusion_weights": effective_weights(v2),
    }

    print("=" * 70)
    print("升级效果追踪  [Q1 工程回归 vs 旧链] / [Q2 真实价值 vs 去水市场]")
    print("=" * 70)

    print("\n--- Q1 工程回归：v2 链 vs 旧链 ---")
    if not v2s or v2s["n"] == 0:
        print("洁净组 v2: 暂无样本")
        report["verdict"] = "WAITING_DATA"
    elif v2s["n"] < MIN_N_VERDICT:
        print(f"洁净组 v2: Brier {v2s['brier']:.4f} (n={v2s['n']}，<{MIN_N_VERDICT} 暂不判定)")
        report["verdict"] = "WARMING_UP"
    else:
        pv = paired_vs_legacy(v2s, lgs) if lgs else None
        base_b = ref.get("stored_final_brier")
        print(f"  v2_clean    Brier {v2s['brier']:.4f}  命中 {v2s['hit_rate']:.1%}  n={v2s['n']}")
        if lgs:
            print(f"  legacy      Brier {lgs['brier']:.4f}  命中 {lgs['hit_rate']:.1%}  n={lgs['n']}")
        if base_b is not None:
            print(f"  冻结基线    Brier {base_b:.4f}")
        if pv:
            print(f"  vs 旧链     ΔBrier {pv['delta_brier']:+.4f} (t={pv['t']:+.2f})")
        better = base_b is not None and v2s["brier"] < base_b - 0.005
        if pv is None:
            verdict = "IMPROVED" if better else ("REGRESSED" if base_b is not None and v2s["brier"] > base_b + 0.005 else "NEUTRAL")
        else:
            verdict = "IMPROVED" if (pv["delta_brier"] > 0.005 and pv["t"] > 1.5) else \
                      ("REGRESSED" if (pv["delta_brier"] < -0.005 and pv["t"] < -1.5) else "NEUTRAL")
        report["verdict"] = verdict
        report["paired_vs_legacy"] = pv
        print(f"  判定: {verdict}   （仅表示换链没搞坏，不表示系统有 edge）")

    print("\n--- Q2 真实价值：v2 链 vs 同批去水市场（配对）---")
    print(f"  配对样本 n={pvm.get('n', 0)}")
    if pvm.get("n", 0) >= 2:
        print(f"  final  Brier {pvm['brier_final']:.4f}")
        print(f"  market Brier {pvm['brier_market']:.4f}")
        print(f"  Δ={pvm['delta_brier']:+.4f}  t={pvm['t']:+.2f}  "
              f"95%CI [{pvm['ci95_delta'][0]:+.4f}, {pvm['ci95_delta'][1]:+.4f}]")
        print(f"  逐场：模型更好 {pvm['matches_better']} / 市场更好 {pvm['matches_worse']}")
        print(f"  判定: {pvm['verdict']}"
              f"{'（显著）' if pvm['significant'] else '（不显著 = 无证据表明有 edge）'}")
    report["paired_vs_market"] = pvm
    report["verdict_vs_market"] = pvm.get("verdict")

    ew = report["effective_fusion_weights"]
    if ew.get("effective"):
        e = ew["effective"]
        print(f"\n--- 运行时有效融合权重 ---\n  model {e['model']:.2%} / "
              f"market {e['market']:.2%} / djyy {e['djyy']:.2%}")
        if ew.get("note"):
            print(f"  ⚠ {ew['note']}")

    print("\n" + "=" * 70)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"✓ 已写入 {OUT.relative_to(ROOT)} (schema v{SCHEMA_VERSION})")


if __name__ == "__main__":
    main()
