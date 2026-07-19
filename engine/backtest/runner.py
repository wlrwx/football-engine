"""回测运行器 - 严格时间隔离，无未来泄漏"""
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..prediction.base import MatchPrediction, TeamRating
from ..prediction.ensemble import EnsembleModel
from ..strategy.kelly import KellyStrategy


@dataclass
class BacktestResult:
    """回测结果"""
    total_matches: int = 0
    recommended: int = 0
    hits: int = 0
    hit_rate: float = 0.0
    avg_odds: float = 0.0
    roi: float = 0.0
    brier_score: float = 0.0
    log_loss: float = 0.0
    max_drawdown: float = 0.0
    pnl_history: list = field(default_factory=list)
    details: list = field(default_factory=list)


class BacktestRunner:
    """
    回测框架。
    对每场已完赛的比赛，只用该比赛之前的数据做预测，严格防止未来泄漏。
    借鉴 lottery-football 的时间隔离设计。
    """

    def __init__(
        self,
        historical_path: Path,
        ratings_path: Path,
        config_dir: Path,
    ):
        self.historical_path = historical_path
        self.ratings_path = ratings_path
        self.config_dir = config_dir
        self.model = EnsembleModel()
        self.strategy = KellyStrategy(config_dir / "strategy.json")

    def run(
        self,
        start_date: str = "",
        end_date: str = "",
        competitions: list[str] | None = None,
        params_override: dict | None = None,
    ) -> BacktestResult:
        """执行回测"""
        matches = self._load_matches(start_date, end_date, competitions)
        result = BacktestResult(total_matches=len(matches))

        cumulative_pnl = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0
        brier_sum = 0.0
        logloss_sum = 0.0
        odds_sum = 0.0

        for match in matches:
            # 构建评级（只用历史数据）
            home_rating = TeamRating(
                name=match["home_team"],
                elo=match.get("home_elo", 1500),
                attack=match.get("home_attack", 1.0),
                defense=match.get("home_defense", 1.0),
            )
            away_rating = TeamRating(
                name=match["away_team"],
                elo=match.get("away_elo", 1500),
                attack=match.get("away_attack", 1.0),
                defense=match.get("away_defense", 1.0),
            )

            # 市场赔率
            market_odds = None
            if match.get("home_odds") and match.get("draw_odds") and match.get("away_odds"):
                market_odds = (
                    float(match["home_odds"]),
                    float(match["draw_odds"]),
                    float(match["away_odds"]),
                )

            # 预测
            pred = self.model.predict(
                home=home_rating,
                away=away_rating,
                market_odds=market_odds,
                handicap=match.get("handicap"),
            )

            # Brier score
            actual = self._outcome_vector(match["home_score"], match["away_score"])
            predicted = [pred.home_win_prob, pred.draw_prob, pred.away_win_prob]
            brier = sum((p - a) ** 2 for p, a in zip(predicted, actual))
            brier_sum += brier

            # Log loss
            actual_idx = actual.index(1.0)
            prob_actual = max(predicted[actual_idx], 1e-15)
            logloss_sum += -1.0 * (prob_actual and 1) * __import__("math").log(prob_actual)

            # 推荐判断
            best_prob = max(predicted)
            best_idx = predicted.index(best_prob)
            selections = ["home", "draw", "away"]
            best_sel = selections[best_idx]

            if market_odds and best_prob > 0.45:
                odds = market_odds[best_idx]
                if odds >= 1.03:
                    result.recommended += 1
                    odds_sum += odds

                    # 是否命中
                    hit = actual[best_idx] == 1.0
                    if hit:
                        result.hits += 1
                        cumulative_pnl += (odds - 1)
                    else:
                        cumulative_pnl -= 1

                    result.pnl_history.append(round(cumulative_pnl, 2))

                    # 最大回撤
                    peak_pnl = max(peak_pnl, cumulative_pnl)
                    drawdown = peak_pnl - cumulative_pnl
                    max_drawdown = max(max_drawdown, drawdown)

        # 汇总
        if result.total_matches > 0:
            result.brier_score = brier_sum / result.total_matches
            result.log_loss = logloss_sum / result.total_matches

        if result.recommended > 0:
            result.hit_rate = result.hits / result.recommended
            result.avg_odds = odds_sum / result.recommended
            result.roi = cumulative_pnl / result.recommended

        result.max_drawdown = max_drawdown
        return result

    def _load_matches(
        self, start_date: str, end_date: str, competitions: list[str] | None
    ) -> list[dict]:
        """加载历史比赛数据"""
        if not self.historical_path.exists():
            return []

        matches = []
        with open(self.historical_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                match_date = row.get("date", "")
                if start_date and match_date < start_date:
                    continue
                if end_date and match_date > end_date:
                    continue
                if competitions and row.get("competition", "") not in competitions:
                    continue
                # 只取有结果的
                if not row.get("home_score") or not row.get("away_score"):
                    continue
                matches.append(row)

        return matches

    @staticmethod
    def _outcome_vector(home_score: int, away_score: int) -> list[float]:
        """比赛结果转为 [主胜, 平, 客胜] 向量"""
        hs, as_ = int(home_score), int(away_score)
        if hs > as_:
            return [1.0, 0.0, 0.0]
        elif hs == as_:
            return [0.0, 1.0, 0.0]
        else:
            return [0.0, 0.0, 1.0]
