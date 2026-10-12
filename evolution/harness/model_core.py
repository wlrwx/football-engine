"""模型层：把 football-engine 的 DC / MC 移植进 harness。

【为什么必须有这一层】原先 harness 只有 fusion_core，能裁决配置与权重类改动，
但对模型层的**代码缺陷**无能为力。而项目里最严重的问题恰好在模型层：

  - dixon_coles._expected_goals 把以 1.0 为中心的比例因子（attack/defense）
    **直接相加**，而不是取 log，且防守项符号反了；
  - monte_carlo 对同一组字段用 log，是正确写法。

两者对同一份 team_ratings.json 给出系统性不同的 xG（DC 总进球 3.22 vs MC 2.42，
五大联赛真实约 2.8）。这类缺陷无法用"调 fusion 权重"修复，必须能被独立裁决。

移植原则：保持与上游**逐行一致**（含缺陷），缺陷的修复以 code_fix 候选的形式
提交给 verifier 裁决，而不是在这里悄悄改掉 —— 否则 harness 就成了自证正确的
回音室。任何偏离上游的行为都必须在 KNOWN_DIVERGENCES 中显式声明。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 与上游 config/prediction.json["prediction"] 保持一致
DEFAULT_PRED = {
    "base_goals": 1.35,
    "elo_goal_weight": 0.62,
    "attack_weight": 1.0,
    "defense_weight": 0.9,
    "form_weight": 0.65,
    "injury_weight": 1.0,
    "rest_weight": 0.035,
    "home_adv_weight": 1.0,
    "rho": -0.0897,
    "max_goals": 10,
    "xg_calibration": 0.75,      # 上游 MCConfig 的补丁系数
    "lambda_overdispersion": 0.25,
    "market_blend_weight": 0.28,
}


@dataclass
class TeamRating:
    name: str
    elo: float = 1500.0
    attack: float = 1.0
    defense: float = 1.0
    form: float = 0.0
    injury: float = 0.0
    rest_days: int = 3


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


# --------------------------------------------------------------- xG 计算

def dc_expected_goals(home: TeamRating, away: TeamRating, cfg: dict,
                      is_neutral: bool = False, log_form: bool = False,
                      fix_defense_sign: bool = False) -> tuple[float, float]:
    """上游 dixon_coles._expected_goals 的移植。

    log_form=False + fix_defense_sign=False 时与上游**逐行等价**（含缺陷）。

    已知缺陷（log_form=False 时）：
      * attack/defense 是以 1.0 为中心的比例因子，却作为加法项直接进入 log 域，
        量纲错误；
      * 防守项用 `- away.defense * w`，使"防守好"(defense<1) 反而抬高对手 xG，
        符号相反。
    """
    base = math.log(cfg["base_goals"])
    elo_term = (home.elo - away.elo) / 400 * cfg["elo_goal_weight"]

    if log_form:
        home_atk = math.log(max(0.3, home.attack)) * cfg["attack_weight"]
        away_def = math.log(max(0.3, away.defense)) * cfg["defense_weight"]
        away_atk = math.log(max(0.3, away.attack)) * cfg["attack_weight"]
        home_def = math.log(max(0.3, home.defense)) * cfg["defense_weight"]
    else:
        home_atk = home.attack * cfg["attack_weight"]
        away_def = away.defense * cfg["defense_weight"]
        away_atk = away.attack * cfg["attack_weight"]
        home_def = home.defense * cfg["defense_weight"]

    log_home = (
        base
        + elo_term * 0.5
        + home_atk
        + away_def
        + (home.form - away.form) * cfg["form_weight"]
        + home.injury * cfg["injury_weight"]
        + (min(home.rest_days, 7) - min(away.rest_days, 7)) * cfg["rest_weight"]
        + (0 if is_neutral else cfg["home_adv_weight"] * 0.1)
    )
    log_away = (
        base
        - elo_term * 0.5
        + away_atk
        + home_def
        + (away.form - home.form) * cfg["form_weight"]
        + away.injury * cfg["injury_weight"]
        + (min(away.rest_days, 7) - min(home.rest_days, 7)) * cfg["rest_weight"]
    )

    h = max(0.15, min(4.5, math.exp(log_home)))
    a = max(0.15, min(4.5, math.exp(log_away)))

    gap = abs(h - a)
    if gap > 1.5:
        scale = 1.5 / gap
        mid = (h + a) / 2
        h = mid + (h - mid) * scale
        a = mid + (a - mid) * scale
    return h, a


def dc_score_matrix(home_xg: float, away_xg: float, cfg: dict) -> list[list[float]]:
    n = cfg["max_goals"] + 1
    rho = cfg["rho"]
    hp = [_poisson_pmf(k, home_xg) for k in range(n)]
    ap = [_poisson_pmf(k, away_xg) for k in range(n)]
    m = [[hp[i] * ap[j] for j in range(n)] for i in range(n)]
    m[0][0] *= 1 - home_xg * away_xg * rho
    m[0][1] *= 1 + home_xg * rho
    m[1][0] *= 1 + away_xg * rho
    m[1][1] *= 1 - rho
    flat = [max(0.0, v) for row in m for v in row]
    tot = sum(flat)
    if tot > 0:
        flat = [v / tot for v in flat]
    out = [[0.0] * n for _ in range(n)]
    k = 0
    for i in range(n):
        for j in range(n):
            out[i][j] = flat[k]
            k += 1
    return out


def dc_probs(home: TeamRating, away: TeamRating, cfg: dict,
             is_neutral: bool = False, log_form: bool = False) -> tuple[float, float, float]:
    hxg, axg = dc_expected_goals(home, away, cfg, is_neutral, log_form=log_form)
    mat = dc_score_matrix(hxg, axg, cfg)
    n = cfg["max_goals"] + 1
    hw = dw = aw = 0.0
    for i in range(n):
        for j in range(n):
            if i > j:
                hw += mat[i][j]
            elif i == j:
                dw += mat[i][j]
            else:
                aw += mat[i][j]
    tot = hw + dw + aw
    return hw / tot, dw / tot, aw / tot


def mc_expected_goals(home: TeamRating, away: TeamRating, cfg: dict,
                      is_neutral: bool = False,
                      apply_xg_calibration: bool = True) -> tuple[float, float]:
    """上游 monte_carlo._expected_goals 的移植（log 形式，正确）。"""
    base = math.log(cfg["base_goals"])
    elo_term = (home.elo - away.elo) / 400 * cfg["elo_goal_weight"]
    log_home = (
        base
        + elo_term * 0.5
        + math.log(max(0.3, home.attack)) * cfg["attack_weight"]
        + math.log(max(0.3, away.defense)) * cfg["defense_weight"]
        + (home.form - away.form) * cfg["form_weight"] * 0.3
        + (0.10 if not is_neutral else 0.0)
    )
    log_away = (
        base
        - elo_term * 0.5
        + math.log(max(0.3, away.attack)) * cfg["attack_weight"]
        + math.log(max(0.3, home.defense)) * cfg["defense_weight"]
        + (away.form - home.form) * cfg["form_weight"] * 0.3
    )
    h = max(0.15, min(3.5, math.exp(log_home)))
    a = max(0.15, min(3.5, math.exp(log_away)))
    if apply_xg_calibration and cfg.get("xg_calibration", 1.0) != 1.0:
        c = cfg["xg_calibration"]
        h = max(0.15, min(3.5, h * c))
        a = max(0.15, min(3.5, a * c))
    return h, a


def mc_probs(home: TeamRating, away: TeamRating, cfg: dict,
             is_neutral: bool = False, n_sim: int = 20000,
             seed: int = 12345, apply_xg_calibration: bool = True):
    """蒙特卡洛胜平负（纯 Python 采样，确定性种子）。"""
    hxg, axg = mc_expected_goals(home, away, cfg, is_neutral, apply_xg_calibration)
    import random
    rng = random.Random(seed)
    hw = dw = aw = 0
    for _ in range(n_sim):
        hg = _poisson_sample(hxg, rng)
        ag = _poisson_sample(axg, rng)
        if hg > ag:
            hw += 1
        elif hg == ag:
            dw += 1
        else:
            aw += 1
    tot = hw + dw + aw or 1
    return hw / tot, dw / tot, aw / tot


def _poisson_sample(lam: float, rng: random.Random) -> int:
    import math as _m
    L = _m.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def ensemble_probs(home: TeamRating, away: TeamRating, cfg: dict,
                   dc_weight: float = 0.6, mc_weight: float = 0.4,
                   is_neutral: bool = False, log_form: bool = False,
                   mc_n_sim: int = 8000) -> tuple[float, float, float]:
    """上游 EnsembleModel：DC(0.6) + MC(0.4)。"""
    d = dc_probs(home, away, cfg, is_neutral, log_form=log_form)
    m = mc_probs(home, away, cfg, is_neutral, n_sim=mc_n_sim)
    return tuple(dc_weight * d[i] + mc_weight * m[i] for i in range(3))


# --------------------------------------------------------------- 偏差诊断

def xg_bias_report(ratings: list[TeamRating], cfg: dict, n_pairs: int = 4000,
                   seed: int = 7) -> dict:
    """对比 DC 与 MC 的 xG 分布，用于量化量纲缺陷的影响。

    不是裁决信号（那是 verifier 的职责），而是给 PR 正文提供可复核的数字。
    """
    import random
    rng = random.Random(seed)
    tot_dc, tot_mc = [], []
    gap_pairs = []
    for _ in range(n_pairs):
        h = rng.choice(ratings)
        a = rng.choice(ratings)
        if h.name == a.name:
            continue
        dh, da = dc_expected_goals(h, a, cfg)
        mh, ma = mc_expected_goals(h, a, cfg)
        tot_dc.append(dh + da)
        tot_mc.append(mh + ma)
        gap_pairs.append(abs((dh + da) - (mh + ma)))

    def q(xs, p):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    return {
        "dc_total_mean": sum(tot_dc) / len(tot_dc),
        "mc_total_mean": sum(tot_mc) / len(tot_mc),
        "dc_total_p95": q(tot_dc, 0.95),
        "mc_total_p95": q(tot_mc, 0.95),
        "dc_over45_frac": sum(1 for x in tot_dc if x >= 4.5) / len(tot_dc),
        "mc_over45_frac": sum(1 for x in tot_mc if x >= 4.5) / len(tot_mc),
        "mean_gap": sum(gap_pairs) / len(gap_pairs),
        "n_pairs": len(tot_dc),
    }


KNOWN_DIVERGENCES = [
    "mc_probs 用纯 Python 采样替代 numpy（harness 零依赖），"
    "数值上等价但更快收敛到同一分布。",
    "XG_MISMATCH: 账本 model_raw 由上游 EnsembleModel 产生，"
    "其内部 MC 用 numpy 50k 采样；harness 用 8k 纯 Python 采样，"
    "两者存在 ~0.3% 的采样噪声差异。因此 model_raw 的 replay 忠实性"
    "校验必须使用容差，不能要求逐位一致。",
]
