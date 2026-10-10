#!/usr/bin/env python3
"""门禁测试：校准之后，是否存在任何正 EV 区间？（2026-10-11 新增）

这是决定后续 9 周工作是否值得做的那个实验。

问题
----
三份外部建议都假定"先修评估层，然后上更深的模型"。但没有任何一份回答：
**在有统计功效的样本上，这套系统的概率估计到底能不能转化为正期望？**

实盘 955 场回答不了（Δ=+0.0036, t=+0.97, n=483）。
6.2 万场历史（其中 33,060 场有完整开+收盘 1X2）可以回答。

设计
----
用**开盘价**下注、用**收盘价**对照。这样 EV 不是零构造：

    edge_t = p_model(t) − p_open_devig(t)        ← 你认为比开盘价便宜多少
    实际盈亏 = (odds_open − 1) if 命中 else −1

    CLV     = p_model(t) − p_close_devig(t)      ← 你的概率比收盘市场高多少

关键：模型概率由 walk-forward 产生，**只见过该场日期之前的比赛**，
盘口移动信息（open→close）不在特征里。否则就是用答案预测答案。

三层问题
--------
Q1 概率层：p_model 相对去水收盘价，有增量吗？      → paired logloss / Brier
Q2 校准层：edge 估计是不是系统性过度自信？          → 分桶 ev_gap + isotonic
Q3 决策层：校准后有没有任何阈值 ROI > 0？           → 阈值扫描

Q3 为否 ⇒ 投注侧应当停止投入（竞彩 12.1% 抽水下更不可能）。

用法
----
    python -m engine.backtest.gate_test --refit-days 14
    python -m engine.backtest.gate_test --thresholds 0.00 0.02 0.05 0.10
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from engine.backtest.historical_replay import (  # noqa: E402
    HistMatch, compare, devig, line_movement, load_football_data,
    market_benchmark, paired_test, walk_forward,
)

OUT_JSON = ROOT / "data" / "state" / "gate_test.json"


# ---------------------------------------------------------------- Q2 校准

def isotonic_fit(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """PAV 算法求保序回归（最小二乘意义下的单调拟合）。

    返回 (块内 x 均值, 块内 y 均值)，两者都已单调不减。

    自己实现而不是引 sklearn：校准是本项目最核心的资产，必须可解释。

    2026-10-11 修正：原实现只维护 x 的块值，且返回的是**块大小**，
    下游拿它当 y 用 → 校准曲线实际是 x 的累积分布，会把过度自信的模型
    "校准"成更自信。属于静默失效且不报错，必须锁住。
    """
    order = np.argsort(x, kind="mergesort")
    xs = np.asarray(x, dtype=float)[order]
    ys = np.asarray(y, dtype=float)[order]

    # 每个块：[sum_x, sum_y, count]
    blocks: list[list[float]] = [[float(a), float(b), 1.0]
                                 for a, b in zip(xs, ys)]

    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] / blocks[i][2] <= blocks[i + 1][1] / blocks[i + 1][2] + 1e-12:
            i += 1
            continue
        a, b = blocks[i], blocks[i + 1]
        blocks[i] = [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
        del blocks[i + 1]
        if i > 0:
            i -= 1

    return (np.asarray([bl[0] / bl[2] for bl in blocks]),
            np.asarray([bl[1] / bl[2] for bl in blocks]))


def isotonic_calibrate(pred: np.ndarray, actual: np.ndarray) -> dict:
    """拟合 p → 实际命中率 的单调映射，返回查表用的 (阈值, 校准值)。"""
    if len(pred) < 50:
        return {"n": len(pred), "ok": False}
    xp, yp = isotonic_fit(pred.astype(float), actual.astype(float))
    return {"n": len(pred), "ok": True, "x": xp.tolist(), "y": yp.tolist()}


def apply_isotonic(curve: dict, probs: list[float]) -> np.ndarray:
    if not curve.get("ok"):
        return np.asarray(probs, dtype=float)
    xs = np.asarray(curve["x"])
    ys = np.asarray(curve["y"])
    return np.clip(np.interp(np.asarray(probs, dtype=float), xs, ys), 0.001, 0.999)


# ---------------------------------------------------------------- 主测试

def run(matches: list[HistMatch], refit_days: int = 14,
        thresholds: list[float] | None = None, verbose: bool = True) -> dict:
    thresholds = thresholds if thresholds is not None else [0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10]
    use = [m for m in matches if m.close and m.open_]
    if verbose:
        print(f"  可用样本（同时具备收盘+开盘）: {len(use)}")

    # walk-forward 产生无泄漏的 p_model
    preds = walk_forward(use, refit_days=refit_days, devig_method="proportional",
                         verbose=verbose)
    if not preds:
        return {"n": 0, "error": "walk_forward 无产出"}

    # 把 walk-forward 的预测与原始赔率对齐
    by_key = {(m.date.isoformat(), m.home, m.away): m for m in use}
    rows = []
    for p in preds:
        m = by_key.get((p["date"], p["home"], p["away"]))
        if m is None or m.open_ is None:
            continue
        p_model = np.asarray(p["p_model"], dtype=float)
        p_close = np.asarray(devig(m.close, "proportional"))
        p_open = np.asarray(devig(m.open_, "proportional"))
        rows.append({
            "close": np.asarray(m.close, dtype=float),
            "p_model": p_model,
            "p_close": p_close,
            "p_open": p_open,
            "odds_open": np.asarray(m.open_, dtype=float),
            "y": p["actual"],
            "league": p["league"],
        })
    if verbose:
        print(f"  对齐后可用于 EV 测试的场次: {len(rows)}")

    # ---------- Q1 概率层 ----------
    q1 = {
        "paired_brier": paired_test([float(r["p_model"][r["y"]] * 0) for r in []] or
                                    [float(((r["p_model"] - np.eye(3)[r["y"]]) ** 2).sum()
                                           - ((r["p_close"] - np.eye(3)[r["y"]]) ** 2).sum())
                                     for r in rows]),
        "paired_logloss": paired_test(
            [-math.log(max(r["p_model"][r["y"]], 1e-12))
             + math.log(max(r["p_close"][r["y"]], 1e-12)) for r in rows]),
        "n": len(rows),
    }

    # ---------- Q2 校准层 ----------
    # 对每场每个结果：估计 edge = p_model − p_open_de vig（相对我能买到的价格）
    edge_est, hit, pmodel_flat = [], [], []
    for r in rows:
        for k in range(3):
            edge_est.append(float(r["p_model"][k] - r["p_open"][k]))
            pmodel_flat.append(float(r["p_model"][k]))
            hit.append(1.0 if r["y"] == k else 0.0)
    edge_est = np.asarray(edge_est)
    hit = np.asarray(hit)
    pmodel_flat = np.asarray(pmodel_flat)

    buckets = []
    edges = [-1, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 1.0]
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if lo >= hi:
            continue
        sel = (edge_est >= lo) & (edge_est < hi)
        n = int(sel.sum())
        if n < 50:
            continue
        actual_rate = float(hit[sel].mean())
        claimed = float(pmodel_flat[sel].mean())
        buckets.append({
            "edge_bucket": f"[{lo:.3f},{hi:.3f})",
            "n": n,
            "actual_hit_rate": round(actual_rate, 4),
            "claimed_prob": round(claimed, 4),
            # 与 data/state/edge_calibration.json 同口径：
            # ev_gap = 实际命中 − 声称概率。负 = 模型过度自信。
            "ev_gap": round(actual_rate - claimed, 4),
            "mean_edge": round(float(edge_est[sel].mean()), 4),
        })

    # 正确的 ev_gap：把该桶内每个结果按各自开盘价算真实 EV
    # （不同结果的赔率差很大，用平均 edge 当代理会失真）
    ev_rows = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if lo >= hi:
            continue
        sel_idx = np.where((edge_est >= lo) & (edge_est < hi))[0]
        if len(sel_idx) < 50:
            continue
        pnl = 0.0
        for j in sel_idx:
            r, k = divmod(j, 3)
            row = rows[r]
            o = float(row["odds_open"][k])
            pnl += (o - 1.0) if row["y"] == k else -1.0
        ev_rows.append((f"[{lo:.3f},{hi:.3f})", len(sel_idx), pnl / len(sel_idx)))

    curve = isotonic_calibrate(edge_est, hit)

    # ---------- Q3 决策层：阈值扫描 ----------
    # 三种口径：
    #   raw     —— 直接用 p_model 的 edge
    #   iso     —— 用 isotonic 校准后的 p_model 再算 edge
    #   shrunk  —— 对 edge 乘收缩因子
    scan = {}
    for label, transform in (
        ("raw", lambda r: r["p_model"]),
        ("iso", lambda r: apply_isotonic(curve, r["p_model"].tolist())),
    ):
        results = []
        for th in thresholds:
            pnl = 0.0
            n_bet = 0
            for r in rows:
                pk = transform(r)
                for k in range(3):
                    e = float(pk[k] - r["p_open"][k])
                    if e >= th:
                        o = float(r["odds_open"][k])
                        pnl += (o - 1.0) if r["y"] == k else -1.0
                        n_bet += 1
            results.append({
                "threshold": th,
                "n_bets": n_bet,
                "roi": round(pnl / n_bet, 4) if n_bet else None,
                "total_pnl_units": round(pnl, 2),
            })
        scan[label] = results

    # CLV：用开盘价下的注，检查最终是否优于收盘线。
    # 两种定义，**赔率口径才是业界标准且不受过度自信污染**：
    #   odds_clv = o_open / o_close − 1   （拿到比收盘更好的价格 → >0）
    #   prob_clv = p_model − p_close      （会被模型的过度自信系统性抬高，不可用作判据）
    clv_rows = []
    for th in thresholds:
        pnl = 0.0
        n_bet = 0
        odds_clv_sum = 0.0
        prob_clv_sum = 0.0
        for r in rows:
            for k in range(3):
                e = float(r["p_model"][k] - r["p_open"][k])
                if e >= th:
                    o_open = float(r["odds_open"][k])
                    o_close = float(r["close"][k])
                    pnl += (o_open - 1.0) if r["y"] == k else -1.0
                    n_bet += 1
                    if o_close > 1.0:
                        odds_clv_sum += (o_open / o_close - 1.0)
                    prob_clv_sum += float(r["p_model"][k] - r["p_close"][k])
        clv_rows.append({
            "threshold": th,
            "n_bets": n_bet,
            "roi": round(pnl / n_bet, 4) if n_bet else None,
            "odds_clv": round(odds_clv_sum / n_bet, 5) if n_bet else None,
            "prob_clv": round(prob_clv_sum / n_bet, 5) if n_bet else None,
        })

    return {
        "n_matches": len(rows),
        "q1_probability_vs_market": {
            "note": "差值 = 模型 − 市场，**负 = 模型更好**",
            "brier": q1["paired_brier"],
            "logloss": q1["paired_logloss"],
        },
        "q2_calibration": {
            "note": ("edge = p_model − 去水开盘概率。ev_gap>0 表示模型高估了自己的优势。"
                     "现盘 edge_calibration.json 五个概率桶 ev_gap 全负，"
                     "这里在 33,000 场上重新检验。"),
            "buckets": buckets,
            "bucket_real_roi": [{"edge_bucket": b, "n": n, "roi": round(r, 4)}
                                for b, n, r in ev_rows],
            "isotonic_curve": {"ok": curve.get("ok"), "n": curve.get("n")},
        },
        "q3_threshold_scan": scan,
        "clv_scan": clv_rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refit-days", type=int, default=14)
    ap.add_argument("--thresholds", type=float, nargs="*", default=None)
    ap.add_argument("--since", default=None)
    args = ap.parse_args()

    print("=" * 70)
    print("门禁测试：校准后是否存在正 EV 区间？")
    print("=" * 70)

    ms = load_football_data()
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
        ms = [m for m in ms if m.date >= since]

    res = run(ms, refit_days=args.refit_days, thresholds=args.thresholds)

    print("\n" + "=" * 70)
    if not res.get("n_matches"):
        print("无有效样本")
        return 1

    print(f"\nQ1 概率层：模型 vs 去水收盘市场（n={res['q1_probability_vs_market']['brier']['n']}）")
    b = res["q1_probability_vs_market"]["brier"]
    l = res["q1_probability_vs_market"]["logloss"]
    print(f"    Brier    Δ={b['mean']}  t={b['t']}  {'显著' if b['significant'] else '不显著'}")
    print(f"    logloss  Δ={l['mean']}  t={l['t']}  {'显著' if l['significant'] else '不显著'}")
    print("    （负 = 模型更好）")

    print("\nQ2 校准层：按 edge 分桶（负 ev_gap = 模型过度自信，与 edge_calibration.json 同口径）")
    print(f"    {'edge 桶':<18}{'n':>8}{'实际命中':>10}{'声称概率':>10}{'ev_gap':>10}{'真实ROI':>10}")
    roi_map = {x["edge_bucket"]: x["roi"] for x in res["q2_calibration"]["bucket_real_roi"]}
    for bk in res["q2_calibration"]["buckets"]:
        roi = roi_map.get(bk["edge_bucket"])
        print(f"    {bk['edge_bucket']:<18}{bk['n']:>8}{bk['actual_hit_rate']:>10.4f}"
              f"{bk['claimed_prob']:>10.4f}{bk['ev_gap']:>10.4f}"
              f"{(f'{roi:>10.4f}' if roi is not None else '         -')}")

    print("\nQ3 决策层：阈值扫描（开盘价下注）")
    for label in ("raw", "iso"):
        print(f"    --- {label} 概率 ---")
        print(f"    {'阈值':>8}{'注数':>10}{'ROI':>10}{'累计PnL':>12}")
        for row in res["q3_threshold_scan"][label]:
            roi = f"{row['roi']*100:>9.2f}%" if row["roi"] is not None else "         -"
            print(f"    {row['threshold']:>8.2f}{row['n_bets']:>10}"
                  f"{roi}{row['total_pnl_units']:>12.1f}")

    print("\nCLV：开盘价下的注，最终是否优于收盘线？")
    print("    odds_clv = o_open/o_close-1（>0 = 拿到比收盘更好的价格，业界标准）")
    print("    prob_clv = p_model-p_close（会被过度自信系统性抬高，仅供对照）")
    print(f"    {'阈值':>8}{'注数':>10}{'ROI':>10}{'odds_CLV':>12}{'prob_CLV':>12}")
    for row in res["clv_scan"]:
        roi = f"{row['roi']*100:>9.2f}%" if row["roi"] is not None else "         -"
        oc = f"{row['odds_clv']*100:>11.2f}%" if row.get("odds_clv") is not None else "           -"
        pc = f"{row['prob_clv']:>12.5f}" if row.get("prob_clv") is not None else "           -"
        print(f"    {row['threshold']:>8.2f}{row['n_bets']:>10}{roi}{oc}{pc}")

    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 已写入 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
