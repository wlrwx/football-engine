#!/usr/bin/env python3
"""平局锚定表重放验证（2026-08-29）

现 fusion.LEAGUE_DRAW_ANCHOR 是 2026-08-12 账本 n>=5 的临时值（0.30-0.55，
量级上不可能是平局率）。football-data.co.uk 10 赛季数据给出真实联赛平局率
（0.24-0.32）。本脚本在已结算账本上重放：

  B   现表（4 联赛，账本临时值）
  E1  新表（football-data 真实平局率，w 不变 0.3）
  E2  新表 + 权重减半（w=0.15）
  E3  现表 + 权重减半（w=0.15）

按治理闸（全样本 + val 双段 t>=1.96）裁决是否翻新表。只读，不写状态。
用法: python3 scripts/anchor_replay.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ablation_replay as ar  # noqa: E402
from engine.prediction import fusion as fz  # noqa: E402
from engine.prediction.fusion import DEFAULT_POST_FUSION  # noqa: E402
from engine.review.post_match import rps_score  # noqa: E402

VAL_FRAC = 0.4
BASELINES = ROOT / "data" / "state" / "football_data_baselines.json"


def switches_new():
    on = {"same_odds_bias", "combo_boost", "market_draw_pull",
          "league_draw_baseline", "league_draw_anchor"}
    return {k: (k in on) for k in DEFAULT_POST_FUSION}


def run_arm(recs, feats, cfg, sw, anchor_map, anchor_w):
    """在给定 anchor 表下重放整本账本。

    注意 ablation_replay 在自己命名空间里 from-import 了 LEAGUE_DRAW_ANCHOR /
    DRAW_ANCHOR_W，两个模块属性都要换，否则补丁不生效（四臂同值即此因）。"""
    import ablation_replay as _ar
    old = (fz.LEAGUE_DRAW_ANCHOR, fz.DRAW_ANCHOR_W,
           _ar.LEAGUE_DRAW_ANCHOR, _ar.DRAW_ANCHOR_W)
    fz.LEAGUE_DRAW_ANCHOR = _ar.LEAGUE_DRAW_ANCHOR = anchor_map
    fz.DRAW_ANCHOR_W = _ar.DRAW_ANCHOR_W = anchor_w
    try:
        briers, hits, rpss = [], [], []
        for r in recs:
            probs = ar.fuse_probabilities(ar.build_input(r, feats, cfg, sw)).probs
            actual = r["actual_idx"]
            briers.append(sum((p - (1.0 if i == actual else 0.0)) ** 2 for i, p in enumerate(probs)))
            hits.append(int(max(range(3), key=lambda i: probs[i]) == actual))
            rpss.append(rps_score(probs, actual))
        return briers, hits, rpss
    finally:
        fz.LEAGUE_DRAW_ANCHOR = old[0]
        fz.DRAW_ANCHOR_W = old[1]
        _ar.LEAGUE_DRAW_ANCHOR = old[2]
        _ar.DRAW_ANCHOR_W = old[3]


def paired_t(arm, base):
    diffs = [b - a for b, a in zip(base, arm)]  # >0 = arm 更好
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / max(1, n - 1)
    se = (var / n) ** 0.5 if var > 0 else 0.0
    return mean, (mean / se if se > 0 else 0.0)


def main() -> int:
    recs = ar.load_ledger()
    feats = ar.load_daily_features()
    cfg = dict(ar.load_cfg())
    champ = json.loads((ROOT / "data/state/fusion_weights.json").read_text(encoding="utf-8"))["champion"]
    cfg.update({"model_weight": champ["model"], "market_weight": champ["market"], "djyy_weight": champ["djyy"]})
    sw = switches_new()

    if not BASELINES.exists():
        print("缺 football_data_baselines.json，先跑 scripts/import_football_data.py")
        return 1
    proposal = json.loads(BASELINES.read_text(encoding="utf-8"))["draw_anchor_proposal"]

    split = int(len(recs) * (1 - VAL_FRAC))
    arms = {
        "B_现表(w=0.3)": (dict(fz.LEAGUE_DRAW_ANCHOR), 0.3),
        "E1_新表真实平局率(w=0.3)": (dict(proposal), 0.3),
        "E2_新表(w=0.15)": (dict(proposal), 0.15),
        "E3_现表(w=0.15)": (dict(fz.LEAGUE_DRAW_ANCHOR), 0.15),
    }

    print(f"账本 {len(recs)} 场（val=后 {len(recs)-split} 场）\n")
    print(f"{'臂':<26}{'全Brier':>9}{'全RPS':>8}{'全命中':>8}   {'valBrier':>9}{'valRPS':>8}{'val命中':>8}   "
          f"{'t(brier)全/val':>14}{'t(rps)全/val':>14}")
    results = {}
    base_b = None
    for name, (amap, w) in arms.items():
        full = run_arm(recs, feats, cfg, sw, amap, w)
        val = run_arm(recs[split:], feats, cfg, sw, amap, w)
        results[name] = (full, val)
        if name.startswith("B_"):
            base_b = name
        fb, fr, fh = sum(full[0])/len(full[0]), sum(full[2])/len(full[2]), sum(full[1])/len(full[1])
        vb, vr, vh = sum(val[0])/len(val[0]), sum(val[2])/len(val[2]), sum(val[1])/len(val[1])
        print(f"{name:<26}{fb:>9.4f}{fr:>8.4f}{fh:>8.1%}   {vb:>9.4f}{vr:>8.4f}{vh:>8.1%}", end="   ")
        if name == base_b:
            print(f"{'基线':>14}{'':>14}")
            continue
        bf, bv = results[base_b]
        _, tb_f = paired_t(full[0], bf[0])
        _, tb_v = paired_t(val[0], bv[0])
        _, tr_f = paired_t(full[2], bf[2])
        _, tr_v = paired_t(val[2], bv[2])
        print(f"{tb_f:>7.2f}/{tb_v:<6.2f}{tr_f:>7.2f}/{tr_v:<6.2f}")

    print("\n裁决：E 臂需 全+val 两段 t>=1.96（Brier 或 RPS 任一口径双过）才翻表；"
          "否则留每周优化器。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
