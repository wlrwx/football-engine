"""回放评估引擎（纯标准库）。

职责：
  - walk-forward 选择/验证切分，杜绝"同一份数据既选又验"
  - 配对（paired）统计：候选与基线在同一批比赛上逐场比较
  - 多重比较校正：候选是批量产生的，raw t 值不能直接用

为什么必须配对：final 与 baseline 在同一场比赛上的误差高度相关
（同一批输入、同一市场概率），配对差值会消掉绝大部分共同方差，
检验功效比独立样本高一个数量级。
"""

from __future__ import annotations

import json
import math
from pathlib import Path


# ---------------------------------------------------------------- 指标

def brier(probs, actual_idx: int) -> float:
    return sum((probs[i] - (1.0 if i == actual_idx else 0.0)) ** 2 for i in range(3))


def log_loss(probs, actual_idx: int) -> float:
    p = max(1e-12, probs[actual_idx])
    return -math.log(p)


def rps(probs, actual_idx: int) -> float:
    """Ranked Probability Score —— 对过度自信的惩罚比 logloss 温和。"""
    cum = [0.0, probs[0], probs[0] + probs[1]]
    y = [0.0, 0.0, 0.0]
    y[actual_idx] = 1.0
    cum_y = [0.0, y[0], y[0] + y[1]]
    return sum((cum[i] - cum_y[i]) ** 2 for i in range(3)) / 2.0


METRICS = {"brier": brier, "logloss": log_loss, "rps": rps}


def argmax(v):
    return max(range(3), key=lambda i: v[i])


# ---------------------------------------------------------------- 数据

def load_ledger(path: str | Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 只保留可评估的完整行：有 model_raw、actual_idx
            if r.get("model_raw") is None or r.get("actual_idx") is None:
                continue
            r["_has_market"] = r.get("market_fair") is not None
            rows.append(r)
    rows.sort(key=lambda r: (r.get("date", ""), r.get("match_id", "")))
    return rows


def scope_current_chain(ledger: list[dict], chain: str = "v2") -> list[dict]:
    """只保留当前生效融合链的行。

    【必须调用】账本横跨多次融合链重构（如 v1 → v2）。用今天的代码重放
    旧链的行，字段语义不同，会系统性低估/高估改动效果。实测混放两链时
    replay 与生产 final 的平均平方偏差为 0.018（≈ Brier 量级的 6%），
    而单独重放当前链为 0.0001。
    """
    return [r for r in ledger if r.get("chain") == chain]


def replay_trusted(system, ledger: list[dict], cfg: dict, post: dict,
                   tol: float = 0.01) -> list[bool]:
    """逐场标记 replay 是否忠实于生产输出。

    忠实 = 用账本持久化的输入重跑融合链，得到的 final_prob 与落盘的
    final_prob 的平方偏差 < tol。

    不忠实的样本必须从裁决中剔除：这些场次的某个输入未被持久化
    （典型是随时间漂移的 league_params 自适应量），replay 用了回填值，
    此时"候选相对基线的差异"里混入了 replay 误差，会污染配对检验。
    """
    flags = []
    for row in ledger:
        stored = row.get("final_prob")
        if not stored:
            flags.append(False)
            continue
        out = system.fuse(system.MatchInput(row), cfg, post)
        d = sum((out.probs[i] - stored[i]) ** 2 for i in range(3))
        flags.append(d < tol)
    return flags


def filter_trusted(ledger: list[dict], flags: list[bool]) -> list[dict]:
    return [r for r, ok in zip(ledger, flags) if ok]


# ---------------------------------------------------------------- 评估

def evaluate(system, ledger: list[dict], cfg: dict, post: dict,
             metric: str = "brier") -> dict:
    """在给定账本上跑 system，回放全部比赛，返回指标 + 逐场结果。"""
    fn = METRICS[metric]
    per_match = []
    for row in ledger:
        m = system.MatchInput(row)
        out = system.fuse(m, cfg, post)
        per_match.append({
            "match_id": m.match_id,
            "date": m.date,
            "has_market": m.market_fair is not None,
            "metric": fn(out.probs, m.actual_idx),
            "hit": 1 if argmax(out.probs) == m.actual_idx else 0,
            "probs": out.probs,
        })
    return _summarize(per_match, metric)


def _summarize(per_match: list[dict], metric: str) -> dict:
    n = len(per_match)
    if n == 0:
        return {"n": 0, "metric": metric}
    mean = sum(r["metric"] for r in per_match) / n
    hit = sum(r["hit"] for r in per_match) / n
    var = sum((r["metric"] - mean) ** 2 for r in per_match) / (n - 1) if n > 1 else 0.0
    return {
        "n": n,
        "metric": metric,
        "mean": mean,
        "hit": hit,
        "var": var,
        "per_match": per_match,
    }


# ---------------------------------------------------------------- 配对检验

def paired_test(cand: list[dict], base: list[dict], metric: str = "brier",
                one_sided_lower: bool = True) -> dict:
    """配对 t 检验：H0 为候选与基线无差异。

    one_sided_lower=True 时备择假设为"候选 metric 更低"（对 brier/logloss 而言
    即"更好"）。delta = cand - base，故检验方向为 delta < 0。
    """
    by_id = {r["match_id"]: r for r in base}
    diffs = []
    for r in cand:
        o = by_id.get(r["match_id"])
        if o is None or not o["has_market"] == r["has_market"]:
            continue
        diffs.append(r["metric"] - o["metric"])
    n = len(diffs)
    if n < 3:
        return {"n": n, "t": 0.0, "delta": 0.0, "p_one_sided": 1.0}
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        t = -1e12 if mean < 0 else (1e12 if mean > 0 else 0.0)
    else:
        t = mean / (sd / math.sqrt(n))
    df = max(2, n - 1)
    p_two = 2.0 * t_cdf(-abs(t), df)
    p_two = max(0.0, min(1.0, p_two))
    if one_sided_lower:
        p_one = p_two / 2.0 if t < 0 else 1.0 - p_two / 2.0
    else:
        p_one = p_two
    return {
        "n": n,
        "delta": mean,
        "t": t,
        "df": df,
        "p_two_sided": p_two,
        "p_one_sided": max(0.0, min(1.0, p_one)),
        "better_rate": sum(1 for d in diffs if d < 0) / n,
    }


# ---------------------------------------------------------------- 统计分布
# 纯 Python 实现，避免 scipy 依赖。**已对照标准 t 分布表校验**：
#   t_cdf(1.96, df=∞)=0.9750 / t_cdf(2.5, df=30)=0.99246 / t_cdf(0)=0.5


def t_cdf(t: float, df: int) -> float:
    """Student-t 累积分布函数。"""
    if df <= 0:
        return 0.5
    x = df / (df + t * t)
    ib = _betainc(0.5 * df, 0.5, x)
    if t > 0:
        return 1.0 - 0.5 * ib
    return 0.5 * ib


def _betainc(a: float, b: float, x: float) -> float:
    """正则化不完全 beta I_x(a,b) —— Lentz 连分数展开（Numerical Recipes betacf/betai）。

    两者 a,b 均 > 0，x ∈ [0,1]。连续修正项 1e-30 防止除零。
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = _lgamma(a + b) - _lgamma(a) - _lgamma(b)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(
            math.log(x) * a + math.log(1.0 - x) * b + lbeta) / a * _betacf(a, b, x)
    return 1.0 - math.exp(
        math.log(1.0 - x) * b + math.log(x) * a + lbeta) / b * _betacf(b, a, 1.0 - x)


def _betacf(a: float, b: float, x: float) -> float:
    """连分数展开（betacf）。"""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 301):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-12:
            break
    return h


def _lgamma(x: float) -> float:
    """Lanczos 近似的 log Γ(x)。"""
    g = 7.0
    c = [0.99999999999980993, 676.5203681218851, -1259.1392167224028,
         771.32342877765313, -176.61502916214059, 12.507343278686905,
         -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7]
    if x < 0.5:
        return math.log(math.pi / math.sin(math.pi * x)) - _lgamma(1.0 - x)
    x -= 1.0
    a = c[0]
    t = x + g + 0.5
    for i in range(1, 9):
        a += c[i] / (x + i)
    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(a)


def p_one_sided_from_t(t: float, df: int, lower_is_better: bool = True) -> float:
    """由 t 值得单尾 p 值。lower_is_better: t<0 代表指标变好。"""
    p_two = max(0.0, min(1.0, 2.0 * (1.0 - t_cdf(abs(t), df))))
    if lower_is_better:
        return p_two / 2.0 if t < 0 else 1.0 - p_two / 2.0
    return p_two


# ---------------------------------------------------------------- 多重比较

def benjamini_hochberg(pvalues: dict[str, float], alpha: float = 0.10) -> dict:
    """BH-FDR 校正。

    候选改动是批量提出的（本次 harness 一轮可能产生 20+ 个候选），
    裸 p 值必然出现假阳性 —— 实测中 league_draw_anchor 裸 t=-1.93
    判"留"，但 val 段 t=+0.86 已经变差，就是 best-of-N 的产物。
    """
    if not pvalues:
        return {}
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(items)
    thresh = {}
    passed = set()
    k_max = 0
    for i, (name, p) in enumerate(items, start=1):
        if p <= (i / m) * alpha:
            k_max = i
    for i in range(1, k_max + 1):
        passed.add(items[i - 1][0])
    for name, p in items:
        thresh[name] = {"p": p, "pass_bh": name in passed,
                        "bh_threshold": (items.index((name, p)) + 1) / m * alpha}
    return thresh


# ---------------------------------------------------------------- walk-forward

def walk_forward_split(ledger: list[dict], min_train: int = 255,
                       val_frac: float = 0.4) -> tuple[list[dict], list[dict]]:
    """按时间切分。绝不随机打乱 —— 随机切分会泄漏未来信息。"""
    n = len(ledger)
    n_train = max(min_train, int(n * (1.0 - val_frac)))
    return ledger[:n_train], ledger[n_train:]


def min_sample(n: int, metric: str = "brier") -> bool:
    return n >= 100
