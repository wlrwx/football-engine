"""
三票制资金管理 — 60/30/10 风险阶梯。

来源: football-analyzer 三票制
理念:
  将每轮投注分为三档:
    - 稳胆票 (60%): 高置信度场次，低赔率，追求命中
    - 搏冷票 (30%): 中等置信度，中高赔率，追求超额收益
    - 彩票票 (10%): 高赔率长串，小注博大奖

每档独立计算 Kelly 注额，再乘以档位比例。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ThreeTicketConfig:
    """三票制参数"""
    # 资金分配比例
    stable_ratio: float = 0.60    # 稳胆票
    value_ratio: float = 0.30     # 搏冷票
    lottery_ratio: float = 0.10   # 彩票票

    # 各档赔率范围
    stable_odds_range: tuple[float, float] = (1.20, 1.80)
    value_odds_range: tuple[float, float] = (1.80, 3.50)
    lottery_odds_range: tuple[float, float] = (3.50, 20.0)

    # 各档最低概率阈值
    stable_min_prob: float = 0.60
    value_min_prob: float = 0.40
    lottery_min_prob: float = 0.20

    # 各档最大注数
    stable_max_picks: int = 4
    value_max_picks: int = 3
    lottery_max_picks: int = 2

    # 单票最大占总资金比
    max_single_ratio: float = 0.08


@dataclass
class TicketPick:
    """单条选项"""
    match_id: str
    selection: str          # "home" / "draw" / "away"
    odds: float
    prob: float             # 模型估计概率
    kelly_fraction: float   # Kelly建议仓位
    ticket_type: str = ""   # "stable" / "value" / "lottery"
    stake: float = 0.0      # 实际注额


@dataclass
class TicketPlan:
    """一轮三票方案"""
    stable_picks: list[TicketPick]
    value_picks: list[TicketPick]
    lottery_picks: list[TicketPick]
    total_stake: float = 0.0
    expected_roi: float = 0.0


class ThreeTicketAllocator:
    """
    三票制资金分配器。

    用法:
        alloc = ThreeTicketAllocator(bankroll=10000)
        plan = alloc.allocate(candidates, kelly_fractions)
    """

    def __init__(
        self,
        bankroll: float,
        config: ThreeTicketConfig | None = None,
        breaker_multiplier: float = 1.0,
    ):
        self.bankroll = bankroll
        self.cfg = config or ThreeTicketConfig()
        self.breaker_multiplier = breaker_multiplier

    def allocate(
        self,
        candidates: list[dict],
    ) -> TicketPlan:
        """
        将候选场次分配到三档。

        candidates: [{match_id, selection, odds, prob, kelly_fraction}]
        """
        stable, value, lottery = [], [], []

        for c in candidates:
            odds = c["odds"]
            prob = c["prob"]
            pick = TicketPick(
                match_id=c["match_id"],
                selection=c["selection"],
                odds=odds,
                prob=prob,
                kelly_fraction=c.get("kelly_fraction", 0.0),
            )

            if self.cfg.stable_odds_range[0] <= odds <= self.cfg.stable_odds_range[1]:
                if prob >= self.cfg.stable_min_prob:
                    pick.ticket_type = "stable"
                    stable.append(pick)
            elif self.cfg.value_odds_range[0] <= odds <= self.cfg.value_odds_range[1]:
                if prob >= self.cfg.value_min_prob:
                    pick.ticket_type = "value"
                    value.append(pick)
            elif self.cfg.lottery_odds_range[0] <= odds <= self.cfg.lottery_odds_range[1]:
                if prob >= self.cfg.lottery_min_prob:
                    pick.ticket_type = "lottery"
                    lottery.append(pick)

        # 按 edge = prob*odds - 1 排序，取前N
        stable.sort(key=lambda p: p.prob * p.odds - 1, reverse=True)
        value.sort(key=lambda p: p.prob * p.odds - 1, reverse=True)
        lottery.sort(key=lambda p: p.prob * p.odds - 1, reverse=True)

        stable = stable[: self.cfg.stable_max_picks]
        value = value[: self.cfg.value_max_picks]
        lottery = lottery[: self.cfg.lottery_max_picks]

        # 计算注额
        effective_bankroll = self.bankroll * self.breaker_multiplier
        stable_pool = effective_bankroll * self.cfg.stable_ratio
        value_pool = effective_bankroll * self.cfg.value_ratio
        lottery_pool = effective_bankroll * self.cfg.lottery_ratio

        self._assign_stakes(stable, stable_pool)
        self._assign_stakes(value, value_pool)
        self._assign_stakes(lottery, lottery_pool)

        total = sum(p.stake for p in stable + value + lottery)
        exp_roi = (
            sum(p.stake * (p.prob * p.odds - 1) for p in stable + value + lottery)
            / max(total, 1)
        )

        return TicketPlan(
            stable_picks=stable,
            value_picks=value,
            lottery_picks=lottery,
            total_stake=round(total, 2),
            expected_roi=round(exp_roi, 4),
        )

    def _assign_stakes(self, picks: list[TicketPick], pool: float) -> None:
        """按Kelly比例分配池内资金"""
        if not picks:
            return
        total_kelly = sum(p.kelly_fraction for p in picks if p.kelly_fraction > 0)
        if total_kelly <= 0:
            return
        max_single = self.bankroll * self.cfg.max_single_ratio

        for p in picks:
            if p.kelly_fraction <= 0:
                continue
            weight = p.kelly_fraction / total_kelly
            raw_stake = pool * weight
            p.stake = round(min(raw_stake, max_single), 2)

    def summary(self, plan: TicketPlan) -> dict:
        """方案摘要"""
        return {
            "stable": [
                {"match": p.match_id, "sel": p.selection, "odds": p.odds, "stake": p.stake}
                for p in plan.stable_picks
            ],
            "value": [
                {"match": p.match_id, "sel": p.selection, "odds": p.odds, "stake": p.stake}
                for p in plan.value_picks
            ],
            "lottery": [
                {"match": p.match_id, "sel": p.selection, "odds": p.odds, "stake": p.stake}
                for p in plan.lottery_picks
            ],
            "total_stake": plan.total_stake,
            "expected_roi": plan.expected_roi,
            "bankroll": self.bankroll,
            "breaker_multiplier": self.breaker_multiplier,
        }
