from __future__ import annotations
"""Elo 自动更新器 - 每次结算后自动更新球队评级"""
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..prediction.base import TeamRating
from ..team_aliases import normalize_team


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
        """加载现有评级（归一化键，合并重复译名条目）"""
        if self.ratings_path.exists():
            data = json.loads(self.ratings_path.read_text())
            for name, r in data.items():
                # 归一化键存储（2026-08-05 修复：同一队多译名条目分裂，
                # 如"塞伊奈"/"塞那乔其"各自独立演进，Elo 差 200+ 分导致方向性错误）
                key = normalize_team(name)
                existing = self.ratings.get(key)
                if existing is None:
                    self.ratings[key] = TeamRating(
                        name=key,
                        elo=r.get("elo", self.cfg.default_elo),
                        attack=r.get("attack", 1.0),
                        defense=r.get("defense", 1.0),
                        form=r.get("form", 0.0),
                        injury=r.get("injury", 0.0),
                        rest_days=r.get("rest_days", 3),
                        last_match_date=r.get("last_match_date"),
                    )
                else:
                    # 重复条目：Elo 更高者胜（弱条目多为预测时创建的空壳或未更新值，
                    # 结算侧数据更可信；真实强队 Elo 会随胜负自然上升）
                    cand_elo = r.get("elo", self.cfg.default_elo)
                    if cand_elo > existing.elo:
                        existing.elo = cand_elo
                        existing.attack = r.get("attack", existing.attack)
                        existing.defense = r.get("defense", existing.defense)
                        existing.form = r.get("form", existing.form)
                        existing.injury = r.get("injury", existing.injury)
                        existing.rest_days = r.get("rest_days", existing.rest_days)
                        existing.last_match_date = r.get(
                            "last_match_date", existing.last_match_date)

    def save(self):
        """持久化评级（归一化键）"""
        data = {}
        for name, r in self.ratings.items():
            data[name] = {
                "elo": round(r.elo, 1),
                "attack": round(r.attack, 4),
                "defense": round(r.defense, 4),
                "form": round(r.form, 4),
                "injury": r.injury,
                "rest_days": r.rest_days,
                "last_match_date": r.last_match_date,
            }
        self.ratings_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写（2026-10-23）：原为裸 write_text，写入中途被杀会留下半截
        # JSON，下次 load 直接崩。tmp + os.replace 保证要么旧要么新。
        tmp = self.ratings_path.with_name(self.ratings_path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.ratings_path)

    def get_rating(self, team_name: str) -> TeamRating:
        """获取球队评级（归一化键），不存在则创建默认"""
        key = normalize_team(team_name)
        if key not in self.ratings:
            self.ratings[key] = TeamRating(name=key, elo=self.cfg.default_elo)
        return self.ratings[key]

    def update(
        self,
        home_team: str,
        away_team: str,
        home_score: int,
        away_score: int,
        is_neutral: bool = False,
        match_date: str | None = None,
    ):
        """根据比赛结果更新 Elo 和攻防（入口归一化，防条目分裂）

        match_date: 比赛日期 YYYY-MM-DD。传入时同步刷新双方 rest_days
        （距上一场比赛天数），否则 rest_days 永远是默认值 3，
        `rest_weight` 是死旋钮（见 DEAD-FEATURES-REST-DAYS）。
        """
        home = self.get_rating(normalize_team(home_team))
        away = self.get_rating(normalize_team(away_team))
        if match_date:
            self._refresh(home, match_date, advance=True)
            self._refresh(away, match_date, advance=True)

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
        da = self.cfg.attack_decay
        dd = self.cfg.defense_decay
        home.attack = da * ((1 - la) * home.attack + la * home_attack_actual)
        away.attack = da * ((1 - la) * away.attack + la * away_attack_actual)
        home.defense = dd * ((1 - ld) * home.defense + ld * home_defense_actual)
        away.defense = dd * ((1 - ld) * away.defense + ld * away_defense_actual)

        # 状态更新（简化：近 5 场得分率）
        home.form = self._update_form(home.form, actual_home)
        away.form = self._update_form(away.form, 1 - actual_home)

    def _update_form(self, current_form: float, result: float) -> float:
        """指数移动平均更新状态"""
        return 0.7 * current_form + 0.3 * (result - 0.5) * 2

    def refresh_rest_for(self, rating: TeamRating, kickoff_date: str) -> int:
        """预测侧调用：按本场开赛日重算 rating.rest_days，不推进 last_match_date。

        与结算侧 `update(match_date=...)` 的区别：
        结算时写下的 rest_days 是"上一场距**结算日**的天数"，而预测
        发生在若干天之后，直接拿来用会把间隔算短。这里在预测前按
        本场 kickoff 重新折算，且**不落盘** —— 落盘的 last_match_date
        只能由真实赛果推进，否则一次预测污染永久状态。

        返回刷新后的 rest_days；rating.last_match_date 为 None 时
        （从未结算过的球队）保持默认 3 并原样返回。
        """
        self._refresh(rating, kickoff_date, advance=False)
        return rating.rest_days

    def _refresh(self, rating: TeamRating, match_date: str,
                 advance: bool) -> bool:
        """刷新 rating.rest_days，按需推进 last_match_date。

        advance 语义（关键，不能靠猜）：
          - True  = match_date 是**刚打完**那场的日期 → 它成为新的
                    last_match_date（结算侧 update() 走这里）；
          - False = match_date 是**即将开赛**那场的日期 → 只折算间隔，
                    不推进（预测侧 refresh_rest_for() 走这里）。
        若预测也推进，一次预测就把"上一场"改写成了还没踢的比赛。

        rest_days 只在 gap>=0 时写：负 gap 说明传入日期早于
        last_match_date（重跑/补录历史赛果），此时保留旧值 ——
        旧值来自更晚的真实赛果，信息更新。同一天（gap==0）写 0，
        语义正确（一天双赛）。

        封顶 14：超过两周的间隔对"疲劳"无意义，且避免一次超长停摆
        （疫情/休赛期）把 rest_days 推到 60+ 污染后续所有比赛。
        """
        try:
            cur = date.fromisoformat(match_date)
        except (ValueError, TypeError):
            return False
        last = rating.last_match_date
        if last:
            try:
                gap = (cur - date.fromisoformat(last)).days
            except (ValueError, TypeError):
                gap = None
            if gap is not None and gap >= 0:
                # 只在 gap>=0 时写：负 gap 意味着传入日期早于 last_match_date
                # （重跑/补录历史赛果）。此时保留旧值不覆盖 —— 旧值来自
                # 更晚的真实赛果，信息更新。同一天（gap==0）写 0，语义正确。
                rating.rest_days = min(gap, 14)
        if advance and (last is None or cur > date.fromisoformat(last)):
            rating.last_match_date = match_date
            return True
        return False
