"""蒙特卡洛模拟模型 - 灵活处理复杂市场"""
import math
from dataclasses import dataclass

import numpy as np

from .base import MatchPrediction, PredictionModel, TeamRating


@dataclass
class MonteCarloConfig:
    simulations: int = 50000
    base_goals: float = 1.35
    elo_goal_weight: float = 0.62
    attack_weight: float = 1.0
    defense_weight: float = 0.9
    form_weight: float = 0.65
    home_advantage: float = 0.10
    # 时间衰减锚点 [天数, 权重]
    time_decay_anchors: list = None
    # 让球平滑权重
    handicap_smoothing: float = 0.274

    def __post_init__(self):
        if self.time_decay_anchors is None:
            self.time_decay_anchors = [
                [0, 1.0], [7, 0.987], [30, 0.946], [90, 0.844],
                [180, 0.712], [365, 0.507], [730, 0.258],
            ]


class MonteCarloModel(PredictionModel):
    """
    蒙特卡洛模拟模型。
    通过大量随机采样模拟比赛结果，能灵活处理让球、比分、总进球等复杂市场。
    使用确定性种子保证可复现。
    """

    def __init__(self, config: MonteCarloConfig | None = None):
        self.cfg = config or MonteCarloConfig()

    @property
    def name(self) -> str:
        return "monte_carlo"

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

        # 确定性种子（基于球队名 + xG，保证可复现）
        seed = hash((home.name, away.name, round(home_xg, 2), round(away_xg, 2)))
        rng = np.random.default_rng(abs(seed) % (2**32))

        # 蒙特卡洛模拟
        n = self.cfg.simulations
        home_goals = rng.poisson(home_xg, size=n)
        away_goals = rng.poisson(away_xg, size=n)

        # 胜平负统计
        home_wins = np.sum(home_goals > away_goals)
        draws = np.sum(home_goals == away_goals)
        away_wins = np.sum(home_goals < away_goals)

        home_win_prob = home_wins / n
        draw_prob = draws / n
        away_win_prob = away_wins / n

        # 淘汰赛：平局分配
        if is_knockout and draw_prob > 0.01:
            elo_diff = home.elo - away.elo
            home_extra = 0.58 * self._logistic(elo_diff / 400) + 0.42 * 0.5
            home_win_prob += draw_prob * home_extra
            away_win_prob += draw_prob * (1 - home_extra)
            draw_prob = 0.0

        # 归一化
        total = home_win_prob + draw_prob + away_win_prob
        home_win_prob /= total
        draw_prob /= total
        away_win_prob /= total

        # 让球概率
        hdp_h, hdp_d, hdp_a = 0.0, 0.0, 0.0
        if handicap is not None:
            adjusted = (home_goals - away_goals) + handicap
            hdp_h = float(np.sum(adjusted > 0.25)) / n
            hdp_a = float(np.sum(adjusted < -0.25)) / n
            hdp_d = 1.0 - hdp_h - hdp_a

            # 让球平滑：向正常概率收缩
            w = self.cfg.handicap_smoothing
            hdp_h = (1 - w) * hdp_h + w * home_win_prob
            hdp_d = (1 - w) * hdp_d + w * draw_prob
            hdp_a = (1 - w) * hdp_a + w * away_win_prob

        return MatchPrediction(
            match_id="",
            home_team=home.name,
            away_team=away.name,
            competition="",
            home_win_prob=round(float(home_win_prob), 4),
            draw_prob=round(float(draw_prob), 4),
            away_win_prob=round(float(away_win_prob), 4),
            home_xg=round(home_xg, 3),
            away_xg=round(away_xg, 3),
            handicap_home_prob=round(hdp_h, 4),
            handicap_draw_prob=round(hdp_d, 4),
            handicap_away_prob=round(hdp_a, 4),
            model_name=self.name,
            confidence=round(float(max(home_win_prob, draw_prob, away_win_prob)), 4),
        )

    def _expected_goals(
        self, home: TeamRating, away: TeamRating, is_neutral: bool
    ) -> tuple[float, float]:
        """计算期望进球（与 Dixon-Coles 共享逻辑）"""
        cfg = self.cfg
        base = math.log(cfg.base_goals)
        elo_term = (home.elo - away.elo) / 400 * cfg.elo_goal_weight

        log_home = (
            base
            + elo_term * 0.5
            + home.attack * cfg.attack_weight
            - away.defense * cfg.defense_weight
            + (home.form - away.form) * cfg.form_weight
            + (cfg.home_advantage if not is_neutral else 0)
        )
        log_away = (
            base
            - elo_term * 0.5
            + away.attack * cfg.attack_weight
            - home.defense * cfg.defense_weight
            + (away.form - home.form) * cfg.form_weight
        )

        home_xg = max(0.15, min(4.5, math.exp(log_home)))
        away_xg = max(0.15, min(4.5, math.exp(log_away)))
        return home_xg, away_xg

    def time_decay_weight(self, days_ago: int) -> float:
        """分段对数时间衰减（借鉴 lottery-football）"""
        anchors = self.cfg.time_decay_anchors
        if days_ago <= 0:
            return 1.0
        if days_ago >= anchors[-1][0]:
            return anchors[-1][1]

        # 找到区间
        for i in range(len(anchors) - 1):
            d0, w0 = anchors[i]
            d1, w1 = anchors[i + 1]
            if d0 <= days_ago <= d1:
                # 对数插值
                if d1 == d0:
                    return w0
                t = math.log(days_ago - d0 + 1) / math.log(d1 - d0 + 1)
                return w0 + t * (w1 - w0)

        return anchors[-1][1]

    @staticmethod
    def _logistic(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))
