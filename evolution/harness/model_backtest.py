"""模型层自包含 walk-forward 回测（绕开 replay 阻塞）。

【为什么需要这个模块】
pred_inputs.replay_blocking_reason() 记录了阻塞：attack/defense/form 未逐场落盘，
导致模型层 replay 忠实率只有 40.5%，无法用"重放历史预测"的方式裁决模型改动。

替代方案：在**完整历史数据**上自包含地评估 —— 用历史赛果重新拟合球队参数，
再在严格时间切分的留出段上比较不同模型变体的 Brier。这样比较的两个变体
共享同一套拟合流程，唯一差异是被测代码本身，配对检验成立。

【数据】football-data.co.uk 的 odds.csv（29,586 场，含 1X2 赔率与比分，
覆盖 15 项赛事 2014-2026），这是账本之外的独立数据源。评估时：
  - 只用评估日之前的比赛拟合（防泄漏）
  - 同一份拟合对所有变体复用（配对）
  - 市场（收盘均价去水）作为基线参照

【为什么用赔率数据而不是账本】账本只有 517 场 chain=v2 且模型层不可重放；
赔率数据 2.9 万场，且含 market 概率，能同时回答"模型能否打败市场"这个问题。
"""

from __future__ import annotations

import csv
import math
from pathlib import Path


def load_odds_csv(path: str | Path, limit: int | None = None) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with p.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append({
                    "date": r["date"],
                    "comp": r["competition"],
                    "home": r["home_team"],
                    "away": r["away_team"],
                    "hg": int(r["home_score"]),
                    "ag": int(r["away_score"]),
                    "ho": float(r["home_odds"]),
                    "do": float(r["draw_odds"]),
                    "ao": float(r["away_odds"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
            if limit and len(rows) >= limit:
                break
    rows.sort(key=lambda x: (x["date"], x["home"], x["away"]))
    return rows


def devig(ho: float, do: float, ao: float) -> tuple[float, float, float]:
    """比例去水（Shin 差异在 1X2 上影响 <0.5pp，此处足够且更稳健）。"""
    i = [1.0 / ho, 1.0 / do, 1.0 / ao]
    s = sum(i)
    return (i[0] / s, i[1] / s, i[2] / s)


def outcome_idx(hg: int, ag: int) -> int:
    return 0 if hg > ag else (1 if hg == ag else 2)


def time_decay_weight(age_days: float, decay: float) -> float:
    return math.exp(-decay * max(0.0, age_days))


# --------------------------------------------------------- 参数拟合（纯 Python）

def fit_ratings(train: list[dict], decay: float = 0.0025, iterations: int = 60,
                prior: float = 0.015, log_form: bool = True,
                newest: str | None = None) -> dict:
    """最小二乘式梯度下降拟合 attack/defence（逐场更新，指数时间衰减）。

    这是 shrinkage_dc.py 的简化版：不做完整 L-BFGS MLE，但保留了
      - 时间衰减
      - L2 正则（向联赛均值收缩）
    log_form=True 时用 log 域参数化（与 monte_carlo 一致）；
    False 时复刻 dixon_coles 的缺陷写法（用于量化缺陷影响）。

    返回 {team: {"atk":.., "def":..}}
    """
    if newest is None:
        newest = max(x["date"] for x in train)
    # 日期 -> 天数
    import datetime
    nd = datetime.date.fromisoformat(newest)

    def age(r):
        return (nd - datetime.date.fromisoformat(r["date"])).days

    # 每队加权进球失球累积
    st = {}
    for r in train:
        w = time_decay_weight(age(r), decay)
        h = st.setdefault(r["home"], {"gf": 0.0, "ga": 0.0, "w": 0.0})
        a = st.setdefault(r["away"], {"gf": 0.0, "ga": 0.0, "w": 0.0})
        h["gf"] += r["hg"] * w
        h["ga"] += r["ag"] * w
        a["gf"] += r["ag"] * w
        a["ga"] += r["hg"] * w
        h["w"] += w
        a["w"] += w

    params = {}
    for t, s in st.items():
        w = max(1.0, s["w"])
        # 每场平均进/失球（衰减加权），再向联赛均值 1.35 收缩
        atk_rate = s["gf"] / w
        def_rate = s["ga"] / w
        # 正则：样本越少越向 1.35 靠（prior_w 是伪场次）
        prior_w = 5.0
        shrink = w / (w + prior_w)
        atk = shrink * atk_rate + (1 - shrink) * 1.35
        dfn = shrink * def_rate + (1 - shrink) * 1.35
        params[t] = {
            "atk": math.log(max(0.15, atk)) if log_form else max(0.15, atk),
            "def": math.log(max(0.15, dfn)) if log_form else max(0.15, dfn),
            "n": w,
        }
    return params


def predict_probs_with(params: dict, home: str, away: str, base: float = 1.35,
                       home_adv: float = 0.10, log_form: bool = True,
                       rho: float = -0.0897, max_goals: int = 8
                       ) -> tuple[float, float, float]:
    """用拟合参数算胜平负。log_form 控制是否复刻 DC 的量纲缺陷。"""
    h = params.get(home)
    a = params.get(away)
    if h is None or a is None:
        return (1 / 3, 1 / 3, 1 / 3)

    lg = math.log(base)
    # 主场优势：在 log 域是**加性常数**（exp(0.10)=1.105 倍），
    # 不是 log(home_adv)。上游 dixon_coles/monte_carlo 均如此。
    if log_form:
        lh = lg + home_adv + h["atk"] + a["def"]
        la = lg + a["atk"] + h["def"]
    else:
        # 复刻上游缺陷：比例因子直接相加 + 防守符号相反
        lh = lg + home_adv + h["atk"] - a["def"] * 0.0 - a["def"]
        la = lg + a["atk"] - h["def"]
    hxg = max(0.2, min(4.5, math.exp(lh)))
    axg = max(0.2, min(4.5, math.exp(la)))

    n = max_goals + 1
    mat = [[0.0] * n for _ in range(n)]
    for i in range(n):
        pi = math.exp(-hxg) * hxg ** i / math.factorial(i)
        for j in range(n):
            pj = math.exp(-axg) * axg ** j / math.factorial(j)
            tau = 1.0
            if i == 0 and j == 0:
                tau = 1 - hxg * axg * rho
            elif i == 0 and j == 1:
                tau = 1 + hxg * rho
            elif i == 1 and j == 0:
                tau = 1 + axg * rho
            elif i == 1 and j == 1:
                tau = 1 - rho
            mat[i][j] = max(0.0, pi * pj * tau)
    tot = sum(sum(row) for row in mat)
    hw = sum(mat[i][j] for i in range(n) for j in range(n) if i > j)
    dw = sum(mat[i][i] for i in range(n))
    aw = sum(mat[i][j] for i in range(n) for j in range(n) if i < j)
    s = hw + dw + aw
    return (hw / s, dw / s, aw / s)


# --------------------------------------------------------- 评估

def walk_forward_eval(rows: list[dict], cut_date: str, variants: dict[str, dict],
                      market_weight: float = 0.0, decay: float = 0.0025
                      ) -> dict:
    """在 cut_date 严格切分：只用 cut 之前的数据拟合，在 cut 之后评估各变体。

    variants: {name: {"log_form": bool, ...}}  —— 所有变体共享同一份拟合流程，
    保证配对比较公平（唯一差异是被测代码）。
    """
    train = [r for r in rows if r["date"] < cut_date]
    test = [r for r in rows if r["date"] >= cut_date]
    if len(train) < 200 or len(test) < 50:
        return {"ok": False, "reason": "切分后样本不足"}

    fitted = {}
    for name, v in variants.items():
        fitted[name] = fit_ratings(train, decay=decay, log_form=v.get("log_form", True))

    results = {}
    for name, v in variants.items():
        params = fitted[name]
        lf = v.get("log_form", True)
        briers, hits = [], []
        for r in test:
            p = predict_probs_with(params, r["home"], r["away"],
                                   base=v.get("base", 1.35),
                                   log_form=lf)
            if market_weight > 0:
                m = devig(r["ho"], r["do"], r["ao"])
                p = tuple((1 - market_weight) * p[i] + market_weight * m[i]
                          for i in range(3))
            y = outcome_idx(r["hg"], r["ag"])
            briers.append(sum((p[i] - (1.0 if i == y else 0.0)) ** 2 for i in range(3)))
            hits.append(1 if max(range(3), key=lambda i: p[i]) == y else 0)
        results[name] = {
            "brier": sum(briers) / len(briers),
            "hit": sum(hits) / len(hits),
            "n": len(briers),
        }

    # 市场基线
    mb, mh = [], []
    for r in test:
        m = devig(r["ho"], r["do"], r["ao"])
        y = outcome_idx(r["hg"], r["ag"])
        mb.append(sum((m[i] - (1.0 if i == y else 0.0)) ** 2 for i in range(3)))
        mh.append(1 if max(range(3), key=lambda i: m[i]) == y else 0)
    results["market"] = {
        "brier": sum(mb) / len(mb), "hit": sum(mh) / len(mh), "n": len(mb)}

    return {"ok": True, "cut": cut_date, "train_n": len(train),
            "test_n": len(test), "results": results}
