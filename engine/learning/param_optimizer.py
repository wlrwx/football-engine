from __future__ import annotations
"""参数自动优化器 - 多阶段搜索（借鉴 lottery-football）"""
import itertools
import random
from dataclasses import dataclass


@dataclass
class OptimizationResult:
    """优化结果"""
    best_params: dict
    best_roi: float
    iterations: int
    method: str
    history: list = None
    objective: str = "roi"
    best_score: float = -999.0
    val_score: float | None = None


class ParamOptimizer:
    """
    多阶段参数优化器。
    粗网格 → 坐标下降 → 随机搜索 → 局部精调。
    支持严格概率评分目标（neg_log_loss / neg_rps / neg_brier）与样本外验证回调。
    """

    def __init__(
        self,
        backtest_fn,
        param_space: dict[str, list],
        objective: str = "neg_log_loss",
        val_backtest_fn=None,
    ):
        """
        Args:
            backtest_fn: 回测函数，接收参数字典，返回指标字典（含 log_loss/rps/brier/roi）
            param_space: 参数搜索空间 {param_name: [values]}
            objective: 优化目标 ("neg_log_loss" | "neg_rps" | "neg_brier" | "roi")
            val_backtest_fn: 可选样本外验证回调，接收最优参数字典并评估 OOS 分数
        """
        self.backtest_fn = backtest_fn
        self.param_space = param_space
        self.objective = objective
        self.val_backtest_fn = val_backtest_fn
        self.history: list[dict] = []

    def optimize(
        self,
        min_matches: int = 100,
        min_avg_odds: float = 1.5,
        max_iterations: int = 500,
    ) -> OptimizationResult:
        """执行多阶段优化"""
        best_params = {}
        best_score = -999.0
        best_roi = -999.0
        iterations = 0

        def _consider(cand: dict):
            nonlocal best_score, best_roi, best_params, iterations
            score, roi = self._evaluate(cand, min_matches, min_avg_odds)
            iterations += 1
            if score > best_score:
                best_score = score
                best_roi = roi
                best_params = cand.copy()

        # 阶段 1：粗网格搜索
        print("[优化] 阶段 1: 粗网格搜索...")
        coarse_space = {k: v[::max(1, len(v)//5)] for k, v in self.param_space.items()}
        for combo in itertools.product(*coarse_space.values()):
            params = dict(zip(coarse_space.keys(), combo))
            _consider(params)
            if iterations >= max_iterations * 0.3:
                break

        # 阶段 2：坐标下降
        print("[优化] 阶段 2: 坐标下降...")
        for _ in range(3):  # 3 轮
            improved = False
            for param_name, values in self.param_space.items():
                for val in values:
                    test_params = best_params.copy()
                    test_params[param_name] = val
                    prev_best = best_score
                    _consider(test_params)
                    if best_score > prev_best:
                        improved = True
                    if iterations >= max_iterations * 0.6:
                        break
                if iterations >= max_iterations * 0.6:
                    break
            if not improved:
                break

        # 阶段 3：随机搜索（在最优附近）
        print("[优化] 阶段 3: 随机局部搜索...")
        while iterations < max_iterations * 0.85:
            test_params = self._perturb(best_params)
            _consider(test_params)

        # 阶段 4：精细局部搜索
        print("[优化] 阶段 4: 精细局部搜索...")
        fine_space = self._fine_grid(best_params)
        for combo in itertools.product(*fine_space.values()):
            params = dict(zip(fine_space.keys(), combo))
            _consider(params)
            if iterations >= max_iterations:
                break

        val_score = None
        if self.val_backtest_fn is not None and best_params:
            try:
                val_res = self.val_backtest_fn(best_params)
                val_score = self._extract_score(val_res)
            except Exception:
                val_score = None

        return OptimizationResult(
            best_params=best_params,
            best_roi=best_roi,
            iterations=iterations,
            method="multi_phase",
            history=self.history,
            objective=self.objective,
            best_score=best_score,
            val_score=val_score,
        )

    def _extract_score(self, result: dict) -> float:
        if self.objective == "neg_log_loss" and result.get("log_loss") is not None:
            return -float(result["log_loss"])
        if self.objective == "neg_rps" and result.get("rps") is not None:
            return -float(result["rps"])
        if self.objective == "neg_brier" and result.get("brier") is not None:
            return -float(result["brier"])
        return float(result.get("roi", -999.0))

    def _evaluate(self, params: dict, min_matches: int, min_avg_odds: float) -> tuple[float, float]:
        """评估一组参数，返回 (target_score, roi)"""
        try:
            result = self.backtest_fn(params)
            roi = float(result.get("roi", -999.0))
            score = self._extract_score(result)
            matches = result.get("match_count", 0)
            avg_odds = result.get("avg_odds", 0)

            # 约束检查
            if matches < min_matches or avg_odds < min_avg_odds:
                score = -999.0
                roi = -999.0

            self.history.append({
                "params": params,
                "score": score,
                "roi": roi,
                "matches": matches,
            })
            return score, roi
        except Exception:
            return -999.0, -999.0

    def _perturb(self, params: dict) -> dict:
        """在最优参数附近随机扰动"""
        new_params = params.copy()
        # 随机选一个参数扰动
        key = random.choice(list(self.param_space.keys()))
        values = self.param_space[key]
        current_idx = values.index(params[key]) if params[key] in values else len(values) // 2
        # 在邻近值中选
        offset = random.choice([-2, -1, 0, 1, 2])
        new_idx = max(0, min(len(values) - 1, current_idx + offset))
        new_params[key] = values[new_idx]
        return new_params

    def _fine_grid(self, center: dict) -> dict[str, list]:
        """在最优参数周围生成精细网格"""
        fine = {}
        for key, values in self.param_space.items():
            if center.get(key) in values:
                idx = values.index(center[key])
                start = max(0, idx - 2)
                end = min(len(values), idx + 3)
                fine[key] = values[start:end]
            else:
                fine[key] = values[:3]
        return fine
