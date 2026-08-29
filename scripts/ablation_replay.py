#!/usr/bin/env python3
"""融合链消融回放（2026-08-29）

用已结算账本（review_ledger.jsonl）+ 每日落盘的 predictions.json，
对融合后处理链逐开关做反事实回放，用配对 Brier 裁决每个开关去留。

方法：
  1. 账本提供 model_raw / market_fair / djyy_prob / actual_idx（预测时冻结值）
  2. data/daily/<date>/predictions.json 补齐 sina 水位、同赔偏差、combo（预测时冻结值）
  3. 用与生产完全相同的 fuse_probabilities（engine/prediction/fusion.py）重放
  4. 不可回放项（lgbm 掺混、isotonic/temperature、新鲜度）保持关闭——
     所有对比臂同等保真度，配对差异仍然有效；与 stored_final 的差记为"残差"
  5. 时间顺序验证：前 60% 定参，后 40% 验证（防过拟合）

用法: python3 scripts/ablation_replay.py [--out data/state/ablation_report.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.pipeline.helpers import _canon_league  # noqa: E402
from engine.prediction.fusion import (  # noqa: E402
    DEFAULT_POST_FUSION,
    DRAW_ANCHOR_W,
    FusionInput,
    LEAGUE_DRAW_ANCHOR,
    fuse_probabilities,
)

REPLAYABLE = [k for k in DEFAULT_POST_FUSION if k not in ("lgbm_blend", "isotonic", "temperature", "freshness")]


class _SameOddsStub:
    def __init__(self, confidence, biases):
        self.confidence = confidence
        self.home_bias, self.draw_bias, self.away_bias = biases


def brier(probs, actual_idx):
    return sum((p - (1.0 if i == actual_idx else 0.0)) ** 2 for i, p in enumerate(probs))


def load_ledger():
    path = ROOT / "data/state/review_ledger.jsonl"
    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("final_prob") and r.get("actual_idx") is not None:
            recs.append(r)
    recs.sort(key=lambda r: (r.get("date", ""), r.get("match_id", "")))
    return recs


def load_daily_features():
    """match_id → {sina, same_odds, combo}，来自每日 predictions.json（预测时冻结值）"""
    feats = {}
    daily_root = ROOT / "data/daily"
    if not daily_root.exists():
        return feats
    for d in sorted(daily_root.iterdir()):
        f = d / "predictions.json"
        if not f.exists():
            continue
        try:
            for m in json.loads(f.read_text(encoding="utf-8")):
                mid = m.get("match_id")
                if not mid:
                    continue
                sina = m.get("sina_odds") or {}
                feats[mid] = {
                    "sina": {"movement": sina.get("movement"), "compression": sina.get("compression") or {}},
                    "same_odds": (
                        _SameOddsStub(m.get("same_odds_confidence") or 0, m.get("same_odds_bias") or [0, 0, 0])
                        if m.get("same_odds_matched")
                        else None
                    ),
                    "combo": m.get("combo_boost") or 0.0,
                }
        except Exception:
            continue
    return feats


def load_league_params():
    path = ROOT / "data/state/league_params.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    leagues = data.get("leagues", data) if isinstance(data, dict) else {}

    def draw_baseline(lg):
        v = leagues.get(_canon_league(lg), {}) if isinstance(leagues, dict) else {}
        return float(v.get("draw_baseline", 0.25)) if isinstance(v, dict) else 0.25

    def draw_strength(lg):
        v = leagues.get(_canon_league(lg), {}) if isinstance(leagues, dict) else {}
        return float(v.get("draw_strength", 0.0)) if isinstance(v, dict) else 0.0

    return draw_baseline, draw_strength


def load_cfg():
    pred = json.loads((ROOT / "config/prediction.json").read_text(encoding="utf-8"))
    cfg = dict(pred.get("fusion", {}))
    try:
        champ = json.loads((ROOT / "data/state/fusion_weights.json").read_text(encoding="utf-8"))["champion"]
        cfg["model_weight"] = champ["model"]
        cfg["market_weight"] = champ["market"]
        cfg["djyy_weight"] = champ["djyy"]
    except Exception:
        pass
    return cfg


def build_input(r, feats, cfg, switches):
    mid = r.get("match_id")
    f = feats.get(mid, {})
    model = r.get("model_raw") or [1 / 3, 1 / 3, 1 / 3]
    market = tuple(r["market_fair"]) if r.get("market_fair") else None
    djyy = r.get("djyy_prob")
    if isinstance(djyy, list):
        djyy = {"home": djyy[0], "draw": djyy[1], "away": djyy[2]}
    lg = r.get("league", "")
    return FusionInput(
        model_probs=tuple(model),
        market_probs=market,
        djyy_probs=djyy,
        cfg=cfg,
        same_odds=f.get("same_odds"),
        combo_boost=f.get("combo", 0.0),
        sina_data=f.get("sina"),
        league_draw_baseline=LB(lg),
        league_draw_strength=LS(lg),
        draw_anchor=LEAGUE_DRAW_ANCHOR.get(_canon_league(lg)),
        draw_anchor_w=DRAW_ANCHOR_W,
        post_fusion=switches,
    )


LB, LS = load_league_params()


def evaluate(recs, feats, cfg, switches, ref_switches=None, ref_cfg=None):
    """返回 (n, brier, hit, paired_diffs)。paired_diffs 相对 ref（i 的 ref_brier - arm_brier，>0=arm 更好）"""
    diffs, bs, hits = [], [], 0
    for r in recs:
        arm = fuse_probabilities(build_input(r, feats, cfg, switches)).probs
        b_arm = brier(arm, r["actual_idx"])
        hits += int(max(range(3), key=lambda i: arm[i]) == r["actual_idx"])
        bs.append(b_arm)
        if ref_switches is not None:
            ref = fuse_probabilities(build_input(r, feats, ref_cfg or cfg, ref_switches)).probs
            diffs.append(brier(ref, r["actual_idx"]) - b_arm)
    n = len(bs)
    return {
        "n": n,
        "brier": sum(bs) / n,
        "hit_rate": hits / n,
        "diffs": diffs,
    }


def paired_stats(diffs):
    if not diffs:
        return {"mean": 0.0, "se": 0.0, "t": 0.0, "n": 0}
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / max(1, n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    return {"mean": mean, "se": se, "t": mean / se if se > 0 else 0.0, "n": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/state/ablation_report.json")
    ap.add_argument("--train-frac", type=float, default=0.6)
    args = ap.parse_args()

    recs = load_ledger()
    feats = load_daily_features()
    cfg = load_cfg()
    join_rate = sum(1 for r in recs if r.get("match_id") in feats) / len(recs)

    split = int(len(recs) * args.train_frac)
    train, val = recs[:split], recs[split:]

    print(f"账本 {len(recs)} 场 (train {len(train)} / val {len(val)}), daily特征 join率 {join_rate:.0%}")
    print(f"权重: model={cfg['model_weight']:.2f} market={cfg['market_weight']:.2f} djyy={cfg['djyy_weight']:.2f}\n")

    # --- 参照系 ---
    all_on = {k: (k in REPLAYABLE) for k in DEFAULT_POST_FUSION}
    all_off = {k: False for k in DEFAULT_POST_FUSION}

    # 存储的 final_prob（生产真值）
    stored_b = [brier(r["final_prob"], r["actual_idx"]) for r in recs]
    stored_hit = sum(
        int(max(range(3), key=lambda i: r["final_prob"][i]) == r["actual_idx"]) for r in recs
    )
    # 纯市场
    mkt = [r for r in recs if r.get("market_fair")]
    mkt_b = sum(brier(r["market_fair"], r["actual_idx"]) for r in mkt) / len(mkt)
    mkt_hit = sum(
        int(max(range(3), key=lambda i: r["market_fair"][i]) == r["actual_idx"]) for r in mkt
    ) / len(mkt)

    replay_on = evaluate(recs, feats, cfg, all_on)
    pure = evaluate(recs, feats, cfg, all_off)

    print("=== 参照系（全样本） ===")
    print(f"{'臂':<28}{'n':>5}{'Brier':>9}{'命中率':>8}")
    print(f"{'存储 final_prob(生产)':<26}{len(recs):>5}{sum(stored_b)/len(recs):>9.4f}{stored_hit/len(recs):>8.1%}")
    print(f"{'纯市场(去水)':<26}{len(mkt):>5}{mkt_b:>9.4f}{mkt_hit:>8.1%}")
    print(f"{'回放-可回放开关全开':<26}{replay_on['n']:>5}{replay_on['brier']:>9.4f}{replay_on['hit_rate']:>8.1%}")
    print(f"{'回放-纯融合(全关)':<26}{pure['n']:>5}{pure['brier']:>9.4f}{pure['hit_rate']:>8.1%}")
    print(f"  ↳ 残差(存储final − 回放全开) = {sum(stored_b)/len(recs) - replay_on['brier']:+.4f} "
          f"= lgbm+校准+新鲜度的净效应（不可回放）\n")

    # --- 逐开关消融（相对"可回放全开"，全样本 + 验证段） ---
    print("=== 逐开关消融：关掉该步的边际效应（配对 ΔBrier>0 = 关掉更好） ===")
    print(f"{'开关':<24}{'全样本Δ':>9}{'t':>7}   {'验证段Δ':>9}{'t':>7}   建议")
    ablation = {}
    for step in REPLAYABLE:
        off = dict(all_on)
        off[step] = False
        full = evaluate(recs, feats, cfg, off, ref_switches=all_on)
        v = evaluate(val, feats, cfg, off, ref_switches=all_on)
        fs, vs = paired_stats(full["diffs"]), paired_stats(v["diffs"])
        verdict = "关" if fs["mean"] > 0 and fs["t"] > 1.0 else ("留" if fs["mean"] < 0 else "中性")
        print(f"{step:<24}{fs['mean']:>+9.4f}{fs['t']:>7.1f}   {vs['mean']:>+9.4f}{vs['t']:>7.1f}   {verdict}")
        ablation[step] = {
            "full": {"delta_brier": fs["mean"], "t": fs["t"], "n": fs["n"], "verdict": verdict},
            "val": {"delta_brier": vs["mean"], "t": vs["t"], "n": vs["n"]},
        }

    # --- 候选配置 vs 现行（验证段配对） ---
    print("\n=== 候选配置（验证段，配对 ΔBrier>0 = 候选更好） ===")
    candidates = {
        "A_纯融合+校准残差留白": all_off,
        "B_只关平局工程三项": {**all_on, "league_draw_anchor": False, "league_draw_baseline": False, "market_draw_pull": False},
        "C_B再关水位概率修正": {**all_on, "league_draw_anchor": False, "league_draw_baseline": False,
                                "market_draw_pull": False, "sina_odds_movement": False},
    }
    cand_results = {}
    for name, sw in candidates.items():
        v = evaluate(val, feats, cfg, sw, ref_switches=all_on)
        vs = paired_stats(v["diffs"])
        cand_results[name] = {"val_delta_brier": vs["mean"], "t": vs["t"], "n": vs["n"]}
        print(f"{name:<28} Δ={vs['mean']:+.4f}  t={vs['t']:.1f}")

    # --- 权重网格（纯融合，train 定 / val 验） ---
    print("\n=== 权重网格搜索（纯融合，train 段选优 → val 段配对验证 vs 现行 champion） ===")
    grid = []
    for wm in [0.0, 0.05, 0.10, 0.15, 0.20]:
        for wk in [0.60, 0.65, 0.70, 0.75, 0.80]:
            for wd in [0.0, 0.10, 0.15, 0.20, 0.30]:
                if wm + wk + wd == 0:
                    continue
                grid.append((wm, wk, wd))
    best = None
    for wm, wk, wd in grid:
        c = {**cfg, "model_weight": wm, "market_weight": wk, "djyy_weight": wd}
        t = evaluate(train, feats, c, all_off)
        if best is None or t["brier"] < best[1]:
            best = ((wm, wk, wd), t["brier"])
    print(f"train 段最优: model={best[0][0]:.2f} market={best[0][1]:.2f} djyy={best[0][2]:.2f}  Brier={best[1]:.4f}")
    c_best = {**cfg, "model_weight": best[0][0], "market_weight": best[0][1], "djyy_weight": best[0][2]}
    v_best = evaluate(val, feats, c_best, all_off, ref_switches=all_off, ref_cfg=cfg)
    vbs = paired_stats(v_best["diffs"])
    print(f"val 段配对 vs 现行权重(纯融合): Δ={vbs['mean']:+.4f}  t={vbs['t']:.1f}  n={vbs['n']}")

    report = {
        "generated_at": str(Path(args.out).stat().st_mtime) if Path(args.out).exists() else "",
        "meta": {
            "records": len(recs), "train": len(train), "val": len(val),
            "feature_join_rate": round(join_rate, 3),
            "weights": {k: cfg[k] for k in ("model_weight", "market_weight", "djyy_weight")},
        },
        "reference": {
            "stored_final_brier": sum(stored_b) / len(recs),
            "stored_final_hit": stored_hit / len(recs),
            "market_brier": mkt_b, "market_hit": mkt_hit, "market_n": len(mkt),
            "replay_on_brier": replay_on["brier"], "replay_on_hit": replay_on["hit_rate"],
            "pure_fusion_brier": pure["brier"], "pure_fusion_hit": pure["hit_rate"],
        },
        "ablation": ablation,
        "candidates": cand_results,
        "weight_search": {
            "best_weights": {"model": best[0][0], "market": best[0][1], "djyy": best[0][2]},
            "train_brier": best[1],
            "val_paired_vs_current": {"delta": vbs["mean"], "t": vbs["t"], "n": vbs["n"]},
        },
    }
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 报告已写入 {out_path}")


if __name__ == "__main__":
    main()
