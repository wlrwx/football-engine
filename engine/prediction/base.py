"""预测模型抽象基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TeamRating:
    """球队评级"""
    name: str
    elo: float = 1500.0
    attack: float = 1.0
    defense: float = 1.0
    form: float = 0.0
    injury: float = 0.0
    rest_days: int = 3


@dataclass
class MatchPrediction:
    """单场比赛预测结果"""
    match_id: str
    home_team: str
    away_team: str
    competition: str
    # 胜平负概率
    home_win_prob: float = 0.0
    draw_prob: float = 0.0
    away_win_prob: float = 0.0
    # 期望进球
    home_xg: float = 0.0
    away_xg: float = 0.0
    # 让球概率（可选）
    handicap_home_prob: float = 0.0
    handicap_draw_prob: float = 0.0
    handicap_away_prob: float = 0.0
    # 元数据
    model_name: str = ""
    confidence: float = 0.0


class PredictionModel(ABC):
    """预测模型接口"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def predict(
        self,
        home: TeamRating,
        away: TeamRating,
        market_odds: tuple[float, float, float] | None = None,
        handicap: float | None = None,
        is_neutral: bool = False,
        is_knockout: bool = False,
    ) -> MatchPrediction:
        """预测单场比赛"""
        ...

    def predict_batch(
        self, matches: list[dict]
    ) -> list[MatchPrediction]:
        """批量预测（默认逐个调用）"""
        results = []
        for m in matches:
            pred = self.predict(
                home=m["home"],
                away=m["away"],
                market_odds=m.get("market_odds"),
                handicap=m.get("handicap"),
                is_neutral=m.get("is_neutral", False),
                is_knockout=m.get("is_knockout", False),
            )
            pred.match_id = m.get("match_id", "")
            pred.home_team = m["home"].name
            pred.away_team = m["away"].name
            pred.competition = m.get("competition", "")
            results.append(pred)
        return results
