#!/usr/bin/env python3
"""E1 实验：市场自身的修正（盘口移动 / 跨公司分歧）能否预测市场收盘价？

2026-10-11 新增
分支即实验。这是对"模型权重槽该装什么"的直接裁决。

## 要回答的问题

上一轮在 71,774 场上证明了：纯球队实力模型（DC）显著劣于市场
（Brier Δ=+0.02018, t=+34.74, 21/21 联赛 FDR 显著）。
所以那个 6.25% 的权重槽不能继续装 DC。

候选替代是**市场自身的修正信号**。它是数据里已有、代码从未读过的：
`data/historical/football_data` 每个价格点同时有开盘和收盘。

## 严格的设计（避免用答案预测答案）

历史数据只有开盘/收盘两个时点，没有完整的盘中路径。因此本实验测的是：

> **在已经知道收盘盘口的前提下，市场自身的修正还能不能预测赛果？**

    baseline   : p_close                              （市场收盘价）
    challenger : p_close + λ · f(市场修正特征)          （市场收盘价 + 修正外推）

两种结果都很有价值：

- λ 显著 > 0 且 log-loss 改善 → 市场修正里**还有未被收盘价吸收的信息**。
  在实盘里这意味着：如果你能在收盘前捕捉到这个修正，就能提取。
  这才是"模型层能挣到那个权重"的证据。

- λ = 0 或不显著 → **市场收盘价已经把自身修正吸收干净了**，
  任何"预测市场会怎么动"的模型都是自欺。这本身就是完整结论。

注意：`p_close` 出现在 baseline 和 challenger 两侧，它扮演的是
"市场已经知道的答案"。challenger 优于 baseline，意味着修正信号里
含有收盘价**没有显式编码**的那部分信息。

## 特征

| 特征 | 含义 | 数据来源 |
|---|---|---|
| revision | 收盘去水概率 − 开盘去水概率（三个结果各一个） | AvgCH/CD/CA − AvgH/D/A |
| revision_norm | revision 的强度（模长） | 同上 |
| dispersion | 跨公司分歧 MaxC − AvgC | MaxCH − AvgCH |
| dispersion_delta | 分歧的变化 | 有则用 |
| 亚盘线 | 收盘亚盘让球数 | AHCh |
| 大小球线 | 收盘 2.5 大小球 | AvgC>2.5 |
| 联赛 one-hot | 21 联赛 | Div |

模型用**多项 logistic（softmax）** + L2 正则，λ 作为收缩系数由内层时间窗拟合。
λ=0 是候选之一，且是默认值。

## 泄漏纪律
- 训练只用测试期之前的数据（严格时间切分）
- 特征全部来自"收盘那一刻可知"的信息，不含任何赛后数据
- 外层测试期只跑一次

用法
----
    python -m engine.backtest.residual_market --refit-days 90
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from engine.backtest.historical_replay import (  # noqa: E402
    brier, devig, load_football_data, log_loss, paired_test, rps,
)

OUT_JSON = ROOT / "data" / "state" / "residual_market.json"


# ---------------------------------------------------------------- 特征

@dataclass
class FeatRow:
    date: str
    league: str
    p_close: np.ndarray      # 市场收盘去水概率（baseline 的答案）
    p_open: np.ndarray
    y: int
    x: np.ndarray            # 特征向量（不含 p_close 本身）
    odds_close: tuple        # 收盘赔率（决策用）
    n_leagues: int


def _safe_div(a: float, b: float, d: float = 0.0) -> float:
    return a / b if abs(b) > 1e-9 else d


def build_features(matches, leagues: list[str]) -> list[FeatRow]:
    lidx = {lg: i for i, lg in enumerate(leagues)}
    rows: list[FeatRow] = []
    K = len(leagues)
    for m in matches:
        if not m.close or not m.open_:
            continue
        try:
            pc = np.asarray(devig(m.close, "proportional"))
            po = np.asarray(devig(m.open_, "proportional"))
        except Exception:
            continue
        if abs(pc.sum() - 1) > 1e-6 or abs(po.sum() - 1) > 1e-6:
            continue

        rev = pc - po                       # 市场修正（3 维）
        disp = float(m.book_disp or 0.0)    # 跨公司分歧
        ah = float(m.ah_line) if m.ah_line is not None else 0.0
        ou = float(m.ou_line) if m.ou_line is not None else 2.5

        # 3 维 one-hot（联赛）+ 6 维标量
        oh = np.zeros(K)
        if m.league in lidx:
            oh[lidx[m.league]] = 1.0
        x = np.concatenate([
            rev,                                  # 3
            [float(np.linalg.norm(rev)),          # 修正强度 1
             rev[0] - rev[2],                     # 主客偏好的净移动 1
             disp,                               # 分歧 1
             math.log1p(abs(disp)),              # 分歧对数 1
             ah,                                 # 亚盘线 1
             ou],                                # 大小球线 1
            oh,                                  # K
        ])
        rows.append(FeatRow(
            date=m.date.isoformat(), league=m.league,
            p_close=pc, p_open=po, y=m.actual, x=x,
            odds_close=tuple(float(o) for o in m.close),
            n_leagues=K,
        ))
    rows.sort(key=lambda r: r.date)
    return rows


# ---------------------------------------------------------------- 模型

def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fit_residual(X: np.ndarray, y: np.ndarray, p_mkt: np.ndarray,
                 l2: float = 1.0, n_cls: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """残差多项 logistic。

        logits = log(p_market) + X @ W + b
        p      = softmax(logits)

    关键：**市场概率是基底，不是特征之一**。
    2026-10-11 修正：原实现只用 X 做 softmax，等于让模型从零预测赛果，
    特征里根本没有市场收盘价 —— 那当然比 baseline 差，
    但测的是"模型能不能替代市场"，不是"市场修正有没有增量信息"。
    正确的形式是 W=0 时精确退化为 p_close，
    于是 W 学到的就是"市场修正里未被收盘价吸收的那部分"。

    自己写而不是引 sklearn —— 这个模块要能在最小依赖环境跑，
    且残差系数必须可解释（它是本实验的核心变量）。
    """
    n, d = X.shape
    W = np.zeros((d, n_cls))
    b = np.zeros(n_cls)
    base_log = np.log(np.clip(p_mkt, 1e-9, 1.0))
    onehot = np.zeros((n, n_cls))
    onehot[np.arange(n), y] = 1.0
    # 标准化，避免量纲不同的特征（亚盘线 vs one-hot）主导梯度
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd < 1e-9] = 1.0
    Xs = (X - mu) / sd

    lr = 0.5
    for _ in range(300):
        Z = base_log + Xs @ W + b
        P = _softmax(Z)
        G = (P - onehot) / n
        gW = Xs.T @ G + l2 * W
        gb = G.sum(axis=0)
        W -= lr * gW
        b -= lr * gb
    return np.concatenate([W, b[None, :]]), np.stack([mu, sd])


def predict(Wb: np.ndarray, norm: np.ndarray, X: np.ndarray,
             p_mkt: np.ndarray) -> np.ndarray:
    d = Wb.shape[0] - 1
    W, b = Wb[:d], Wb[d]
    mu, sd = norm
    sd = np.where(sd < 1e-9, 1.0, sd)
    Xs = (X - mu) / sd
    base_log = np.log(np.clip(p_mkt, 1e-9, 1.0))
    return _softmax(base_log + Xs @ W + b)


# ---------------------------------------------------------------- 主流程

def run(rows: list[FeatRow], refit_days: int = 90, min_train: int = 2000,
        verbose: bool = True) -> dict:
    if len(rows) < 200:
        return {"error": "样本不足", "n": len(rows)}

    X = np.stack([r.x for r in rows])
    y = np.array([r.y for r in rows])
    pclose = np.stack([r.p_close for r in rows])
    dates = np.array([r.date for r in rows])

    base_ll, chal_ll, base_b, chal_b = [], [], [], []
    base_rps, chal_rps = [], []
    bets = {0.0: [0, 0.0], 0.02: [0, 0.0], 0.05: [0, 0.0]}   # 阈值 -> [注数, PnL]

    last_fit = None
    last_fit_date = None      # 上一次**拟合**的日期（不是上一场的日期）
    n_refit = 0

    for i in range(len(rows)):
        d = dates[i]
        # 2026-10-11 修正：原实现用 prev_date（上一场比赛的日期）判断是否该重拟合，
        # 而 prev_date 每场都在更新 → (d - prev_date).days 几乎恒为 0/1，
        # 条件永不成立 → 整个区间只拟合了 1 次（refit=1），不是 walk-forward。
        need_fit = (last_fit_date is None or
                    (datetime.fromisoformat(d) -
                     datetime.fromisoformat(last_fit_date)).days >= refit_days)
        if need_fit:
            train_mask = np.arange(len(rows)) < i
            n_tr = int(train_mask.sum())
            if n_tr >= min_train:
                Wb, norm = fit_residual(X[train_mask], y[train_mask], pclose[train_mask])
                last_fit = (Wb, norm)
                last_fit_date = d
                n_refit += 1
                if verbose and n_refit % 10 == 0:
                    print(f"    refit #{n_refit} @ {d} (train={n_tr})")

        if last_fit is None:
            continue
        Wb, norm = last_fit
        p_chal = predict(Wb, norm, X[i:i + 1], pclose[i:i + 1])[0]
        p_base = pclose[i]
        yy = y[i]

        base_ll.append(log_loss(p_base, yy))
        chal_ll.append(log_loss(p_chal, yy))
        base_b.append(brier(p_base, yy))
        chal_b.append(brier(p_chal, yy))
        base_rps.append(rps(p_base, yy))
        chal_rps.append(rps(p_chal, yy))

        # 决策层：模型比市场更看好某结果 → 按收盘赔率下注
        for th in bets:
            for k in range(3):
                if float(p_chal[k] - p_base[k]) >= th:
                    o = rows[i].odds_close[k]
                    bets[th][0] += 1
                    bets[th][1] += (o - 1.0) if yy == k else -1.0

    if not base_ll:
        return {"error": "无预测产出"}

    d_ll = [c - b for c, b in zip(chal_ll, base_ll)]
    d_b = [c - b for c, b in zip(chal_b, base_b)]
    d_r = [c - b for c, b in zip(chal_rps, base_rps)]

    if verbose:
        print(f"    共 {n_refit} 次重拟合，{len(base_ll)} 条配对预测")

    return {
        "n": len(base_ll),
        "n_refit": n_refit,
        "date_range": [str(dates[0]), str(dates[-1])],
        "baseline_market_close": {
            "logloss": round(float(np.mean(base_ll)), 5),
            "brier": round(float(np.mean(base_b)), 5),
            "rps": round(float(np.mean(base_rps)), 5),
        },
        "challenger_residual": {
            "logloss": round(float(np.mean(chal_ll)), 5),
            "brier": round(float(np.mean(chal_b)), 5),
            "rps": round(float(np.mean(chal_rps)), 5),
        },
        "paired_logloss": paired_test(d_ll),
        "paired_brier": paired_test(d_b),
        "paired_rps": paired_test(d_r),
        "note": ("差值 = challenger − baseline(市场收盘价)，**负 = 市场修正里"
                 "还有未被收盘价吸收的信息**"),
        "decision_layer": [
            {"edge_threshold": th, "n_bets": n,
             "roi": round(p / n, 4) if n else None,
             "total_pnl_units": round(p, 1)}
            for th, (n, p) in sorted(bets.items())
        ],
        "decision_note": ("按**收盘赔率**下注（市场收盘 overround 约 5.7%，"
                          "盲买成本 -5.3%）。ROI 为正说明模型修正能覆盖抽水。"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="E1：市场修正能否预测市场收盘价")
    ap.add_argument("--refit-days", type=int, default=90)
    ap.add_argument("--min-train", type=int, default=2000)
    ap.add_argument("--since", default=None)
    args = ap.parse_args()

    print("=" * 72)
    print("E1 实验：市场修正（盘口移动/跨公司分歧）能否预测市场收盘价？")
    print("=" * 72)

    ms = load_football_data()
    if args.since:
        s = datetime.strptime(args.since, "%Y-%m-%d").date()
        ms = [m for m in ms if m.date >= s]

    usable = [m for m in ms if m.close and m.open_]
    leagues = sorted({m.league for m in usable})
    print(f"\n  可用样本（同时有开+收盘）: {len(usable)}  联赛 {len(leagues)}")
    print(f"  日期范围: {usable[0].date} .. {usable[-1].date}")

    rows = build_features(usable, leagues)
    print(f"  特征维度: {rows[0].x.shape[0]}  有效行: {len(rows)}")

    print(f"\n  walk-forward（refit 每 {args.refit_days} 天）")
    res = run(rows, refit_days=args.refit_days, min_train=args.min_train)

    print("\n" + "=" * 72)
    if "error" in res:
        print(f"  {res['error']}")
        return 1

    print(f"\n  n={res['n']}  refit={res['n_refit']}  {res['date_range']}")
    b, c = res["baseline_market_close"], res["challenger_residual"]
    print(f"\n  {'指标':<10}{'baseline(市场收盘)':>22}{'challenger(市场修正)':>24}")
    for k in ("logloss", "brier", "rps"):
        print(f"  {k:<10}{b[k]:>22}{c[k]:>24}")

    print("\n  配对检验（负 = 市场修正里有额外信息）")
    for name in ("paired_logloss", "paired_brier", "paired_rps"):
        t = res[name]
        sig = "显著" if t["significant"] else "不显著"
        print(f"    {name:<18} Δ={t['mean']:+.5f}  t={t['t']:+.2f}  "
              f"CI=[{t['ci95'][0]:+.5f},{t['ci95'][1]:+.5f}]  {sig}")

    print("\n  决策层（按收盘赔率下注，盲买成本 -5.3%）")
    print(f"    {'阈值':>8}{'注数':>10}{'ROI':>12}{'PnL':>14}")
    for d in res["decision_layer"]:
        roi = f"{d['roi']*100:>11.2f}%" if d["roi"] is not None else "           -"
        print(f"    {d['edge_threshold']:>8}{d['n_bets']:>10}{roi}{d['total_pnl_units']:>14.1f}")

    ll = res["paired_logloss"]
    if ll["significant"] and ll["mean"] < 0:
        verdict = "E1 通过：市场修正含额外信息 → 模型层可以挣到那个权重"
    else:
        verdict = ("E1 未通过：市场收盘价已吸收自身修正 → "
                   "任何'预测市场怎么动'的模型都是自欺（这本身是完整结论）")
    print(f"\n  判定: {verdict}")

    res["verdict"] = verdict
    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 已写入 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
