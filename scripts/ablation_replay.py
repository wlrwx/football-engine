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

from engine.beijing_time import beijing_now  # noqa: E402
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


def logloss(probs, actual_idx):
    p = max(1e-15, min(1.0 - 1e-15, float(probs[actual_idx])))
    return -math.log(p)


def evaluate_fusion_modes(recs, feats, cfg, switches, val):
    """配对对比线性融合 (linear) 与对数赔率池化 (logit) 的样本内/外表现。"""
    lin_cfg = {**cfg, "fusion_mode": "linear"}
    log_cfg = {**cfg, "fusion_mode": "logit"}

    def _run_pair(subset):
        b_diffs, ll_diffs = [], []
        b_lin, b_log, ll_lin, ll_log = [], [], [], []
        for r in subset:
            p_lin = fuse_probabilities(build_input(r, feats, lin_cfg, switches)).probs
            p_log = fuse_probabilities(build_input(r, feats, log_cfg, switches)).probs
            act = r["actual_idx"]
            bl1, bl2 = brier(p_lin, act), brier(p_log, act)
            l1, l2 = logloss(p_lin, act), logloss(p_log, act)
            b_lin.append(bl1)
            b_log.append(bl2)
            ll_lin.append(l1)
            ll_log.append(l2)
            # Δ = logit - linear (< 0 表示 logit 更好)
            b_diffs.append(bl2 - bl1)
            ll_diffs.append(l2 - l1)
        bs = paired_stats(b_diffs)
        ls = paired_stats(ll_diffs)
        n = len(subset)
        return {
            "n": n,
            "linear_brier": sum(b_lin) / n if n else 0.0,
            "logit_brier": sum(b_log) / n if n else 0.0,
            "delta_brier": bs["mean"],
            "t_brier": bs["t"],
            "linear_logloss": sum(ll_lin) / n if n else 0.0,
            "logit_logloss": sum(ll_log) / n if n else 0.0,
            "delta_logloss": ls["mean"],
            "t_logloss": ls["t"],
        }

    return {"full": _run_pair(recs), "val": _run_pair(val)}


def evaluate_temperature_scaling(train, val):
    """仅用 train 段拟合 TemperatureScaler，并在 val 段评估样本外配对 ΔBrier / ΔLogLoss。"""
    import tempfile
    import numpy as np
    from engine.prediction.temperature_scaling import TemperatureScaler

    if len(train) < 30 or not val:
        return {"n_train": len(train), "n_val": len(val), "fitted": False}
    with tempfile.TemporaryDirectory(prefix="abl_temp_") as tmpdir:
        scaler = TemperatureScaler(Path(tmpdir) / "temp.json")
        scaler.fit(
            np.array([r["final_prob"] for r in train], dtype=float),
            np.array([r["actual_idx"] for r in train], dtype=int),
        )
        b_diffs, ll_diffs = [], []
        b_raw, b_cal, ll_raw, ll_cal = [], [], [], []
        for r in val:
            raw = tuple(r["final_prob"])
            cal = scaler.calibrate(raw)
            act = r["actual_idx"]
            br, bc = brier(raw, act), brier(cal, act)
            lr, lc = logloss(raw, act), logloss(cal, act)
            b_raw.append(br)
            b_cal.append(bc)
            ll_raw.append(lr)
            ll_cal.append(lc)
            b_diffs.append(bc - br)
            ll_diffs.append(lc - lr)
    bs = paired_stats(b_diffs)
    ls = paired_stats(ll_diffs)
    nv = len(val)
    return {
        "fitted": scaler._fitted,
        "temperature": round(float(scaler.temperature), 6),
        "n_train": len(train),
        "n_val": nv,
        "val_brier_before": sum(b_raw) / nv,
        "val_brier_after": sum(b_cal) / nv,
        "delta_brier": bs["mean"],
        "t_brier": bs["t"],
        "val_logloss_before": sum(ll_raw) / nv,
        "val_logloss_after": sum(ll_cal) / nv,
        "delta_logloss": ls["mean"],
        "t_logloss": ls["t"],
    }


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

    # 【2026-10-09】账本跨两代融合链：v1（chain 缺失，2026-07-20~08-28，425 场）
    # 与 v2（2026-08-29 起，概率层重构）。用今天的代码重放 v1 行会带系统性偏差：
    #   混放两链 replay vs 生产 final 平均平方偏差 0.018（Brier 量级 6%）
    #   仅 v2                                      0.0001
    # 旧代码不按 chain 过滤，把 6% 的测量误差混进每次消融的配对检验。
    # 现在只留当前链，缺失 chain 的旧行不再参与重放（数据已成历史，不可回头）。
    _v2 = [r for r in recs if r.get("chain") == "v2"]
    # 污染样本隔离（2026-10-11）：v2 链里有 20 场已修 bug 的残留
    #（league_draw_baseline 把巴甲平局概率抬到 0.5025）。回放的目的是
    # "每个开关贡献多少"，带着被污染的样本回放等于在测一个假基线。
    try:
        from engine.integrity.quarantine import clean_records as _qfilter
        chain_records = _qfilter(_v2)
    except Exception:
        chain_records = _v2
    n_old = len(recs) - len(_v2)
    if n_old:
        print(f"[ablation_replay] 已排除 {n_old}/{len(recs)} 场旧链（chain!=v2）行，避免重放偏差")
    n_bad = len(_v2) - len(chain_records)
    if n_bad:
        print(f"[ablation_replay] 已排除 {n_bad} 场污染样本（融合链平局概率越界），"
              f"剩余 {len(chain_records)} 场")
    return chain_records


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


def reweight_model(cfg: dict, w: float) -> dict:
    """把模型槽设为 w，其余源（市场/DJYY）按原比例分掉 1-w。

    单变量扫描必须保持非模型源的相对比例，否则"归零模型"同时改变了
    市场/DJYY 的内部配比，测到的就不是单一的 model_weight 效应。
    """
    market = float(cfg["market_weight"])
    djyy = float(cfg["djyy_weight"])
    if abs(w - float(cfg["model_weight"])) < 1e-12:
        return dict(cfg)  # 同一权重必须逐位相同，否则浮点重分配会造出假的 t 值
    base = market + djyy
    rest = max(0.0, 1.0 - w)
    return {
        **cfg,
        "model_weight": w,
        "market_weight": rest * market / base if base > 0 else rest,
        "djyy_weight": rest * djyy / base if base > 0 else 0.0,
    }


def direction_change_rate(recs, feats, cfg_a, cfg_b, switches):
    """两套权重下 argmax 方向不一致的场次占比（归零影响的"可见面"）。"""
    changed = 0
    for r in recs:
        a = fuse_probabilities(build_input(r, feats, cfg_a, switches)).probs
        b = fuse_probabilities(build_input(r, feats, cfg_b, switches)).probs
        if max(range(3), key=lambda i: a[i]) != max(range(3), key=lambda i: b[i]):
            changed += 1
    return changed / len(recs) if recs else 0.0


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

    # --- 模型权重单变量扫描（2026-10-28）---
    # 目的：回答"把 model_weight 归零会不会更好"。与上面的网格搜索的区别：
    #   1. 用**生产**后处理开关（网格用的是纯融合 all_off）
    #   2. 单变量：market/djyy 按原比例分掉剩余权重，不混入内部配比变化
    #   3. 报告配对 Δ 的 95%CI、验证段结果，以及 argmax 方向变化率（归零的"可见面"）
    print("\n=== 模型权重单变量扫描（生产后处理开关，ΔBrier<0 = 比 champion 好） ===")
    prod_switches = {
        k: bool(cfg.get("post_fusion", {}).get(k, False))
        for k in DEFAULT_POST_FUSION
    }
    champ_full = evaluate(recs, feats, cfg, prod_switches)
    print(f"champion: model={cfg['model_weight']:.2f} market={cfg['market_weight']:.2f} "
          f"djyy={cfg['djyy_weight']:.2f}  Brier={champ_full['brier']:.4f}  "
          f"命中={champ_full['hit_rate']:.1%}  n={champ_full['n']}")
    print(f"{'w_model':>8}{'Brier':>9}{'命中':>8}{'Δ全样本':>10}{'t':>7}{'Δ验证段':>10}{'t':>7}{'方向变化':>9}")
    sweep = []
    for w in (0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10):
        c = reweight_model(cfg, w)
        full = evaluate(recs, feats, c, prod_switches,
                        ref_switches=prod_switches, ref_cfg=cfg)
        v = evaluate(val, feats, c, prod_switches,
                     ref_switches=prod_switches, ref_cfg=cfg)
        fs, vst = paired_stats(full["diffs"]), paired_stats(v["diffs"])
        # diffs = ref − arm（正 = arm 更好）；报告按项目惯例 Δ = arm − ref（负 = 更好）
        delta, delta_val = -fs["mean"], -vst["mean"]
        ci = 1.96 * fs["se"]
        churn = direction_change_rate(recs, feats, c, cfg, prod_switches)
        print(f"{w:>8.2f}{full['brier']:>9.4f}{full['hit_rate']:>8.1%}"
              f"{delta:>+10.4f}{fs['t']:>7.1f}{delta_val:>+10.4f}{vst['t']:>7.1f}{churn:>9.1%}")
        sweep.append({
            "model_weight": w,
            "market_weight": round(c["market_weight"], 4),
            "djyy_weight": round(c["djyy_weight"], 4),
            "brier": full["brier"], "hit_rate": full["hit_rate"],
            "delta_vs_champion": delta, "t": fs["t"],
            "ci95": [delta - ci, delta + ci],
            "val_delta": delta_val, "val_t": vst["t"],
            "direction_change_rate": churn, "n": full["n"],
        })

    # 纯市场臂：不走比例重分配（市场 100%），作为"归零"的下限参照
    pure_mkt_cfg = {**cfg, "model_weight": 0.0, "market_weight": 1.0, "djyy_weight": 0.0}
    pm = evaluate(recs, feats, pure_mkt_cfg, prod_switches,
                  ref_switches=prod_switches, ref_cfg=cfg)
    pms = paired_stats(pm["diffs"])
    print(f"{'纯市场':>8}{pm['brier']:>9.4f}{pm['hit_rate']:>8.1%}"
          f"{-pms['mean']:>+10.4f}{pms['t']:>7.1f}")
    sweep_pure_market = {
        "brier": pm["brier"], "hit_rate": pm["hit_rate"],
        "delta_vs_champion": -pms["mean"], "t": pms["t"], "n": pm["n"],
    }

    # --- 融合模式对比（linear vs logit） & 温度缩放样本外验证 ---
    fusion_mode_cmp = evaluate_fusion_modes(recs, feats, cfg, prod_switches, val)
    print("\n=== 融合模式 OOS 对比 (logit - linear, 负数=logit更好) ===")
    for split_name in ("full", "val"):
        fm = fusion_mode_cmp[split_name]
        print(f"  [{split_name:<4}] n={fm['n']} ΔBrier={fm['delta_brier']:+.5f} (t={fm['t_brier']:+.2f}) "
              f"ΔLogLoss={fm['delta_logloss']:+.5f} (t={fm['t_logloss']:+.2f})")

    temp_oos = evaluate_temperature_scaling(train, val)
    print("\n=== Temperature Scaling OOS 验证 (train 拟合 → val 评估, 负数=更好) ===")
    if temp_oos.get("fitted"):
        print(f"  T={temp_oos['temperature']:.4f} n_val={temp_oos['n_val']} "
              f"ΔBrier={temp_oos['delta_brier']:+.5f} (t={temp_oos['t_brier']:+.2f}) "
              f"ΔLogLoss={temp_oos['delta_logloss']:+.5f} (t={temp_oos['t_logloss']:+.2f})")

    report = {
        # 2026-10-28 修：原实现写的是**上一次**报告文件的 mtime，不是本次生成时间。
        "generated_at": beijing_now().isoformat(timespec="seconds"),
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
        "model_weight_sweep": {
            "switches": "production",
            "champion": {
                "model": cfg["model_weight"], "market": cfg["market_weight"],
                "djyy": cfg["djyy_weight"],
                "brier": champ_full["brier"], "hit_rate": champ_full["hit_rate"],
            },
            "arms": sweep,
            "pure_market": sweep_pure_market,
        },
        "fusion_mode_comparison": fusion_mode_cmp,
        "temperature_scaling_oos": temp_oos,
    }
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 报告已写入 {out_path}")


if __name__ == "__main__":
    # Windows 控制台默认 cp936，报告里的 ↳/Δ 等符号会 UnicodeEncodeError
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    main()
