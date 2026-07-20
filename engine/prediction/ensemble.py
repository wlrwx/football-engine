from __future__ import annotations
"""集成模型 - 多模型加权融合"""
from .base import MatchPrediction, PredictionModel, TeamRating
from .dixon_coles import DixonColesModel, DixonColesConfig
from .monte_carlo import MonteCarloModel, MonteCarloConfig


class EnsembleModel(PredictionModel):
    """
    集成预测：并行运行多个模型，加权融合结果。
    权重可通过 champion/challenger 机制动态调整。
    """

    def __init__(
        self,
        dc_config: DixonColesConfig | None = None,
        mc_config: MonteCarloConfig | None = None,
        weights: dict[str, float] | None = None,
    ):
        self.models: list[PredictionModel] = [
            DixonColesModel(dc_config),
            MonteCarloModel(mc_config),
        ]
        # 默认权重
        self.weights = weights or {"dixon_coles": 0.6, "monte_carlo": 0.4}

    @property
    def name(self) -> str:
        return "ensemble"

    def predict(
        self,
        home: TeamRating,
        away: TeamRating,
        market_odds: tuple[float, float, float] | None = None,
        handicap: float | None = None,
        is_neutral: bool = False,
        is_knockout: bool = False,
    ) -> MatchPrediction:
        predictions = []
        for model in self.models:
            pred = model.predict(
                home=home,
                away=away,
                market_odds=market_odds,
                handicap=handicap,
                is_neutral=is_neutral,
                is_knockout=is_knockout,
            )
            predictions.append(pred)

        # 加权融合
        total_weight = sum(self.weights.get(p.model_name, 0) for p in predictions)
        if total_weight == 0:
            total_weight = len(predictions)

        fused = MatchPrediction(
            match_id="",
            home_team=home.name,
            away_team=away.name,
            competition="",
            model_name=self.name,
        )

        for pred in predictions:
            w = self.weights.get(pred.model_name, 1.0) / total_weight
            fused.home_win_prob += pred.home_win_prob * w
            fused.draw_prob += pred.draw_prob * w
            fused.away_win_prob += pred.away_win_prob * w
            fused.home_xg += pred.home_xg * w
            fused.away_xg += pred.away_xg * w
            fused.handicap_home_prob += pred.handicap_home_prob * w
            fused.handicap_draw_prob += pred.handicap_draw_prob * w
            fused.handicap_away_prob += pred.handicap_away_prob * w

        # 归一化
        prob_sum = fused.home_win_prob + fused.draw_prob + fused.away_win_prob
        if prob_sum > 0:
            fused.home_win_prob /= prob_sum
            fused.draw_prob /= prob_sum
            fused.away_win_prob /= prob_sum

        fused.home_win_prob = round(fused.home_win_prob, 4)
        fused.draw_prob = round(fused.draw_prob, 4)
        fused.away_win_prob = round(fused.away_win_prob, 4)
        fused.home_xg = round(fused.home_xg, 3)
        fused.away_xg = round(fused.away_xg, 3)
        fused.confidence = round(max(fused.home_win_prob, fused.draw_prob, fused.away_win_prob), 4)

        # 比分分布来自MC模型（DC不产出比分分布）
        for pred in predictions:
            if pred.model_name == "monte_carlo":
                fused.top_scores = pred.top_scores
                fused.top_total_goals = pred.top_total_goals
                break

        return fused

    def update_weights(self, new_weights: dict[str, float]):
        """动态更新模型权重（由 learning 模块调用）"""
        self.weights = new_weights
