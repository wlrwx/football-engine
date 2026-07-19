"""参数自动优化器 - 多阶段搜索（借鉴 lottery-football）"""
import itertools
import json
import random
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OptimizationResult:
    """优化结果"""
    best_params: dict
    best_roi: float
    iterations: int
    method: str
    history: list = None


class ParamOptimizer:
    """
    多阶段参数优化器。
    粗网格 → 坐标下降 → 随机搜索 → 局部精调。
    借鉴 lottery-football 的 optimize-backtest-parameters 设计。
    """

    def __init__(self, backtest_fn, param_space: dict[str, list]):
        """
        Args:
            backtest_fn: 回测函数，接收参数字典，返回 ROI
            param_space: 参数搜索空间 {param_name: [values]}
        """
        self.backtest_fn = backtest_fn
        self.param_space = param_space
        self.history: list[dict] = []

    def optimize(
        self,
        min_matches: int = 100,
        min_avg_odds: float = 1.5,
        max_iterations: int = 500,
    ) -> OptimizationResult:
        """执行多阶段优化"""
        best_params = {}
        best_roi = -999.0
        iterations = 0

        # 阶段 1：粗网格搜索
        print("[优化] 阶段 1: 粗网格搜索...")
        coarse_space = {k: v[::max(1, len(v)//5)] for k, v in self.param_space.items()}
        for combo in itertools.product(*coarse_space.values()):
            params = dict(zip(coarse_space.keys(), combo))
            roi = self._evaluate(params, min_matches, min_avg_odds)
            iterations += 1
            if roi > best_roi:
                best_roi = roi
                best_params = params.copy()
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
                    roi = self._evaluate(test_params, min_matches, min_avg_odds)
                    iterations += 1
                    if roi > best_roi:
                        best_roi = roi
                        best_params = test_params.copy()
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
            roi = self._evaluate(test_params, min_matches, min_avg_odds)
            iterations += 1
            if roi > best_roi:
                best_roi = roi
                best_params = test_params.copy()

        # 阶段 4：精细局部搜索
        print("[优化] 阶段 4: 精细局部搜索...")
        fine_space = self._fine_grid(best_params)
        for combo in itertools.product(*fine_space.values()):
            params = dict(zip(fine_space.keys(), combo))
            roi = self._evaluate(params, min_matches, min_avg_odds)
            iterations += 1
            if roi > best_roi:
                best_roi = roi
                best_params = params.copy()
            if iterations >= max_iterations:
                break

        return OptimizationResult(
            best_params=best_params,
            best_roi=best_roi,
            iterations=iterations,
            method="multi_phase",
            history=self.history,
        )

    def _evaluate(self, params: dict, min_matches: int, min_avg_odds: float) -> float:
        """评估一组参数"""
        try:
            result = self.backtest_fn(params)
            roi = result.get("roi", -999)
            matches = result.get("match_count", 0)
            avg_odds = result.get("avg_odds", 0)

            # 约束检查
            if matches < min_matches:
                roi = -999
            if avg_odds < min_avg_odds:
                roi = -999

            self.history.append({"params": params, "roi": roi, "matches": matches})
            return roi
        except Exception:
            return -999

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
