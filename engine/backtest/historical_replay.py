#!/usr/bin/env python3
"""历史回放：把 6.2 万场带收盘价的比赛接进来（2026-10-11 新增）

背景
----
`data/historical/football_data/` 里有 146 个 football-data.co.uk 全字段 CSV，
共 71,838 场，其中 **62,347 场有多公司完整收盘 1X2（AvgCH/CD/CA）**。
但全仓库检索 `AvgCH|B365CH|PSCH|...` **零命中** —— `import_football_data.py`
只消费 FTHG/FTAG/FTR/Date 四列算联赛平局率，所有赔率列从未被读过。

后果：评估样本只有实盘账本的 955 场，任何差异都做不出统计功效
（实测 final vs market Δ=+0.0036, t=+0.97, n=483）。

本模块做三件事
--------------
1. `load_football_data()`   — 读入历史比赛 + 开盘/收盘/亚盘/大小球
2. `devig()`               — 多种去抽水口径（收盘价的可比性完全取决于它）
3. `walk_forward()`        — 严格时间切分的 DC 拟合 + 预测，与市场同批配对比较

泄漏纪律
--------
* 预测某场比赛时，DC 只见过 `date < 该场日期` 的比赛（硬约束，不是"大致如此"）。
* 市场基准用**收盘价**本身。收盘价在开赛前已知，不构成泄漏。
* 联赛参数、平局锚定等任何静态统计，一律只用测试期之前的数据。

用法
----
    python -m engine.backtest.historical_replay --refit-days 14
    python -m engine.backtest.historical_replay --since 2020-01-01 --dry-run
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = ROOT / "data" / "historical" / "football_data"
OUT_JSON = ROOT / "data" / "state" / "historical_replay.json"

# home / draw / away 的固定顺序，全模块统一
IDX = {"home": 0, "draw": 1, "away": 2}


# ---------------------------------------------------------------- 数据结构

@dataclass
class HistMatch:
    date: date
    league: str
    home: str
    away: str
    hg: int
    ag: int
    # 赔率：收盘（Avg 多公司均值）与开盘
    close: Optional[tuple[float, float, float]] = None
    open_: Optional[tuple[float, float, float]] = None
    # 收盘亚盘线（让球数）与收盘大小球线
    ah_line: Optional[float] = None
    ou_line: Optional[float] = None
    # 跨公司分歧：Max - Avg，越大说明各家分歧越高
    book_disp: Optional[float] = None
    shots: Optional[tuple[int, int]] = None
    ht_score: Optional[tuple[int, int]] = None

    @property
    def actual(self) -> int:
        if self.hg > self.ag:
            return 0
        if self.hg == self.ag:
            return 1
        return 2


def _f(v) -> Optional[float]:
    try:
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none", "-"):
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _i(v) -> Optional[int]:
    x = _f(v)
    return int(x) if x is not None else None


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _triplet(row: dict, ph: str, pd_: str, pa: str) -> Optional[tuple[float, float, float]]:
    h, d, a = _f(row.get(ph)), _f(row.get(pd_)), _f(row.get(pa))
    if h and d and a and h > 1 and d > 1 and a > 1:
        return (h, d, a)
    return None


def _norm_new_row(r: dict) -> dict:
    """把 /new/ 端点格式归一到标准字段名。

    2026-10-11 新增。之前 loader 只认 FTHG/FTAG，把 7 个 /new/ 文件
    （BRA/USA/JPN/SWE/NOR/RUS/FIN，共 29,368 行）整批丢弃——而这些文件
    **是有收盘赔率的**（AvgCH/CD/CA + Pinnacle 的 PSCH/CD/CA），
    只是比分字段叫 HG/AG。

    注意：/new/ 端点**只有收盘、没有开盘**，所以这批数据能进市场基准，
    但不能进 gate_test（后者需要"开盘价下注、收盘价对照"）。
    """
    out = dict(r)
    out["FTHG"] = r.get("HG")
    out["FTAG"] = r.get("AG")
    out["HomeTeam"] = r.get("Home")
    out["AwayTeam"] = r.get("Away")
    ftr = (r.get("Res") or "").strip().upper()
    out["FTR"] = ftr if ftr in ("H", "D", "A") else None
    out["Div"] = (r.get("Country") or r.get("League") or "").strip()
    return out


def load_football_data(raw_dir: Path = RAW_DIR) -> list[HistMatch]:
    """读入 football-data.co.uk 原始 CSV（标准格式 + /new/ 格式）。"""
    out: list[HistMatch] = []
    if not raw_dir.exists():
        return out
    for f in sorted(glob.glob(str(raw_dir / "*.csv"))):
        try:
            with open(f, newline="", encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh)
                fields = set(reader.fieldnames or [])
                is_new = "HG" in fields and "AG" in fields
                for raw in reader:
                    r = _norm_new_row(raw) if is_new else raw
                    d = _parse_date(r.get("Date", ""))
                    hg, ag = _i(r.get("FTHG")), _i(r.get("FTAG"))
                    if d is None or hg is None or ag is None:
                        continue
                    close = _triplet(r, "AvgCH", "AvgCD", "AvgCA")
                    if close is None:
                        close = _triplet(r, "PSCH", "PSCD", "PSCA")
                    if close is None:
                        close = _triplet(r, "B365CH", "B365CD", "B365CA")
                    # 跨公司分歧（收盘 Max − 收盘 Avg）
                    disp = None
                    mh, ah = _f(r.get("MaxCH")), _f(r.get("AvgCH"))
                    if mh is not None and ah is not None:
                        disp = abs(mh - ah)
                    out.append(HistMatch(
                        date=d,
                        league=(r.get("Div") or Path(f).stem.split("_")[0]).strip(),
                        home=(r.get("HomeTeam") or "").strip(),
                        away=(r.get("AwayTeam") or "").strip(),
                        hg=hg, ag=ag,
                        close=close,
                        # /new/ 端点没有开盘列，显式留空而不是误读
                        open_=_triplet(r, "AvgH", "AvgD", "AvgA"),
                        ah_line=_f(r.get("AHCh")) if _f(r.get("AHCh")) is not None else _f(r.get("AHh")),
                        ou_line=_f(r.get("AvgC>2.5")),
                        book_disp=disp,
                        shots=((_i(r.get("HS")), _i(r.get("AS")))
                               if _i(r.get("HS")) is not None and _i(r.get("AS")) is not None else None),
                        ht_score=((_i(r.get("HTHG")), _i(r.get("HTAG")))
                                  if _i(r.get("HTHG")) is not None and _i(r.get("HTAG")) is not None else None),
                    ))
        except Exception:
            continue
    out.sort(key=lambda m: m.date)
    return out


# ---------------------------------------------------------------- 去抽水

def devig(odds: tuple[float, float, float], method: str = "proportional") -> tuple[float, float, float]:
    """把博彩赔率转成无水位概率。

    proportional — 等比例缩放（最常用，对 1X2 足够）
    power        — 等比幂缩放，对长赔率更宽容
    shin         — Shin 模型，假设庄家对热门有信息优势（需 z>0）
    """
    h, d, a = odds
    inv = np.array([1.0 / h, 1.0 / d, 1.0 / a])
    overround = inv.sum()

    if method == "proportional":
        return tuple(inv / overround)

    if method == "power":
        # 求解 p ∝ inv^k，k 使 sum(p)=1
        lo, hi = 0.5, 3.0
        for _ in range(60):
            k = (lo + hi) / 2
            p = inv ** k
            s = p.sum()
            if s > 1:
                lo = k
            else:
                hi = k
        k = (lo + hi) / 2
        p = inv ** k
        return tuple(p / p.sum())

    if method == "shin":
        # Shin: p_i = (sqrt(z^2 + 4(1-z)*inv_i^2/Σinv) - z) / (2(1-z))
        z = 0.02
        base = overround
        p = np.array([
            (math.sqrt(z * z + 4 * (1 - z) * (1.0 / o) ** 2 / base) - z) / (2 * (1 - z))
            for o in odds
        ])
        s = p.sum()
        return tuple(p / s) if s > 0 else tuple(inv / overround)

    raise ValueError(f"unknown devig method: {method}")


# ---------------------------------------------------------------- 评分

def brier(p: np.ndarray, y: int) -> float:
    onehot = np.zeros(3)
    onehot[y] = 1.0
    return float(((p - onehot) ** 2).sum())


def log_loss(p: np.ndarray, y: int, eps: float = 1e-12) -> float:
    return float(-math.log(max(p[y], eps)))


def rps(p: np.ndarray, y: int) -> float:
    """Ranked Probability Score（home/draw/away 有序，不能像 Brier 那样无视邻近类别）。"""
    order = [0, 1, 2]
    cp = 0.0
    cy = 0.0
    s = 0.0
    for k in order[:-1]:
        cp += p[k]
        cy += 1.0 if y == k else 0.0
        s += (cp - cy) ** 2
    return s / (len(order) - 1)


def paired_test(diffs: list[float]) -> dict:
    """配对 t 检验。diffs = 模型分 − 基准分，**负 = 模型更好**。"""
    n = len(diffs)
    if n < 2:
        return {"n": n, "mean": None, "t": None, "ci95": None, "significant": False}
    a = np.asarray(diffs, dtype=float)
    m = float(a.mean())
    sd = float(a.std(ddof=1))
    se = sd / math.sqrt(n)
    t = m / se if se > 0 else 0.0
    return {
        "n": n,
        "mean": round(m, 5),
        "t": round(t, 3),
        "ci95": [round(m - 1.96 * se, 5), round(m + 1.96 * se, 5)],
        "significant": abs(t) >= 1.96,
    }


def bh_fdr(pvals: dict[str, float], alpha: float = 0.10) -> dict[str, dict]:
    """Benjamini–Hochberg FDR。21 个联赛各出一次结论，不校正必然出假阳性。"""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, dict] = {}
    prev_reject = True
    for rank, (k, p) in enumerate(items, start=1):
        thresh = alpha * rank / m
        reject = p <= thresh and prev_reject
        prev_reject = reject
        out[k] = {"p": p, "rank": rank, "threshold": round(thresh, 4), "reject": reject}
    return out


# ---------------------------------------------------------------- 资产盘点

def inventory(matches: list[HistMatch]) -> dict:
    n = len(matches)
    with_close = [m for m in matches if m.close]
    with_open = [m for m in matches if m.open_]
    both = [m for m in matches if m.close and m.open_]
    return {
        "total_matches": n,
        "date_range": [str(matches[0].date), str(matches[-1].date)] if matches else None,
        "leagues": len({m.league for m in matches}),
        "with_closing_1x2": len(with_close),
        "with_opening_1x2": len(with_open),
        "with_open_and_close": len(both),
        "with_closing_ah_line": sum(1 for m in matches if m.ah_line is not None),
        "with_closing_ou_line": sum(1 for m in matches if m.ou_line is not None),
        "with_shots": sum(1 for m in matches if m.shots),
        "with_ht_score": sum(1 for m in matches if m.ht_score),
        "book_disp_available": sum(1 for m in matches if m.book_disp is not None),
    }


def market_benchmark(matches: list[HistMatch], devig_method: str = "proportional") -> dict:
    """去水收盘价本身作为基准：Brier / logloss / RPS / 校准曲线。"""
    sub = [m for m in matches if m.close]
    if not sub:
        return {"n": 0}
    bs, ls, rs = [], [], []
    overround = []
    buckets: dict[str, list[int]] = defaultdict(list)
    by_league: dict[str, list[float]] = defaultdict(list)

    for m in sub:
        p = np.array(devig(m.close, devig_method))
        y = m.actual
        bs.append(brier(p, y))
        ls.append(log_loss(p, y))
        rs.append(rps(p, y))
        overround.append(sum(1.0 / o for o in m.close))
        by_league[m.league].append(bs[-1])
        top = int(np.argmax(p))
        buckets[f"{p[top]*100:.0f}分档"].append(1 if top == y else 0)

    calib = {}
    for k in sorted(buckets, key=lambda s: int(s.replace("分档", ""))):
        v = buckets[k]
        calib[k] = {"n": len(v), "hit_rate": round(sum(v) / len(v), 4)}

    return {
        "n": len(sub),
        "devig_method": devig_method,
        "brier": round(float(np.mean(bs)), 5),
        "log_loss": round(float(np.mean(ls)), 5),
        "rps": round(float(np.mean(rs)), 5),
        "overround_mean": round(float(np.mean(overround)), 4),
        "overround_median": round(float(np.median(overround)), 4),
        "argmax_calibration": calib,
        "brier_by_league": {k: {"n": len(v), "brier": round(float(np.mean(v)), 5)}
                            for k, v in sorted(by_league.items())},
    }


def line_movement(matches: list[HistMatch], devig_method: str = "proportional") -> dict:
    """开盘 → 收盘的盘口移动分布。

    这是 closing-line-efficiency 文献里最稳健的单一信号，而它天然存在：
    每个价格点同时有开盘和收盘两份。现有 data/historical/odds.csv 只有单一
    价格，把这个信号彻底丢掉了。
    """
    both = [m for m in matches if m.close and m.open_]
    if not both:
        return {"n": 0}
    deltas = []
    for m in both:
        po = np.array(devig(m.open_, devig_method))
        pc = np.array(devig(m.close, devig_method))
        deltas.append(pc - po)   # >0 = 市场收盘时更看好该结果
    d = np.asarray(deltas)
    return {
        "n": len(both),
        "mean_abs_move": round(float(np.abs(d).mean()), 5),
        "max_abs_move": round(float(np.abs(d).max()), 5),
        "home": {"mean": round(float(d[:, 0].mean()), 5), "std": round(float(d[:, 0].std()), 5)},
        "draw": {"mean": round(float(d[:, 1].mean()), 5), "std": round(float(d[:, 1].std()), 5)},
        "away": {"mean": round(float(d[:, 2].mean()), 5), "std": round(float(d[:, 2].std()), 5)},
        "note": "收盘-开盘的去水概率变化。>0 = 市场收盘更看好该结果。",
    }


# ---------------------------------------------------------------- walk-forward

class _FitRow:
    """喂给 fit_shrinkage_dc 的适配器（它要 home_team/away_team/... 属性）。"""
    __slots__ = ("home_team", "away_team", "home_score", "away_score", "match_date")

    def __init__(self, m: HistMatch):
        self.home_team = m.home
        self.away_team = m.away
        self.home_score = m.hg
        self.away_score = m.ag
        self.match_date = m.date


def walk_forward(
    matches: list[HistMatch],
    refit_days: int = 14,
    train_window_days: int = 730,
    min_train: int = 800,
    devig_method: str = "proportional",
    verbose: bool = True,
) -> list[dict]:
    """严格时间切分：预测某场时，DC 只见过该场日期之前的比赛。

    每 refit_days 天重拟合一次，窗口内所有比赛共用同一套参数。
    """
    from engine.prediction.shrinkage_dc import fit_shrinkage_dc, ShrinkageDCConfig

    matches = sorted(matches, key=lambda m: m.date)
    if not matches:
        return []

    preds: list[dict] = []
    model = None
    fitted_upto: Optional[date] = None
    i = 0
    n_refit = 0

    while i < len(matches):
        cur = matches[i]

        if model is None or (fitted_upto is not None and (cur.date - fitted_upto).days >= refit_days):
            window_start = cur.date - timedelta(days=train_window_days)
            train = [m for m in matches[:i] if m.date >= window_start]
            if len(train) >= min_train:
                try:
                    model = fit_shrinkage_dc(
                        (_FitRow(m) for m in train),
                        ShrinkageDCConfig(),
                        date_attr="match_date",
                    )
                    fitted_upto = cur.date
                    n_refit += 1
                    if verbose and n_refit % 10 == 0:
                        print(f"    refit #{n_refit} @ {cur.date} (train={len(train)})")
                except Exception as e:
                    if verbose:
                        print(f"    fit 失败 @ {cur.date}: {type(e).__name__}: {e}")
                    model = None
            # 样本不足时保持 model=None，继续前进

        if model is not None:
            try:
                pm = model.predict_probs(cur.home, cur.away)
                p_model = np.array(pm, dtype=float)
                if abs(float(p_model.sum()) - 1.0) < 1e-6 and np.all(p_model >= 0):
                    p_market = np.array(devig(cur.close, devig_method)) if cur.close else None
                    preds.append({
                        "date": cur.date.isoformat(),
                        "league": cur.league,
                        "home": cur.home, "away": cur.away,
                        "actual": cur.actual,
                        "p_model": p_model.tolist(),
                        "p_market": p_market.tolist() if p_market is not None else None,
                        "has_market": p_market is not None,
                        "brier_model": brier(p_model, cur.actual),
                        "logloss_model": log_loss(p_model, cur.actual),
                        "rps_model": rps(p_model, cur.actual),
                        "brier_market": brier(p_market, cur.actual) if p_market is not None else None,
                        "logloss_market": log_loss(p_market, cur.actual) if p_market is not None else None,
                        "rps_market": rps(p_market, cur.actual) if p_market is not None else None,
                    })
            except Exception:
                pass
        i += 1

    if verbose:
        print(f"    共 {n_refit} 次重拟合，产出 {len(preds)} 条预测")
    return preds


def compare(preds: list[dict]) -> dict:
    """模型 vs 市场，同批配对。"""
    paired = [p for p in preds if p["has_market"]]
    if not paired:
        return {"n": 0}

    d_b = [p["brier_model"] - p["brier_market"] for p in paired]
    d_l = [p["logloss_model"] - p["logloss_market"] for p in paired]
    d_r = [p["rps_model"] - p["rps_market"] for p in paired]

    res = {
        "n": len(paired),
        "model_brier": round(float(np.mean([p["brier_model"] for p in paired])), 5),
        "market_brier": round(float(np.mean([p["brier_market"] for p in paired])), 5),
        "model_logloss": round(float(np.mean([p["logloss_model"] for p in paired])), 5),
        "market_logloss": round(float(np.mean([p["logloss_market"] for p in paired])), 5),
        "model_rps": round(float(np.mean([p["rps_model"] for p in paired])), 5),
        "market_rps": round(float(np.mean([p["rps_market"] for p in paired])), 5),
        "paired_brier": paired_test(d_b),
        "paired_logloss": paired_test(d_l),
        "paired_rps": paired_test(d_r),
        "wins": sum(1 for x in d_b if x < 0),
        "losses": sum(1 for x in d_b if x > 0),
        "note": "差值 = 模型 − 市场，**负 = 模型更好**。这是决定系统有无边际信息的唯一检验。",
    }

    # 分联赛 + BH-FDR
    bl: dict[str, list[float]] = defaultdict(list)
    for p in paired:
        bl[p["league"]].append(p["brier_model"] - p["brier_market"])
    leagues = {}
    pvals: dict[str, float] = {}
    for k, v in bl.items():
        if len(v) < 30:
            continue
        tst = paired_test(v)
        leagues[k] = {"n": len(v), "delta_brier": tst["mean"], "t": tst["t"],
                      "better": tst["mean"] < 0}
        pvals[k] = 2 * (1 - 0.5 * (1 + math.erf(abs(tst["t"]) / math.sqrt(2))))
    if pvals:
        fdr = bh_fdr(pvals)
        for k, v in fdr.items():
            leagues[k]["p"] = round(v["p"], 5)
            leagues[k]["fdr_reject"] = v["reject"]
        res["by_league"] = leagues
        res["fdr_alpha"] = 0.10
        res["fdr_note"] = "未经 FDR 校正的联赛差异不得作为配置依据。"
    return res


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="历史回放：6.2万场收盘价 vs walk-forward DC")
    ap.add_argument("--refit-days", type=int, default=14)
    ap.add_argument("--train-window-days", type=int, default=730)
    ap.add_argument("--devig", default="proportional", choices=["proportional", "power", "shin"])
    ap.add_argument("--since", default=None, help="只回放该日期之后（调试用）")
    ap.add_argument("--no-walkforward", action="store_true", help="只出市场基准，不跑模型")
    ap.add_argument("--out", default=str(OUT_JSON))
    args = ap.parse_args()

    print("=" * 70)
    print("历史回放 —— data/historical/football_data（此前从未被读取的赔率资产）")
    print("=" * 70)

    matches = load_football_data()
    inv = inventory(matches)
    print("\n[1] 资产盘点")
    for k, v in inv.items():
        print(f"    {k:<26} {v}")

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
        matches = [m for m in matches if m.date >= since]
        print(f"\n    --since {args.since} → 过滤后 {len(matches)} 场")

    print(f"\n[2] 市场基准（去水收盘价，{args.devig}）")
    bench = market_benchmark(matches, args.devig)
    if bench.get("n"):
        print(f"    n={bench['n']}  Brier {bench['brier']}  logloss {bench['log_loss']}  RPS {bench['rps']}")
        print(f"    overround 均值 {bench['overround_mean']} / 中位 {bench['overround_median']}")
        print("    argmax 校准:", ", ".join(
            f"{k}→{v['hit_rate']:.3f}(n={v['n']})" for k, v in bench["argmax_calibration"].items()))
    else:
        print("    无收盘价数据")

    print("\n[3] 盘口移动（开盘 → 收盘）")
    mv = line_movement(matches, args.devig)
    if mv.get("n"):
        print(f"    n={mv['n']}  平均绝对移动 {mv['mean_abs_move']}  最大 {mv['max_abs_move']}")
    else:
        print("    无同时具备开盘+收盘的样本")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "devig_method": args.devig,
        "inventory": inv,
        "market_benchmark": bench,
        "line_movement": mv,
    }

    if not args.no_walkforward:
        print(f"\n[4] walk-forward DC（refit 每 {args.refit_days} 天，"
              f"训练窗 {args.train_window_days} 天）")
        preds = walk_forward(matches, refit_days=args.refit_days,
                             train_window_days=args.train_window_days,
                             devig_method=args.devig)
        print("\n[5] 模型 vs 市场（同批配对）")
        cmp_ = compare(preds)
        if cmp_.get("n"):
            print(f"    n={cmp_['n']}")
            print(f"    Brier  模型 {cmp_['model_brier']}  市场 {cmp_['market_brier']}")
            print(f"           配对 Δ={cmp_['paired_brier']['mean']}  "
                  f"t={cmp_['paired_brier']['t']}  "
                  f"{'显著' if cmp_['paired_brier']['significant'] else '不显著'}")
            print(f"    logloss 模型 {cmp_['model_logloss']}  市场 {cmp_['market_logloss']}")
            print(f"           配对 Δ={cmp_['paired_logloss']['mean']}  "
                  f"t={cmp_['paired_logloss']['t']}  "
                  f"{'显著' if cmp_['paired_logloss']['significant'] else '不显著'}")
            print(f"    RPS    模型 {cmp_['model_rps']}  市场 {cmp_['market_rps']}")
            print(f"           配对 Δ={cmp_['paired_rps']['mean']}  "
                  f"t={cmp_['paired_rps']['t']}  "
                  f"{'显著' if cmp_['paired_rps']['significant'] else '不显著'}")
            print(f"    逐场 Brier：模型更好 {cmp_['wins']} / 市场更好 {cmp_['losses']}")
            if cmp_.get("by_league"):
                print("\n    分联赛（Brier Δ，负 = 模型更好）与 FDR：")
                for k, v in sorted(cmp_["by_league"].items(), key=lambda kv: kv[1]["delta_brier"]):
                    flag = "✅FDR显著" if v.get("fdr_reject") else "  "
                    print(f"      {k:<8} n={v['n']:<5} Δ={v['delta_brier']:+.5f} "
                          f"t={v['t']:>+6.2f}  p={v.get('p', 0):.4f} {flag}")
        else:
            print("    无有效配对样本")
        report["walk_forward"] = {"refit_days": args.refit_days,
                                  "train_window_days": args.train_window_days,
                                  "n_predictions": len(preds),
                                  "comparison": cmp_}
        report["_predictions_sample"] = preds[:200]

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n✓ 已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
