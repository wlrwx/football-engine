#!/usr/bin/env python3
"""Dixon-Coles 交叉验证：penaltyblog 参照实现 vs 生产 model_raw（2026-08-29）

诊断目标（账本深诊：模型 argmax 44.9% vs 市场 54.7%，8 月差距扩大）：
  1. 生产 DC 缺时间衰减（xi）吗？——扫 xi 拟合本地 5.7 万场历史，
     在账本 411 场（match_history.db，有队名+比分）上出样本外指标
  2. 参照实现的 Brier/RPS/命中率 vs 生产 model_raw / market_fair / final

在 /tmp/pb_venv（隔离环境）运行，不污染生产 venv：
  /tmp/pb_venv/bin/python scripts/dc_crossval_penaltyblog.py
"""
from __future__ import annotations

import csv
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
MATCHES = ROOT / "data" / "historical" / "matches.csv"
DB = ROOT / "data" / "state" / "match_history.db"

TRAIN_FROM = "2022-07-01"   # 近 4 年（可调）
TRAIN_TO = "2026-07-18"     # 历史库终点（账本期样本外）
XI_SWEEP = [0.0, 0.001, 0.002, 0.0035, 0.005]


def brier(probs, actual):
    return sum((p - (1.0 if i == actual else 0.0)) ** 2 for i, p in enumerate(probs))


def rps(probs, actual):
    p_h, p_hd = probs[0], probs[0] + probs[1]
    o_h = 1.0 if actual == 0 else 0.0
    o_hd = 1.0 if actual in (0, 1) else 0.0
    return ((p_h - o_h) ** 2 + (p_hd - o_hd) ** 2) / 2


def load_train() -> pd.DataFrame:
    rows = []
    with open(MATCHES, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not (TRAIN_FROM <= (r.get("date") or "") <= TRAIN_TO):
                continue
            try:
                hg, ag = int(r["home_score"]), int(r["away_score"])
            except (ValueError, KeyError, TypeError):
                continue
            rows.append((r["date"], r["home_team"], r["away_team"], hg, ag))
    df = pd.DataFrame(rows, columns=["date", "home_team", "away_team",
                                     "goals_home", "goals_away"])
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna().reset_index(drop=True)


def load_eval():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    out = []
    for r in db.execute(
        "SELECT date, league, home_team, away_team, score_home, score_away,"
        " brier_model, brier_market, brier_final FROM match_history"
        " WHERE score_home IS NOT NULL AND score_away IS NOT NULL"
    ):
        sh, sa = r["score_home"], r["score_away"]
        actual = 0 if sh > sa else (1 if sh == sa else 2)
        out.append(dict(r) | {"actual_idx": actual})
    return out


def build_model(df, xi):
    """新版 penaltyblog API：时间衰减经 weights 数组传入（dixon_coles_weights）。

    Cython 损失函数要求可写缓冲，pandas/numpy 只读视图需显式拷贝。"""
    import numpy as np
    import penaltyblog as pb
    if xi > 0:
        w = pb.models.dixon_coles_weights(df["date"], xi=xi, base_date=None)
        weights = np.array(w, dtype=float, copy=True)
    else:
        weights = None  # 静态拟合（无时间衰减）
    return pb.models.DixonColesGoalModel(
        np.array(df["goals_home"], dtype=np.int64, copy=True),
        np.array(df["goals_away"], dtype=np.int64, copy=True),
        list(df["home_team"]),
        list(df["away_team"]),
        weights=weights,
    )


def predict_probs(model, home, away):
    """FootballProbabilityGrid.home_draw_away → [p_h, p_d, p_a]。"""
    grid = model.predict(home, away)
    v = grid.home_draw_away
    return [float(x) for x in v]


def main() -> int:
    df = load_train()
    print(f"训练集 {len(df)} 场（{TRAIN_FROM}→{TRAIN_TO}）")
    evals = load_eval()
    print(f"评估集 {len(evals)} 场（match_history.db）\n")

    # 生产基线（同评估集）
    for src, col in (("生产 model_raw", "brier_model"), ("市场 market", "brier_market"),
                     ("生产 final", "brier_final")):
        vals = [r[col] for r in evals if r[col] is not None]
        if vals:
            print(f"  {src:<14} Brier {sum(vals)/len(vals):.4f} (n={len(vals)})")
    print()

    print(f"{'xi':>7}{'训练秒':>8}{'可预测':>7}{'Brier':>9}{'RPS':>9}{'argmax命中':>11}")
    for xi in XI_SWEEP:
        t0 = time.time()
        model = build_model(df, xi)
        model.fit()
        dt = time.time() - t0
        br, rp, hit, n = [], [], 0, 0
        for r in evals:
            try:
                probs = predict_probs(model, r["home_team"], r["away_team"])
            except Exception:
                continue  # 队名不在训练集（如新晋球队/杯赛异名）
            n += 1
            br.append(brier(probs, r["actual_idx"]))
            rp.append(rps(probs, r["actual_idx"]))
            hit += int(max(range(3), key=lambda i: probs[i]) == r["actual_idx"])
        print(f"{xi:>7}{dt:>7.0f}s{n:>7}{sum(br)/n:>9.4f}{sum(rp)/n:>9.4f}{hit/n:>11.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
