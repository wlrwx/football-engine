#!/usr/bin/env python3
"""候选投注闸门实验（2026-08-29）

在已结算账本上，以 B 新包（champion 权重 + 消融后开关）为基线，
重放三个候选方向/仓位闸门，按与 upgrade_acceptance 相同的治理闸
（全样本 + val 段双过 t≥1.96；0/1 命中差异另报 McNemar 精确检验）：

  E 反向全让   final argmax 与市场 argmax 不一致时跟随市场
  E2 反向有条件让 仅当 final 前二差距 < margin 时跟随市场
  F 禁选平局   final 选平且市场不选平 → 改选主/客中概率更高者
  G 热门区全免 赔率<1.8 注量系数 0.5 → 0.0（账本 pnl 确定性核算）

只读，不写状态。用法: python3 scripts/gate_experiment.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ablation_replay as ar  # noqa: E402
from engine.prediction.fusion import DEFAULT_POST_FUSION  # noqa: E402

VAL_FRAC = 0.4
FAVORITE_ODDS = 1.8  # 与 engine/strategy/kelly.py favorite_band_factor 一致


def switches_new():
    on = {"same_odds_bias", "combo_boost", "market_draw_pull",
          "league_draw_baseline", "league_draw_anchor"}
    return {k: (k in on) for k in DEFAULT_POST_FUSION}


def load_champion_cfg():
    champ = json.loads((ROOT / "data/state/fusion_weights.json").read_text(encoding="utf-8"))["champion"]
    cfg = dict(ar.load_cfg())
    cfg["model_weight"] = champ["model"]
    cfg["market_weight"] = champ["market"]
    cfg["djyy_weight"] = champ["djyy"]
    return cfg


def gate_pick(probs, gate, market_am, margin=0.05):
    """在融合概率上应用方向闸门，返回 pick idx。"""
    pick = max(range(3), key=lambda i: probs[i])
    if gate == "B":
        return pick
    if gate == "E" and market_am is not None and pick != market_am:
        return market_am
    if gate == "E2" and market_am is not None and pick != market_am:
        others = [probs[i] for i in range(3) if i != pick]
        if max(others) - min(others) < margin:
            return market_am
    if gate == "F" and pick == 1 and market_am is not None and market_am != 1:
        return market_am
    return pick


def mcnemar(b_correct, g_correct):
    """非平局配对（仅两臂结论不同的场次），二项精确检验。"""
    b_only = sum(1 for b, g in zip(b_correct, g_correct) if b and not g)
    g_only = sum(1 for b, g in zip(b_correct, g_correct) if g and not b)
    n = b_only + g_only
    if n == 0:
        return 0, 0, 1.0
    k = min(b_only, g_only)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) * 2 / (2 ** n)
    return b_only, g_only, min(1.0, p)


def run(recs, feats, cfg, sw, gate, market_am_map):
    hits = []
    picks_log = []
    for r in recs:
        probs = ar.fuse_probabilities(ar.build_input(r, feats, cfg, sw)).probs
        m = market_am_map.get(r["match_id"])
        pick = gate_pick(probs, gate, m)
        hit = int(pick == r["actual_idx"])
        hits.append(hit)
        picks_log.append((r["match_id"], pick))
    return hits, picks_log


def paired_t(diffs):
    n = len(diffs)
    if not n:
        return 0.0
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / max(1, n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    return mean / se if se > 0 else 0.0


def main() -> int:
    recs = ar.load_ledger()
    feats = ar.load_daily_features()
    cfg = load_champion_cfg()
    sw = switches_new()
    split = int(len(recs) * (1 - VAL_FRAC))
    market_am = {r["match_id"]: max(range(3), key=lambda i: r["market_fair"][i])
                 for r in recs if r.get("market_fair")}

    base_hits, _ = run(recs, feats, cfg, sw, "B", market_am)
    print(f"账本 {len(recs)} 场（val=后 {len(recs)-split} 场）\n")
    print(f"{'闸门':<26}{'全命中':>8}{'Δpp':>7}{'t':>7}{'McNemar p':>10}   "
          f"{'val命中':>8}{'Δpp':>7}{'t':>7}{'McNemar p':>10}")

    for gate in ("E", "E2", "F"):
        g_hits, _ = run(recs, feats, cfg, sw, gate, market_am)
        line = f"{gate:<26}"
        for seg in (slice(0, len(recs)), slice(split, len(recs))):
            b, g = base_hits[seg], g_hits[seg]
            diffs = [gi - bi for bi, gi in zip(b, g)]
            bo, go, p = mcnemar(b, g)
            line += (f"{sum(g)/len(g):>8.1%}{(sum(g)-sum(b))/len(g)*100:>7.1f}"
                     f"{paired_t(diffs):>7.2f}{p:>10.3f}   ")
        print(line)

    # G 热门区全免：pnl 确定性核算（×0.5 已上线，比较 ×0 的增量）
    fav = [r for r in recs if r.get("best_selection")
           and isinstance(r["best_selection"], dict)
           and r["best_selection"].get("odds") is not None
           and r["best_selection"]["odds"] < FAVORITE_ODDS]
    if not fav:
        # 回退：用 odds_band 字段近似（1.0-1.5 档）
        fav = [r for r in recs if r.get("odds_band") == "1.0-1.5"]
    pnl_now = sum((r.get("pnl") or 0) * 0.5 for r in fav)
    pnl_zero = 0.0
    n_fav = len(fav)
    fav_hits = sum(int(r.get("hit")) for r in fav if r.get("hit") is not None)
    print(f"\nG 热门区(<{FAVORITE_ODDS}) 共 {n_fav} 场，命中 {fav_hits}"
          f"（{fav_hits/n_fav:.1%} if n else '-'），账本 pnl 合计 "
          f"{sum(r.get('pnl') or 0 for r in fav):+.1f}")
    print(f"  现行 ×0.5 注量贡献 {pnl_now:+.1f} → 全免后 {pnl_zero:+.1f}"
          f"（Δ={-pnl_now:+.1f}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
