"""蒙特卡洛模拟模型 - 灵活处理复杂市场"""
import math
from dataclasses import dataclass, field

import numpy as np

from .base import MatchPrediction, PredictionModel, TeamRating
from .enhanced import build_seed, head_to_head_factor, top_score_predictions, top_total_goals


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
        h2h_history: list[dict] | None = None,
    ) -> MatchPrediction:
        # 计算期望进球（含 H2H 调整）
        h2h_factor = 1.0
        if h2h_history:
            h2h_factor = head_to_head_factor(home.name, away.name, h2h_history)

        home_xg, away_xg = self._expected_goals(home, away, is_neutral, h2h_factor)

        # 市场赔率校准xG：当球队缺少真实ratings时用赔率反推
        is_default_home = (home.elo == 1500.0 and home.attack == 1.0)
        is_default_away = (away.elo == 1500.0 and away.attack == 1.0)
        if market_odds and (is_default_home or is_default_away):
            odds_xg = self._xg_from_odds(market_odds)
            if odds_xg:
                if is_default_home and is_default_away:
                    home_xg, away_xg = odds_xg
                elif is_default_home:
                    # 只校准缺失的一方，保持另一方的模型xG
                    away_xg = odds_xg[1]
                    home_xg = odds_xg[0] * (home_xg / max(0.3, odds_xg[0]))
                else:
                    home_xg = odds_xg[0]
                    away_xg = odds_xg[1] * (away_xg / max(0.3, odds_xg[1]))

        # 确定性种子（借鉴 lottery-football: 相同输入永远相同输出）
        seed = build_seed(
            f"{home.name}_{away.name}", home_xg, away_xg, self.cfg.simulations
        )
        rng = np.random.default_rng(seed % (2**32))

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

        # 比分分布 & 总进球分布
        scores = top_score_predictions(home_goals, away_goals, top_n=8)
        totals = top_total_goals(home_goals, away_goals, top_n=6)

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
            top_scores=scores,
            top_total_goals=totals,
            model_name=self.name,
            confidence=round(float(max(home_win_prob, draw_prob, away_win_prob)), 4),
        )

    @staticmethod
    def _xg_from_odds(market_odds: tuple[float, float, float]) -> tuple[float, float] | None:
        """从市场赔率反推xG（用于缺少ratings的球队）
        
        原理: 平局概率与总进球强相关(高平局率=低进球),
        主客胜率比例决定进球分配。
        """
        oh, od, oa = market_odds
        if oh <= 1.0 or od <= 1.0 or oa <= 1.0:
            return None

        # 去水: 简单归一化
        imp_h, imp_d, imp_a = 1.0 / oh, 1.0 / od, 1.0 / oa
        total_imp = imp_h + imp_d + imp_a
        ph = imp_h / total_imp
        pd = imp_d / total_imp
        pa = imp_a / total_imp

        # 平局概率 → 总进球 (经验公式: pd≈0.30→2.0球, pd≈0.20→3.0球)
        total_goals = max(1.2, min(4.0, 4.2 - 6.5 * pd))

        # 主客胜率比例 → 进球分配
        # 加入主场优势微调(+0.15)
        home_share = (ph + 0.05) / (ph + pa + 0.10)
        home_xg = total_goals * home_share
        away_xg = total_goals * (1 - home_share)

        return (
            max(0.2, min(3.5, round(home_xg, 3))),
            max(0.2, min(3.5, round(away_xg, 3))),
        )

    def _expected_goals(
        self, home: TeamRating, away: TeamRating, is_neutral: bool, h2h_factor: float = 1.0
    ) -> tuple[float, float]:
        """计算期望进球（含 H2H 调整）
        
        attack/defense 是乘法比例因子(1.0=联赛平均)，取log后变为加法项。
        """
        cfg = self.cfg
        base = math.log(cfg.base_goals)
        elo_term = (home.elo - away.elo) / 400 * cfg.elo_goal_weight

        # attack/defense 是比例因子，log后做加法
        home_atk = math.log(max(0.3, home.attack)) * cfg.attack_weight
        away_def = math.log(max(0.3, away.defense)) * cfg.defense_weight
        away_atk = math.log(max(0.3, away.attack)) * cfg.attack_weight
        home_def = math.log(max(0.3, home.defense)) * cfg.defense_weight

        log_home = (
            base
            + elo_term * 0.5
            + home_atk
            + away_def  # defense<1.0=好防守, log为负, 降低对手xG
            + (home.form - away.form) * cfg.form_weight * 0.3
            + (cfg.home_advantage if not is_neutral else 0)
        )
        log_away = (
            base
            - elo_term * 0.5
            + away_atk
            + home_def  # defense<1.0=好防守, log为负, 降低对手xG
            + (away.form - home.form) * cfg.form_weight * 0.3
        )

        home_xg = max(0.15, min(3.5, math.exp(log_home)))
        away_xg = max(0.15, min(3.5, math.exp(log_away)))

        # H2H 调整：乘到主队，除到客队（借鉴 lottery-football）
        home_xg *= h2h_factor
        away_xg /= h2h_factor
        home_xg = max(0.15, min(3.5, home_xg))
        away_xg = max(0.15, min(3.5, away_xg))

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
