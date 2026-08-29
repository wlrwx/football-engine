#!/usr/bin/env python3
"""升级验收：多场景重放对比（2026-08-29）

在已结算账本上对比四个配置臂，量化"组合拳升级"的净效应：
  A baseline  旧冠军权重(0.10/0.75/0.15) + 可回放开关全开   ← 升级前生产行为
  B 新包      新冠军权重(0.05/0.75/0.20) + 消融裁决后开关    ← 工作区待验收
  C 权重最优  weight_search 最优(0/0.60/0.15≈市场0.8/djyy0.2) + 同 B 开关
  D 纯市场影子 model=0/djyy=0 + 全关                      ← 命中率天花板参照

只读，不写状态。用法: python3 scripts/upgrade_acceptance.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ablation_replay as ar  # noqa: E402
from engine.prediction.fusion import DEFAULT_POST_FUSION  # noqa: E402

VAL_FRAC = 0.4  # 与 ablation_replay 的 60/40 切分一致


def switches_of(on_names):
    return {k: (k in on_names) for k in DEFAULT_POST_FUSION}


def pick_stats(recs, feats, cfg, sw, market_argmax):
    """选向层统计：反向场数、平局选择、投影命中"""
    draw_picks = draw_hits = 0
    against = against_hits = 0
    hits = 0
    n = 0
    for r in recs:
        arm = ar.fuse_probabilities(ar.build_input(r, feats, cfg, sw)).probs
        pick = max(range(3), key=lambda i: arm[i])
        n += 1
        hits += int(pick == r["actual_idx"])
        m = market_argmax.get(r["match_id"])
        if m is not None:
            if pick != m:
                against += 1
                against_hits += int(pick == r["actual_idx"])
        if pick == 1:
            draw_picks += 1
            draw_hits += int(pick == r["actual_idx"])
    return {
        "n": n,
        "hit": hits / n,
        "against_market": against,
        "against_hit": against_hits / against if against else 0.0,
        "draw_picks": draw_picks,
        "draw_hit": draw_hits / draw_picks if draw_picks else 0.0,
    }


def main() -> int:
    recs = ar.load_ledger()
    feats = ar.load_daily_features()
    split = int(len(recs) * (1 - VAL_FRAC))
    val = recs[split:]

    champ_new = json.loads((ROOT / "data/state/fusion_weights.json").read_text(encoding="utf-8"))["champion"]
    cfg_old = {"model_weight": 0.10, "market_weight": 0.75, "djyy_weight": 0.15,
               "lgbm_weight": 0.10, "combo_boost_cap": 0.03, "same_odds_min_confidence": 0.3,
               "same_odds_max_adjust": 0.05, "djyy_min_confidence": 0.5, "djyy_disagree_penalty": 0.5}
    cfg_new = {**cfg_old, "model_weight": champ_new["model"], "market_weight": champ_new["market"],
               "djyy_weight": champ_new["djyy"]}
    cfg_best = {**cfg_old, "model_weight": 0.0, "market_weight": 0.60, "djyy_weight": 0.15}
    cfg_pure = {**cfg_old, "model_weight": 0.0, "market_weight": 1.0, "djyy_weight": 0.0}

    sw_all_on = switches_of(set(ar.REPLAYABLE))
    sw_new = switches_of({"same_odds_bias", "combo_boost", "market_draw_pull",
                          "league_draw_baseline", "league_draw_anchor"})
    sw_off = switches_of(set())

    arms = [
        ("A_旧配置(升级前)", cfg_old, sw_all_on),
        ("B_新包(工作区)", cfg_new, sw_new),
        ("C_权重最优", cfg_best, sw_new),
        ("D_纯市场影子", cfg_pure, sw_off),
    ]

    market_argmax = {r["match_id"]: max(range(3), key=lambda i: r["market_fair"][i])
                     for r in recs if r.get("market_fair")}

    print(f"账本 {len(recs)} 场（val=后 {len(val)} 场，时间切分）\n")
    print(f"{'臂':<18}{'全Brier':>9}{'全命中':>8}{'valBrier':>9}{'val命中':>8}"
          f"{'反向场':>7}{'反向命中':>8}{'选平':>5}{'平命中':>7}")
    results = {}
    base_full = base_val = None
    for name, cfg, sw in arms:
        f = ar.evaluate(recs, feats, cfg, sw)
        v = ar.evaluate(val, feats, cfg, sw)
        ps = pick_stats(recs, feats, cfg, sw, market_argmax)
        results[name] = (f, v, ps)
        if name.startswith("A"):
            base_full, base_val = f, v
        print(f"{name:<18}{f['brier']:>9.4f}{f['hit_rate']:>8.1%}{v['brier']:>9.4f}{v['hit_rate']:>8.1%}"
              f"{ps['against_market']:>7}{ps['against_hit']:>8.1%}{ps['draw_picks']:>5}{ps['draw_hit']:>7.1%}")

    print("\n--- 配对显著性（相对 A 旧配置，ΔBrier>0 = 更好） ---")
    for name, cfg, sw in arms[1:]:
        f = ar.evaluate(recs, feats, cfg, sw, ref_switches=sw_all_on, ref_cfg=cfg_old)
        v = ar.evaluate(val, feats, cfg, sw, ref_switches=sw_all_on, ref_cfg=cfg_old)
        fs, vs = ar.paired_stats(f["diffs"]), ar.paired_stats(v["diffs"])
        print(f"{name:<18} 全: Δ={fs['mean']:+.5f} t={fs['t']:+.2f}   val: Δ={vs['mean']:+.5f} t={vs['t']:+.2f}")

    bf, bv = base_full["hit_rate"], base_val["hit_rate"]
    print(f"\n基线命中: 全 {bf:.1%} / val {bv:.1%}")
    for name in results:
        f, v, _ = results[name]
        print(f"{name}: 命中变化 全 {f['hit_rate']-bf:+.1%} / val {v['hit_rate']-bv:+.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
