"""Elo 自动更新器 - 每次结算后自动更新球队评级"""
import json
from dataclasses import dataclass
from pathlib import Path

from ..prediction.base import TeamRating


@dataclass
class EloConfig:
    k_factor: float = 32.0
    home_advantage: float = 65.0
    goal_diff_multiplier: float = 8.0
    max_elo: float = 2200.0
    min_elo: float = 1000.0
    default_elo: float = 1500.0
    # 攻防更新
    attack_decay: float = 0.95
    defense_decay: float = 0.95
    attack_learning: float = 0.05
    defense_learning: float = 0.05


class EloUpdater:
    """
    自动 Elo 更新。
    sporttery-prediction 的 team_ratings 是手动维护的，这里实现自动闭环。
    """

    def __init__(self, ratings_path: Path, config: EloConfig | None = None):
        self.ratings_path = ratings_path
        self.cfg = config or EloConfig()
        self.ratings: dict[str, TeamRating] = {}
        self._load()

    def _load(self):
        """加载现有评级"""
        if self.ratings_path.exists():
            data = json.loads(self.ratings_path.read_text())
            for name, r in data.items():
                self.ratings[name] = TeamRating(
                    name=name,
                    elo=r.get("elo", self.cfg.default_elo),
                    attack=r.get("attack", 1.0),
                    defense=r.get("defense", 1.0),
                    form=r.get("form", 0.0),
                    injury=r.get("injury", 0.0),
                    rest_days=r.get("rest_days", 3),
                )

    def save(self):
        """持久化评级"""
        data = {}
        for name, r in self.ratings.items():
            data[name] = {
                "elo": round(r.elo, 1),
                "attack": round(r.attack, 4),
                "defense": round(r.defense, 4),
                "form": round(r.form, 4),
                "injury": r.injury,
                "rest_days": r.rest_days,
            }
        self.ratings_path.parent.mkdir(parents=True, exist_ok=True)
        self.ratings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def get_rating(self, team_name: str) -> TeamRating:
        """获取球队评级，不存在则创建默认"""
        if team_name not in self.ratings:
            self.ratings[team_name] = TeamRating(name=team_name, elo=self.cfg.default_elo)
        return self.ratings[team_name]

    def update(
        self,
        home_team: str,
        away_team: str,
        home_score: int,
        away_score: int,
        is_neutral: bool = False,
    ):
        """根据比赛结果更新 Elo 和攻防"""
        home = self.get_rating(home_team)
        away = self.get_rating(away_team)

        # 期望得分
        home_adv = 0 if is_neutral else self.cfg.home_advantage
        expected_home = 1.0 / (1.0 + 10 ** ((away.elo - home.elo - home_adv) / 400))

        # 实际得分
        if home_score > away_score:
            actual_home = 1.0
        elif home_score == away_score:
            actual_home = 0.5
        else:
            actual_home = 0.0

        # 进球差加权
        goal_diff = abs(home_score - away_score)
        k = self.cfg.k_factor * (1 + self.cfg.goal_diff_multiplier * max(0, goal_diff - 1) / 10)

        # Elo 更新
        home.elo += k * (actual_home - expected_home)
        away.elo -= k * (actual_home - expected_home)

        # 限制范围
        home.elo = max(self.cfg.min_elo, min(self.cfg.max_elo, home.elo))
        away.elo = max(self.cfg.min_elo, min(self.cfg.max_elo, away.elo))

        # 攻防更新（指数移动平均）
        baseline = 1.35  # 联赛平均进球
        home_attack_actual = home_score / max(baseline, 0.1)
        away_attack_actual = away_score / max(baseline, 0.1)
        home_defense_actual = away_score / max(baseline, 0.1)
        away_defense_actual = home_score / max(baseline, 0.1)

        la = self.cfg.attack_learning
        ld = self.cfg.defense_learning
        home.attack = (1 - la) * home.attack + la * home_attack_actual
        away.attack = (1 - la) * away.attack + la * away_attack_actual
        home.defense = (1 - ld) * home.defense + ld * home_defense_actual
        away.defense = (1 - ld) * away.defense + ld * away_defense_actual

        # 状态更新（简化：近 5 场得分率）
        home.form = self._update_form(home.form, actual_home)
        away.form = self._update_form(away.form, 1 - actual_home)

    def _update_form(self, current_form: float, result: float) -> float:
        """指数移动平均更新状态"""
        return 0.7 * current_form + 0.3 * (result - 0.5) * 2
