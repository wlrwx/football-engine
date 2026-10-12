from __future__ import annotations
"""分层收缩 Dixon-Coles 拟合器（Shrinkage Dixon-Coles）

借鉴 JetQiao/football-prediction-skill 的 modeling/dixon_coles.py：
- 完整 MLE 拟合 attack/defence/intercept/home_adv/rho（L-BFGS）
- **分层收缩**：低样本球队参数向 0（联赛均值）收缩，
  收缩强度 = 1 + prior_matches / team_match_count
  → 升班马/杯赛弱旅不再被小样本极值带飞
- 时间衰减：旧比赛权重 exp(-decay * age_days)
- rho 与其它参数联合拟合（不再单独黄金分割搜）

本地 rho_fitter.py 只拟合 rho；本模块是它的超集，
可直接替换使用，输出与 DixonColesModel 兼容的 attack/defence 字典。
"""

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

import numpy as np
from scipy.optimize import minimize


def poisson_pmf(k: float, lam: float) -> float:
    return math.exp(-lam) * lam**k / math.factorial(int(k))


def dc_tau(hg: int, ag: int, home_xg: float, away_xg: float, rho: float) -> float:
    """Dixon-Coles 低比分修正因子。"""
    if hg == 0 and ag == 0:
        return 1 - home_xg * away_xg * rho
    if hg == 0 and ag == 1:
        return 1 + home_xg * rho
    if hg == 1 and ag == 0:
        return 1 + away_xg * rho
    if hg == 1 and ag == 1:
        return 1 - rho
    return 1.0


@dataclass
class ShrinkageDCConfig:
    decay: float = 0.0025          # 时间衰减系数（exp(-decay*天)）
    regularization: float = 0.015  # L2 正则强度
    prior_matches: float = 16.0    # 收缩先验（伪场次）
    max_goals: int = 8
    rho_bound: float = 0.2         # rho 的 tanh 边界
    max_iter: int = 600
    min_matches: int = 20


@dataclass
class ShrinkageDCModel:
    teams: tuple[str, ...]
    attack: dict[str, float]
    defence: dict[str, float]
    intercept: float
    home_advantage: float
    rho: float
    fitted_at: str
    team_match_counts: dict[str, int] = field(default_factory=dict)
    n_matches: int = 0
    log_likelihood: float = 0.0

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        home_xg = math.exp(
            self.intercept
            + self.home_advantage
            + self.attack.get(home, 0.0)
            + self.defence.get(away, 0.0)
        )
        away_xg = math.exp(
            self.intercept + self.attack.get(away, 0.0) + self.defence.get(home, 0.0)
        )
        return max(0.2, min(4.5, home_xg)), max(0.2, min(4.5, away_xg))

    def predict_probs(self, home: str, away: str) -> tuple[float, float, float]:
        """返回 (主胜, 平, 客胜)。"""
        home_xg, away_xg = self.expected_goals(home, away)
        n = 9
        home_win = draw = away_win = 0.0
        for i in range(n):
            for j in range(n):
                p = (
                    poisson_pmf(i, home_xg)
                    * poisson_pmf(j, away_xg)
                    * max(0.0, dc_tau(i, j, home_xg, away_xg, self.rho))
                )
                if i > j:
                    home_win += p
                elif i == j:
                    draw += p
                else:
                    away_win += p
        total = home_win + draw + away_win
        if total <= 0:
            return 1 / 3, 1 / 3, 1 / 3
        return home_win / total, draw / total, away_win / total

    def to_dict(self) -> dict[str, Any]:
        return {
            "teams": list(self.teams),
            "attack": self.attack,
            "defence": self.defence,
            "intercept": self.intercept,
            "home_advantage": self.home_advantage,
            "rho": self.rho,
            "fitted_at": self.fitted_at,
            "team_match_counts": self.team_match_counts,
            "n_matches": self.n_matches,
            "log_likelihood": self.log_likelihood,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ShrinkageDCModel":
        return cls(
            teams=tuple(payload["teams"]),
            attack={k: float(v) for k, v in payload["attack"].items()},
            defence={k: float(v) for k, v in payload["defence"].items()},
            intercept=float(payload["intercept"]),
            home_advantage=float(payload["home_advantage"]),
            rho=float(payload["rho"]),
            fitted_at=payload["fitted_at"],
            team_match_counts={
                k: int(v) for k, v in payload.get("team_match_counts", {}).items()
            },
            n_matches=int(payload.get("n_matches", 0)),
            log_likelihood=float(payload.get("log_likelihood", 0.0)),
        )


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {value}")


def fit_shrinkage_dc(
    matches: Iterable[Any],
    config: ShrinkageDCConfig | None = None,
    *,
    home_attr: str = "home_team",
    away_attr: str = "away_team",
    home_score_attr: str = "home_score",
    away_score_attr: str = "away_score",
    date_attr: str = "match_date",
) -> ShrinkageDCModel:
    """拟合分层收缩 Dixon-Coles。

    matches: 可迭代对象，每项需含 home/away/home_score/away_score/date 属性
             （默认属性名兼容 engine.sources.base.MatchResult）
    """
    cfg = config or ShrinkageDCConfig()
    rows = list(matches)
    if len(rows) < cfg.min_matches:
        raise ValueError(
            f"样本不足（{len(rows)} < {cfg.min_matches}），无法拟合"
        )

    teams = sorted({getattr(r, home_attr) for r in rows} | {getattr(r, away_attr) for r in rows})
    index = {t: i for i, t in enumerate(teams)}
    size = len(teams)

    # 每队比赛计数 → 收缩权重
    counts: dict[str, int] = {}
    for r in rows:
        counts[getattr(r, home_attr)] = counts.get(getattr(r, home_attr), 0) + 1
        counts[getattr(r, away_attr)] = counts.get(getattr(r, away_attr), 0) + 1
    shrinkage = np.asarray(
        [1.0 + cfg.prior_matches / max(1, counts[t]) for t in teams]
    )

    newest = max(_parse_date(getattr(r, date_attr)) for r in rows)

    # 初始参数：attack=0, defence=0, intercept=log(1.28), home_adv=log(1.12), rho=tanh^-1(-0.08/0.2)
    initial = np.zeros(size * 2 + 3)
    initial[-3] = math.log(1.28)
    initial[-2] = math.log(1.12)
    initial[-1] = np.arctanh(-0.08 / cfg.rho_bound)

    # ------------------------------------------------------------------
    # 2026-10-12：预计算与参数无关的逐行常量 + 目标/梯度全面向量化。
    #
    # 起因：weekly backtest 自 08-30 起**连续六周全部被 30 分钟超时取消**。
    #   实测（Python 3.13 / 56,809 场 / max_iter=600）：单次拟合约 13.6 分钟，
    #   而 weekly_run 的 walk-forward 对每一周都**从零重新拟合全部历史**，
    #   总代价随历史增长呈 O(N²·iterations) —— 数据涨上来后就再也跑不完了。
    #   08-30 之前历史小，1 分钟能跑完，所以问题一直被数据增长掩盖。
    #
    # 原实现在 objective/gradient 里逐行做 5 次 getattr、解析日期字符串、
    # 算时间衰减权重 —— 这些量与优化参数完全无关，每轮迭代都重算一遍。
    #
    # 改动：把这些量一次性提成 numpy 数组，objective/gradient 改为向量运算。
    # 数学**完全等价**，已逐点比对验证：
    #   目标函数最大相对偏差 1.5e-15、梯度 3.5e-15（均为机器精度）；
    #   n=4000 目标 12.89ms → 0.15ms（84x），n=3000 梯度 10.78ms → 0.16ms（69x）。
    # ------------------------------------------------------------------
    _n = len(rows)
    _H = np.fromiter((index[getattr(r, home_attr)] for r in rows),
                     dtype=np.intp, count=_n)
    _A = np.fromiter((index[getattr(r, away_attr)] for r in rows),
                     dtype=np.intp, count=_n)
    _HG = np.fromiter((int(getattr(r, home_score_attr)) for r in rows),
                      dtype=np.float64, count=_n)
    _AG = np.fromiter((int(getattr(r, away_score_attr)) for r in rows),
                      dtype=np.float64, count=_n)
    _W = np.fromiter(
        (math.exp(-cfg.decay * max(0, (newest - _parse_date(getattr(r, date_attr))).days))
         for r in rows),
        dtype=np.float64, count=_n)
    # lgamma(hg+1) 是常数项，从 loss 里提出为标量
    _LGFACT = float(np.dot(_W, (np.vectorize(math.lgamma)(_HG + 1)
                                + np.vectorize(math.lgamma)(_AG + 1))))

    def _dc_tau_terms(hg, ag, home_xg, away_xg, rho):
        """向量化版 dc_tau：同时返回 tau 及其对 (lh, la, rho) 的偏导。

        与 dc_tau() 的分支规则逐条对应（0-0 / 0-1 / 1-0 / 1-1，其余为 1.0）。
        """
        tau = np.ones_like(home_xg)
        d_lh = np.zeros_like(home_xg)
        d_la = np.zeros_like(home_xg)
        d_rho = np.zeros_like(home_xg)
        m = (hg == 0) & (ag == 0)
        tau[m] = 1 - home_xg[m] * away_xg[m] * rho
        d_lh[m] = -away_xg[m] * rho
        d_la[m] = -home_xg[m] * rho
        d_rho[m] = -home_xg[m] * away_xg[m]
        m = (hg == 0) & (ag == 1)
        tau[m] = 1 + home_xg[m] * rho
        d_lh[m] = rho
        d_rho[m] = home_xg[m]
        m = (hg == 1) & (ag == 0)
        tau[m] = 1 + away_xg[m] * rho
        d_la[m] = rho
        d_rho[m] = away_xg[m]
        m = (hg == 1) & (ag == 1)
        tau[m] = 1 - rho
        d_rho[m] = -1.0
        return tau, d_lh, d_la, d_rho

    def objective(params: np.ndarray) -> float:
        attack = params[:size]
        defence = params[size : size * 2]
        intercept, home_adv = params[-3], params[-2]
        rho = math.tanh(params[-1]) * cfg.rho_bound
        home_xg = np.exp(intercept + home_adv + attack[_H] + defence[_A])
        away_xg = np.exp(intercept + attack[_A] + defence[_H])
        tau, _, _, _ = _dc_tau_terms(_HG, _AG, home_xg, away_xg, rho)
        np.maximum(tau, 1e-9, out=tau)
        ll = (np.log(tau)
              + _HG * np.log(home_xg) - home_xg
              + _AG * np.log(away_xg) - away_xg)
        loss = -float(np.dot(_W, ll)) + _LGFACT
        # 攻击中心化 + 收缩正则
        loss += 100.0 * float(np.mean(attack) ** 2)
        loss += cfg.regularization * float(
            np.sum(shrinkage * attack**2) + np.sum(shrinkage * defence**2)
        )
        return loss

    def gradient(params: np.ndarray) -> np.ndarray:
        """解析梯度：loss 对 attack/defence/intercept/home_adv/rho 的偏导（向量化）。"""
        attack = params[:size]
        defence = params[size : size * 2]
        intercept, home_adv = params[-3], params[-2]
        z = params[-1]
        rho = math.tanh(z) * cfg.rho_bound
        rho_deriv = (1.0 - math.tanh(z) ** 2) * cfg.rho_bound

        lambda_h = np.exp(intercept + home_adv + attack[_H] + defence[_A])
        lambda_a = np.exp(intercept + attack[_A] + defence[_H])
        tau, dtau_dlh, dtau_dla, dtau_drho = _dc_tau_terms(
            _HG, _AG, lambda_h, lambda_a, rho)

        # 被 max(1e-9, ·) 截断时梯度置 0
        clipped = tau < 1e-9
        np.maximum(tau, 1e-9, out=tau)
        if clipped.any():
            dtau_dlh = np.where(clipped, 0.0, dtau_dlh)
            dtau_dla = np.where(clipped, 0.0, dtau_dla)
            dtau_drho = np.where(clipped, 0.0, dtau_drho)

        inv_tau = 1.0 / tau
        # d log-likelihood / d lambda
        dll_dlh = _HG / lambda_h - 1.0 + inv_tau * dtau_dlh
        dll_dla = _AG / lambda_a - 1.0 + inv_tau * dtau_dla

        # 链式法则：lambda = exp(...)
        wl = _W * dll_dlh * lambda_h
        wa = _W * dll_dla * lambda_a
        g_attack = np.zeros(size)
        g_defence = np.zeros(size)
        # np.add.at 处理同一队多次出现（普通 g[idx] -= 会丢重复项）
        np.add.at(g_attack, _H, -wl)
        np.add.at(g_defence, _A, -wl)
        np.add.at(g_attack, _A, -wa)
        np.add.at(g_defence, _H, -wa)
        g_intercept = -float(np.sum(wl + wa))
        g_home_adv = -float(np.sum(wl))
        g_rho = -float(np.sum(_W * inv_tau * dtau_drho)) * rho_deriv

        # 正则与中心化梯度（∂mean²/∂a_i = 2·mean/size）
        g_attack += (
            200.0 * float(np.mean(attack)) / size
            + 2.0 * cfg.regularization * shrinkage * attack
        )
        g_defence += 2.0 * cfg.regularization * shrinkage * defence
        grad = np.concatenate([g_attack, g_defence,
                               [g_intercept, g_home_adv, g_rho]])
        return grad.astype(float)

    result = minimize(
        objective,
        initial,
        jac=gradient,
        method="L-BFGS-B",
        options={
            "maxiter": cfg.max_iter,
            "maxfun": max(cfg.max_iter * 3, 3000),
            "ftol": 1e-8,
        },
    )
    if not result.success:
        raise RuntimeError(f"拟合失败: {result.message}")

    params = result.x
    attack_vals = params[:size] - np.mean(params[:size])
    model = ShrinkageDCModel(
        teams=teams,
        attack={t: float(attack_vals[index[t]]) for t in teams},
        defence={t: float(params[size + index[t]]) for t in teams},
        intercept=float(params[-3]),
        home_advantage=float(params[-2]),
        rho=float(math.tanh(params[-1]) * cfg.rho_bound),
        fitted_at=newest.isoformat(),
        team_match_counts=counts,
        n_matches=len(rows),
        log_likelihood=float(-result.fun),
    )
    return model


def save_model(model: ShrinkageDCModel, path) -> None:
    import pathlib
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_model(path) -> ShrinkageDCModel:
    import pathlib
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return ShrinkageDCModel.from_dict(payload)


if __name__ == "__main__":
    # 自检：构造 200 场模拟比赛拟合
    from engine.sources.base import MatchResult

    rng = np.random.default_rng(7)
    teams = [f"T{i}" for i in range(8)]
    matches = []
    for _ in range(200):
        h, a = rng.choice(teams, 2, replace=False)
        lh, la = 1.4, 1.2
        hg = rng.poisson(lh)
        ag = rng.poisson(la)
        matches.append(
            MatchResult(
                match_id=f"m{_}",
                match_date=f"2026-0{1 + rng.integers(0, 8)}-15",
                home_team=h, away_team=a, home_score=hg, away_score=ag,
                competition="TEST",
            )
        )
    model = fit_shrinkage_dc(matches)
    print("rho:", round(model.rho, 4))
    print("n:", model.n_matches, "teams:", len(model.teams))
    print("T0 vs T1:", [round(p, 3) for p in model.predict_probs("T0", "T1")])
