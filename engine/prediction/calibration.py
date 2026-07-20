from __future__ import annotations
"""多市场校准 + Shin去水 + 对数意见池 + 温度校准
移植自 football-prediction-skill (JetQiao)
"""
import math
from dataclasses import dataclass

import numpy as np


# ============================================================
# Shin 去水法（比简单归一化更精确）
# ============================================================
def devig_shin(odds: list[float]) -> list[float]:
    """
    Shin (1993) 去水模型。
    考虑内幕交易者对赔率的影响，求解参数 z。
    比简单 1/odds 归一化更准确地还原真实概率。
    """
    n = len(odds)
    implied = [1.0 / o for o in odds]
    overround = sum(implied)

    if overround <= 1.0:
        return implied  # 无 vig

    # 二分法求解 z (Shin 参数)
    lo, hi = 0.0, 1.0 - 1.0 / max(implied)
    for _ in range(100):
        z = (lo + hi) / 2
        probs = []
        valid = True
        for imp in implied:
            inner = z ** 2 + 4 * (1 - z) * imp ** 2 / overround
            if inner < 0:
                valid = False
                break
            p = (math.sqrt(inner) - z) / (2 * (1 - z))
            probs.append(p)
        if not valid:
            hi = z
            continue
        total = sum(probs)
        if abs(total - 1.0) < 1e-8:
            return probs
        elif total > 1.0:
            lo = z
        else:
            hi = z

    # fallback: 简单归一化
    return [imp / overround for imp in implied]


def devig_power(odds: list[float]) -> list[float]:
    """
    幂法去水：求解 sum(p_i^k) = 1 的指数 k。
    修正热门-冷门偏差（favorite-longshot bias）。
    """
    implied = [1.0 / o for o in odds]
    overround = sum(implied)
    if overround <= 1.0:
        return implied

    # 二分法求 k
    lo, hi = 0.5, 3.0
    for _ in range(100):
        k = (lo + hi) / 2
        total = sum(p ** k for p in implied)
        if abs(total - 1.0) < 1e-8:
            break
        elif total > 1.0:
            lo = k
        else:
            hi = k

    probs = [p ** k for p in implied]
    s = sum(probs)
    return [p / s for p in probs]


def devig_multiplicative(odds: list[float]) -> list[float]:
    """简单乘法去水（归一化）"""
    implied = [1.0 / o for o in odds]
    total = sum(implied)
    return [p / total for p in implied]


def select_devig_method(odds: list[float]) -> list[float]:
    """自动选择去水方法（默认 Shin，最精确）"""
    return devig_shin(odds)


# ============================================================
# 对数意见池（Logarithmic Opinion Pool）
# ============================================================
def log_opinion_pool(
    model_probs: list[float],
    market_probs: list[float],
    market_weight: float = 0.58,
) -> list[float]:
    """
    对数意见池融合：几何平均。
    exp((1-w)*log(p_model) + w*log(p_market))
    比算术平均更尊重极端概率，不会把 0.01 和 0.99 平均成 0.5。
    """
    eps = 1e-10
    fused = []
    for pm, pk in zip(model_probs, market_probs):
        pm = max(pm, eps)
        pk = max(pk, eps)
        log_fused = (1 - market_weight) * math.log(pm) + market_weight * math.log(pk)
        fused.append(math.exp(log_fused))

    # 归一化
    total = sum(fused)
    return [p / total for p in fused]


# ============================================================
# 温度校准（Temperature Scaling）
# ============================================================
def temperature_scale(logits: list[float], temperature: float) -> list[float]:
    """
    温度缩放：logits / T → softmax。
    T > 1: 软化（减少过度自信）
    T < 1: 锐化（增加自信）
    """
    scaled = [l / temperature for l in logits]
    max_l = max(scaled)
    exps = [math.exp(l - max_l) for l in scaled]
    total = sum(exps)
    return [e / total for e in exps]


def probs_to_logits(probs: list[float]) -> list[float]:
    """概率转 logits"""
    eps = 1e-10
    return [math.log(max(p, eps)) for p in probs]


def fit_temperature(
    predictions: list[list[float]],
    outcomes: list[int],
) -> float:
    """
    拟合最优温度参数（最小化 NLL）。
    predictions: [[p_home, p_draw, p_away], ...]
    outcomes: [0=home, 1=draw, 2=away, ...]
    需要 >= 30 样本。
    """
    if len(predictions) < 30:
        return 1.0

    logits_list = [probs_to_logits(p) for p in predictions]

    best_t = 1.0
    best_nll = float("inf")

    # 网格搜索 [0.35, 3.0]
    for t_int in range(35, 301, 5):
        t = t_int / 100.0
        nll = 0.0
        for logits, outcome in zip(logits_list, outcomes):
            probs = temperature_scale(logits, t)
            nll -= math.log(max(probs[outcome], 1e-10))
        if nll < best_nll:
            best_nll = nll
            best_t = t

    return best_t


# ============================================================
# 多市场 KL 散度校准
# ============================================================
@dataclass
class MarketOdds:
    """一场比赛的所有盘口赔率"""
    had: tuple[float, float, float] | None = None  # 胜平负
    hhad: tuple[float, float, float] | None = None  # 让球胜平负
    handicap: float = 0.0
    ttg: list[float] | None = None  # 总进球 (0,1,2,3,4,5,6,7+)
    crs: list[float] | None = None  # 比分 (31种)
    hafu: list[float] | None = None  # 半全场 (9种)


# 各市场权重（借鉴 football-prediction-skill）
MARKET_WEIGHTS = {
    "had": 1.0,
    "hhad": 1.1,
    "ttg": 1.0,
    "crs": 0.65,
    "hafu": 0.4,
}


def kl_divergence(p: list[float], q: list[float]) -> float:
    """KL(P || Q)"""
    eps = 1e-10
    return sum(pi * math.log(max(pi, eps) / max(qi, eps)) for pi, qi in zip(p, q))


def score_matrix_to_markets(
    matrix: np.ndarray,
    handicap: float = 0.0,
) -> dict[str, list[float]]:
    """从比分矩阵推导各盘口概率"""
    n = matrix.shape[0]
    results = {}

    # HAD
    home_win = float(np.tril(matrix, -1).sum())
    draw = float(np.trace(matrix))
    away_win = float(np.triu(matrix, 1).sum())
    total = home_win + draw + away_win
    if total > 0:
        results["had"] = [home_win / total, draw / total, away_win / total]

    # HHAD (让球)
    hdp_h, hdp_d, hdp_a = 0.0, 0.0, 0.0
    for i in range(n):
        for j in range(n):
            diff = (i - j) + handicap
            p = matrix[i, j]
            if diff > 0.25:
                hdp_h += p
            elif diff < -0.25:
                hdp_a += p
            else:
                hdp_d += p
    hdp_total = hdp_h + hdp_d + hdp_a
    if hdp_total > 0:
        results["hhad"] = [hdp_h / hdp_total, hdp_d / hdp_total, hdp_a / hdp_total]

    # TTG (总进球 0-7+)
    ttg = []
    for g in range(min(8, 2 * n)):
        prob = 0.0
        for i in range(n):
            j = g - i
            if 0 <= j < n:
                prob += matrix[i, j]
        ttg.append(float(prob))
    # 7+ 合并
    if len(ttg) > 8:
        ttg[7] = sum(ttg[7:])
        ttg = ttg[:8]
    ttg_total = sum(ttg)
    if ttg_total > 0:
        results["ttg"] = [p / ttg_total for p in ttg]

    return results


def multi_market_calibration(
    home_xg: float,
    away_xg: float,
    market: MarketOdds,
    max_iter: int = 50,
    lr: float = 0.05,
) -> tuple[float, float]:
    """
    多市场 KL 校准：调整 xG 使比分矩阵同时解释所有盘口。
    返回校准后的 (home_xg, away_xg)。

    核心思想：博彩公司在不同盘口的定价包含互补信息，
    让所有盘口同时"投票"确定最可能的比分分布。
    """
    best_hxg, best_axg = home_xg, away_xg
    best_kl = float("inf")

    for iteration in range(max_iter):
        # 构建比分矩阵
        matrix = _build_matrix(best_hxg, best_axg)
        derived = score_matrix_to_markets(matrix, market.handicap)

        # 计算加权 KL
        total_kl = 0.0
        for mkt_name, mkt_odds in [
            ("had", market.had),
            ("hhad", market.hhad),
            ("ttg", market.ttg),
        ]:
            if mkt_odds is None:
                continue
            market_probs = devig_shin(list(mkt_odds))
            model_probs = derived.get(mkt_name)
            if model_probs and len(model_probs) == len(market_probs):
                w = MARKET_WEIGHTS.get(mkt_name, 1.0)
                total_kl += w * kl_divergence(market_probs, model_probs)

        if total_kl < best_kl:
            best_kl = total_kl

        # 梯度方向：简单坐标下降
        # 尝试微调 home_xg 和 away_xg
        improved = False
        for dh in [-lr, lr]:
            for da in [-lr, lr]:
                test_h = max(0.2, min(4.5, best_hxg + dh))
                test_a = max(0.2, min(4.5, best_axg + da))
                test_matrix = _build_matrix(test_h, test_a)
                test_derived = score_matrix_to_markets(test_matrix, market.handicap)

                test_kl = 0.0
                for mkt_name, mkt_odds in [
                    ("had", market.had),
                    ("hhad", market.hhad),
                    ("ttg", market.ttg),
                ]:
                    if mkt_odds is None:
                        continue
                    market_probs = devig_shin(list(mkt_odds))
                    model_probs = test_derived.get(mkt_name)
                    if model_probs and len(model_probs) == len(market_probs):
                        w = MARKET_WEIGHTS.get(mkt_name, 1.0)
                        test_kl += w * kl_divergence(market_probs, model_probs)

                if test_kl < total_kl:
                    best_hxg, best_axg = test_h, test_a
                    total_kl = test_kl
                    improved = True

        if not improved:
            lr *= 0.5  # 学习率衰减
            if lr < 0.001:
                break

    return best_hxg, best_axg


def _build_matrix(home_xg: float, away_xg: float, max_goals: int = 10) -> np.ndarray:
    """构建泊松比分矩阵"""
    n = max_goals + 1
    home_probs = np.array([_poisson_pmf(k, home_xg) for k in range(n)])
    away_probs = np.array([_poisson_pmf(k, away_xg) for k in range(n)])
    matrix = np.outer(home_probs, away_probs)
    total = matrix.sum()
    if total > 0:
        matrix /= total
    return matrix


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)
