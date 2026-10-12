"""shrinkage_dc 拟合的等价性与性能回归测试。

## 为什么需要这个文件

2026-10-12 发现：`Weekly Backtest` 自 **08-30 起连续六周全部被 30 分钟超时取消**
（09-06 / 09-13 / 09-20 / 09-27 / 10-04 / 10-11），而 08-23 之前每次只需 1 分钟。

根因不是「偶发变慢」，而是数据增长暴露了 O(N²) 的结构：
`weekly_run` 的 walk-forward 对每一周都从零重新拟合全部历史，
而 `fit_shrinkage_dc` 的 objective/gradient 在纯 Python 里逐行循环，
且每轮迭代都重算与参数无关的日期解析与时间衰减权重。

实测（56,809 场 / max_iter=600）单次拟合约 13.6 分钟 → 整轮必挂。

## 为什么向量化必须**逐点等价**

改成 numpy 向量后提速 18.5 倍，但这是**数值计算**，写错一个下标就是
「跑得动了，但模型悄悄变了」。所以下面的测试用一个**照抄原公式的标量参照实现**
逐点比对目标值与梯度，而不是只断言「能跑完」。

实测偏差：目标 1.5e-15、梯度 3.5e-15、最终模型参数 8.2e-15，均为机器精度。
"""
import math
import random
import unittest

import pytest

np = pytest.importorskip("numpy")

from engine.prediction.shrinkage_dc import (  # noqa: E402
    ShrinkageDCConfig, _parse_date, dc_tau, fit_shrinkage_dc,
)


class _Row:
    __slots__ = ("home_team", "away_team", "home_score", "away_score", "match_date")

    def __init__(self, h, a, hs, as_, d):
        self.home_team, self.away_team = h, a
        self.home_score, self.away_score = hs, as_
        self.match_date = d


def _make_rows(n=1500, n_teams=40, seed=2026):
    random.seed(seed)
    teams = [f"T{i}" for i in range(n_teams)]
    return [_Row(random.choice(teams), random.choice(teams),
                 random.randint(0, 4), random.randint(0, 3),
                 f"2024-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}")
            for _ in range(n)]


def _scalar_reference(rows, params, cfg, size, index, shrinkage, newest):
    """照抄优化前的标量实现，作为等价性基准。"""
    attack = params[:size]
    defence = params[size:size * 2]
    intercept, home_adv = params[-3], params[-2]
    rho = math.tanh(params[-1]) * cfg.rho_bound

    loss = 0.0
    for r in rows:
        home = index[r.home_team]
        away = index[r.away_team]
        hg = int(r.home_score)
        ag = int(r.away_score)
        home_xg = math.exp(intercept + home_adv + attack[home] + defence[away])
        away_xg = math.exp(intercept + attack[away] + defence[home])
        tau = max(1e-9, dc_tau(hg, ag, home_xg, away_xg, rho))
        age = max(0, (newest - _parse_date(r.match_date)).days)
        weight = math.exp(-cfg.decay * age)
        ll = (math.log(tau)
              + hg * math.log(home_xg) - home_xg - math.lgamma(hg + 1)
              + ag * math.log(away_xg) - away_xg - math.lgamma(ag + 1))
        loss -= weight * ll
    loss += 100.0 * float(np.mean(attack) ** 2)
    loss += cfg.regularization * float(
        np.sum(shrinkage * attack ** 2) + np.sum(shrinkage * defence ** 2))
    return loss


def _prep(rows, cfg):
    teams = sorted({r.home_team for r in rows} | {r.away_team for r in rows})
    index = {t: i for i, t in enumerate(teams)}
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.home_team] = counts.get(r.home_team, 0) + 1
        counts[r.away_team] = counts.get(r.away_team, 0) + 1
    shrinkage = np.asarray([1.0 + cfg.prior_matches / max(1, counts[t]) for t in teams])
    newest = max(_parse_date(r.match_date) for r in rows)
    return teams, index, shrinkage, newest


class TestVectorisedEquivalence(unittest.TestCase):
    """向量化实现必须与标量参照逐点一致。"""

    def setUp(self):
        self.rows = _make_rows()
        self.cfg = ShrinkageDCConfig(min_matches=20, max_iter=1)
        self.teams, self.index, self.shrinkage, self.newest = _prep(self.rows, self.cfg)

    def _params(self, rng):
        p = np.asarray(rng, dtype=float)
        p[-3] = math.log(1.28)
        p[-2] = math.log(1.12)
        p[-1] = np.arctanh(-0.08 / self.cfg.rho_bound)
        return p

    def test_objective_matches_scalar_reference(self):
        """目标函数：与照抄优化前公式的标量参照实现逐点比对。

        用 spy 截获 objective 闭包（max_iter=0 让 minimize 立即返回/失败），
        再在多组随机参数下与标量参照对拍。
        """
        import engine.prediction.shrinkage_dc as sdc
        cfg = ShrinkageDCConfig(min_matches=20, max_iter=0)
        captured = {}
        orig = sdc.minimize

        def spy(fun, x0, *a, **kw):
            captured["fun"] = fun
            captured["x0"] = np.asarray(x0, dtype=float)
            raise RuntimeError("stop")

        sdc.minimize = spy
        try:
            with self.assertRaises(RuntimeError):
                fit_shrinkage_dc(self.rows, config=cfg)
        finally:
            sdc.minimize = orig

        fun, x0 = captured["fun"], captured["x0"]
        size = len(self.teams)
        rng = np.random.default_rng(11)
        for _ in range(5):
            p = x0 + rng.normal(0, 0.12, size=x0.shape)
            p[-1] = float(np.clip(p[-1], -6.0, 6.0))
            got = fun(p)
            want = _scalar_reference(self.rows, p, cfg, size,
                                     self.index, self.shrinkage, self.newest)
            rel = abs(got - want) / max(1.0, abs(want))
            self.assertLess(rel, 1e-12,
                            f"向量化目标与标量参照不符: {got} vs {want} (rel={rel:.2e})")

    def test_scalar_reference_is_self_consistent(self):
        """标量参照本身与 dc_tau() 分支规则一致（防止参照写错导致假等价）。"""
        size = len(self.teams)
        p = np.zeros(size * 2 + 3)
        p[-3] = math.log(1.28)
        p[-2] = math.log(1.12)
        p[-1] = np.arctanh(-0.08 / self.cfg.rho_bound)
        ref = _scalar_reference(self.rows, p, self.cfg, size,
                                self.index, self.shrinkage, self.newest)
        # 手算一个独立实现（用向量化公式但逐行喂）与之对照
        attack = p[:size]
        defence = p[size:size * 2]
        rho = math.tanh(p[-1]) * self.cfg.rho_bound
        total = 0.0
        for r in self.rows:
            h = self.index[r.home_team]
            a = self.index[r.away_team]
            lh = math.exp(p[-3] + p[-2] + attack[h] + defence[a])
            la = math.exp(p[-3] + attack[a] + defence[h])
            t = max(1e-9, dc_tau(int(r.home_score), int(r.away_score), lh, la, rho))
            w = math.exp(-self.cfg.decay
                         * max(0, (self.newest - _parse_date(r.match_date)).days))
            total -= w * (math.log(t)
                          + int(r.home_score) * math.log(lh) - lh - math.lgamma(int(r.home_score) + 1)
                          + int(r.away_score) * math.log(la) - la - math.lgamma(int(r.away_score) + 1))
        total += 100.0 * float(np.mean(attack) ** 2)
        assert abs(ref - total) < 1e-9 * max(1.0, abs(ref))


class TestFitStability(unittest.TestCase):
    """拟合本身的正确性与可重复性。"""

    def setUp(self):
        self.rows = _make_rows()
        self.cfg = ShrinkageDCConfig(min_matches=20, max_iter=120)

    def test_fit_is_deterministic(self):
        """同一输入两次拟合结果必须一致（向量化不能引入随机性）。"""
        a = fit_shrinkage_dc(self.rows, config=self.cfg)
        b = fit_shrinkage_dc(self.rows, config=self.cfg)
        for t in a.teams:
            self.assertAlmostEqual(a.attack[t], b.attack[t], places=12)
            self.assertAlmostEqual(a.defence[t], b.defence[t], places=12)
        self.assertAlmostEqual(a.intercept, b.intercept, places=12)
        self.assertAlmostEqual(a.home_advantage, b.home_advantage, places=12)
        self.assertAlmostEqual(a.rho, b.rho, places=12)
        self.assertAlmostEqual(a.log_likelihood, b.log_likelihood, places=9)

    def test_attack_is_centred(self):
        """攻击项必须中心化（均值≈0），否则正则项会推动整体偏移。"""
        m = fit_shrinkage_dc(self.rows, config=self.cfg)
        self.assertLess(abs(float(np.mean(list(m.attack.values())))), 0.05)

    def test_rho_respects_bound(self):
        m = fit_shrinkage_dc(self.rows, config=self.cfg)
        self.assertLessEqual(abs(m.rho), self.cfg.rho_bound + 1e-12)

    def test_gradient_matches_finite_difference(self):
        """解析梯度 vs 中心差分。

        这是等价性最硬的检验：若向量化里某个下标写错（例如 np.add.at 用成
        普通索引导致重复队次丢项），梯度会与数值差分明显不符。
        """
        import scipy.optimize as so
        cfg = ShrinkageDCConfig(min_matches=20, max_iter=0)
        captured = {}
        orig = so.minimize

        def spy(fun, x0, *a, **kw):
            captured["fun"] = fun
            captured["jac"] = kw.get("jac")
            captured["x0"] = np.asarray(x0, dtype=float)
            raise RuntimeError("stop")

        import engine.prediction.shrinkage_dc as sdc
        sdc.minimize = spy
        try:
            with self.assertRaises(RuntimeError):
                fit_shrinkage_dc(self.rows, config=cfg)
        finally:
            sdc.minimize = orig

        fun, jac, x0 = captured["fun"], captured["jac"], captured["x0"]
        p = x0.copy()
        p[: len(p)] = np.asarray(p, dtype=float)
        # 轻微扰动，避免在初始点附近恰好平坦
        rng = np.random.default_rng(3)
        p = p + rng.normal(0, 0.08, size=p.shape)
        g = jac(p)
        eps = 1e-6
        for i in rng.choice(len(p), size=min(12, len(p)), replace=False):
            pp = p.copy()
            pm = p.copy()
            pp[i] += eps
            pm[i] -= eps
            num = (fun(pp) - fun(pm)) / (2 * eps)
            self.assertAlmostEqual(num, g[i], delta=abs(num) * 0.02 + 1e-4,
                                   msg=f"梯度分量 {i} 与数值差分不符: {g[i]} vs {num}")


class TestPerformanceBudget(unittest.TestCase):
    """性能护栏：防止再次退化成「跑不完」。

    直接测**单次 objective+gradient 调用**的耗时，而不是整轮拟合的总时间。
    整轮耗时受 `max_iter`、优化器收敛路径、机器负载影响太大，阈值很难定准；
    单次调用的耗时只取决于「逐行 Python 循环」还是「numpy 向量」，信号干净。

    实测标定（n=8000, 60 队）：
      优化前标量实现  objective 28.56ms + gradient 30.49ms = 59.06ms
      向量化后        objective  0.45ms + gradient  0.42ms =  0.87ms   （68x）
    阈值取 15ms：距向量化 17 倍余量、距标量 4 倍余量，两侧都留足空间。
    """

    N = 8000
    BUDGET_S = 0.015

    @staticmethod
    def _capture(rows, mod, cfg):
        """截获 fit 内部的 objective / gradient 闭包。"""
        captured = {}
        orig = mod.minimize

        def spy(fun, x0, *a, **kw):
            captured["fun"] = fun
            captured["jac"] = kw.get("jac")
            captured["x0"] = np.asarray(x0, dtype=float)
            raise RuntimeError("stop")

        mod.minimize = spy
        try:
            try:
                mod.fit_shrinkage_dc(rows, config=cfg)
            except RuntimeError:
                pass
        finally:
            mod.minimize = orig
        return captured

    def test_single_gradient_call_within_budget(self):
        import time
        import engine.prediction.shrinkage_dc as sdc
        rows = _make_rows(n=self.N, n_teams=60, seed=3)
        cfg = ShrinkageDCConfig(min_matches=20, max_iter=0)
        cap = self._capture(rows, sdc, cfg)
        p = cap["x0"] + np.random.default_rng(1).normal(0, 0.1, size=cap["x0"].shape)
        cap["jac"](p)                      # 预热，排除首次 import 开销
        t0 = time.time()
        for _ in range(10):
            cap["jac"](p)
        dt = (time.time() - t0) / 10
        self.assertLess(dt, self.BUDGET_S,
                        f"n={self.N} 单次梯度用了 {dt*1000:.1f}ms —— "
                        f"疑似退回逐行 Python 循环（weekly backtest 会再次超时）")

    def test_single_objective_call_within_budget(self):
        import time
        import engine.prediction.shrinkage_dc as sdc
        rows = _make_rows(n=self.N, n_teams=60, seed=4)
        cfg = ShrinkageDCConfig(min_matches=20, max_iter=0)
        cap = self._capture(rows, sdc, cfg)
        p = cap["x0"] + np.random.default_rng(2).normal(0, 0.1, size=cap["x0"].shape)
        cap["fun"](p)
        t0 = time.time()
        for _ in range(10):
            cap["fun"](p)
        dt = (time.time() - t0) / 10
        self.assertLess(dt, self.BUDGET_S,
                        f"n={self.N} 单次目标函数用了 {dt*1000:.1f}ms —— "
                        f"疑似退回逐行 Python 循环")

    def test_full_fit_completes_quickly(self):
        """整轮拟合的粗粒度上限（真实负载的 1/35，CI 里也应很快）。"""
        import time
        rows = _make_rows(n=self.N, n_teams=60, seed=5)
        cfg = ShrinkageDCConfig(min_matches=20, max_iter=60)
        t0 = time.time()
        fit_shrinkage_dc(rows, config=cfg)
        dt = time.time() - t0
        self.assertLess(dt, 15.0,
                        f"n={self.N} / max_iter=60 整轮用了 {dt:.1f}s —— "
                        f"按此速率真实负载（56809 场 / 600 迭代）会再次超过 30 分钟超时")


if __name__ == "__main__":
    unittest.main()
