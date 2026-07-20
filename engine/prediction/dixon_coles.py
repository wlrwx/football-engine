from __future__ import annotations
"""Dixon-Coles 泊松模型 - 解析解，速度快"""
import math
from dataclasses import dataclass

import numpy as np

from .base import MatchPrediction, PredictionModel, TeamRating


@dataclass
class DixonColesConfig:
    base_goals: float = 1.35
    elo_goal_weight: float = 0.62
    attack_weight: float = 1.0
    defense_weight: float = 0.9
    form_weight: float = 0.65
    injury_weight: float = 1.0
    rest_weight: float = 0.035
    home_adv_weight: float = 1.0
    market_blend_weight: float = 0.28
    rho: float = -0.10
    max_goals: int = 10


class DixonColesModel(PredictionModel):
    """
    Dixon-Coles 双变量泊松模型。
    解析计算比分概率矩阵，无需模拟，速度极快。
    """

    def __init__(self, config: DixonColesConfig | None = None):
        self.cfg = config or DixonColesConfig()

    @property
    def name(self) -> str:
        return "dixon_coles"

    def predict(
        self,
        home: TeamRating,
        away: TeamRating,
        market_odds: tuple[float, float, float] | None = None,
        handicap: float | None = None,
        is_neutral: bool = False,
        is_knockout: bool = False,
    ) -> MatchPrediction:
        # 计算期望进球
        home_xg, away_xg = self._expected_goals(home, away, is_neutral)

        # 计算比分概率矩阵
        score_matrix = self._score_distribution(home_xg, away_xg)

        # 从矩阵提取胜平负
        home_win = float(np.tril(score_matrix, -1).sum())
        draw = float(np.trace(score_matrix))
        away_win = float(np.triu(score_matrix, 1).sum())

        # 淘汰赛处理：平局概率分配给加时/点球
        if is_knockout and draw > 0.01:
            elo_diff = home.elo - away.elo
            home_extra = 0.58 * self._logistic(elo_diff / 400) + 0.42 * 0.5
            home_win += draw * home_extra
            away_win += draw * (1 - home_extra)
            draw = 0.0

        # 归一化
        total = home_win + draw + away_win
        if total > 0:
            home_win /= total
            draw /= total
            away_win /= total

        # 市场混合
        if market_odds and all(o and o > 1.0 for o in market_odds):
            market_probs = self._de_vig(market_odds)
            w = self.cfg.market_blend_weight
            home_win = (1 - w) * home_win + w * market_probs[0]
            draw = (1 - w) * draw + w * market_probs[1]
            away_win = (1 - w) * away_win + w * market_probs[2]

        # 让球概率
        hdp_h, hdp_d, hdp_a = 0.0, 0.0, 0.0
        if handicap is not None:
            hdp_h, hdp_d, hdp_a = self._handicap_probs(score_matrix, handicap)

        return MatchPrediction(
            match_id="",
            home_team=home.name,
            away_team=away.name,
            competition="",
            home_win_prob=round(home_win, 4),
            draw_prob=round(draw, 4),
            away_win_prob=round(away_win, 4),
            home_xg=round(home_xg, 3),
            away_xg=round(away_xg, 3),
            handicap_home_prob=round(hdp_h, 4),
            handicap_draw_prob=round(hdp_d, 4),
            handicap_away_prob=round(hdp_a, 4),
            model_name=self.name,
            confidence=round(max(home_win, draw, away_win), 4),
        )

    def _expected_goals(
        self, home: TeamRating, away: TeamRating, is_neutral: bool
    ) -> tuple[float, float]:
        """计算双方期望进球"""
        cfg = self.cfg
        base = math.log(cfg.base_goals)

        # Elo 差项
        elo_term = (home.elo - away.elo) / 400 * cfg.elo_goal_weight

        # 主队期望进球
        log_home = (
            base
            + elo_term * 0.5
            + home.attack * cfg.attack_weight
            - away.defense * cfg.defense_weight
            + (home.form - away.form) * cfg.form_weight
            + home.injury * cfg.injury_weight
            + (min(home.rest_days, 7) - min(away.rest_days, 7)) * cfg.rest_weight
            + (0 if is_neutral else cfg.home_adv_weight * 0.1)
        )

        # 客队期望进球
        log_away = (
            base
            - elo_term * 0.5
            + away.attack * cfg.attack_weight
            - home.defense * cfg.defense_weight
            + (away.form - home.form) * cfg.form_weight
            + away.injury * cfg.injury_weight
            + (min(away.rest_days, 7) - min(home.rest_days, 7)) * cfg.rest_weight
        )

        home_xg = max(0.15, min(4.5, math.exp(log_home)))
        away_xg = max(0.15, min(4.5, math.exp(log_away)))

        return home_xg, away_xg

    def _score_distribution(self, lambda_h: float, lambda_a: float) -> np.ndarray:
        """Dixon-Coles 比分概率矩阵（含低比分修正）"""
        n = self.cfg.max_goals + 1
        rho = self.cfg.rho

        # 独立泊松概率
        home_probs = np.array([self._poisson_pmf(k, lambda_h) for k in range(n)])
        away_probs = np.array([self._poisson_pmf(k, lambda_a) for k in range(n)])

        # 外积得到联合分布
        matrix = np.outer(home_probs, away_probs)

        # Dixon-Coles 低比分修正
        matrix[0, 0] *= 1 - lambda_h * lambda_a * rho
        matrix[0, 1] *= 1 + lambda_h * rho
        matrix[1, 0] *= 1 + lambda_a * rho
        matrix[1, 1] *= 1 - rho

        # 确保非负并归一化
        matrix = np.maximum(matrix, 0)
        total = matrix.sum()
        if total > 0:
            matrix /= total

        return matrix

    def _handicap_probs(
        self, score_matrix: np.ndarray, handicap: float
    ) -> tuple[float, float, float]:
        """从比分矩阵计算让球概率"""
        n = score_matrix.shape[0]
        home_win, draw, away_win = 0.0, 0.0, 0.0

        for i in range(n):
            for j in range(n):
                adjusted_diff = (i - j) + handicap  # 主队让球后的净胜球
                p = score_matrix[i, j]
                if adjusted_diff > 0.25:
                    home_win += p
                elif adjusted_diff < -0.25:
                    away_win += p
                else:
                    draw += p

        total = home_win + draw + away_win
        if total > 0:
            return home_win / total, draw / total, away_win / total
        return 0.0, 0.0, 0.0

    @staticmethod
    def _poisson_pmf(k: int, lam: float) -> float:
        """泊松概率质量函数"""
        return math.exp(-lam) * (lam ** k) / math.factorial(k)

    @staticmethod
    def _de_vig(odds: tuple[float, float, float]) -> tuple[float, float, float]:
        """去除 vig，归一化为概率"""
        implied = [1.0 / o for o in odds]
        total = sum(implied)
        return tuple(p / total for p in implied)

    @staticmethod
    def _logistic(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))
