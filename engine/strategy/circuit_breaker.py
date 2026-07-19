"""
熔断机制 — 连续亏损自动降注/停注。

来源: jincai-model 纪律模块
规则:
  - 连续亏损 >= 3  → 降注至 50%
  - 连续亏损 >= 6  → 降注至 25%
  - 连续亏损 >= 12 → 降注至 10%（最小注）
  - 连续亏损 >= 15 → 完全停注，等待人工/自动重置
  - 单日亏损超过 bankroll * max_daily_loss → 当日停注
  - 周亏损超过 bankroll * max_weekly_loss → 本周停注
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path


@dataclass
class CircuitBreakerConfig:
    """熔断参数"""
    # 连败阶梯（注额）
    tier1_streak: int = 3       # 第一档连败数
    tier1_ratio: float = 0.50   # 第一档注额比例
    tier2_streak: int = 6
    tier2_ratio: float = 0.25
    tier3_streak: int = 12
    tier3_ratio: float = 0.10
    halt_streak: int = 15       # 完全停注

    # 连败阶梯（置信阈值收紧）
    tier1_confidence: float = 0.70   # 3连败 → 只出 confidence>0.70 的票
    tier2_confidence: float = 0.85   # 6连败 → 只出 confidence>0.85
    tier3_confidence: float = 0.95   # 12连败 → 只出 confidence>0.95

    # 日/周止损
    max_daily_loss_ratio: float = 0.05    # 单日最大亏损占本金比
    max_weekly_loss_ratio: float = 0.12   # 单周最大亏损占本金比

    # 恢复
    recovery_streak: int = 2    # 连赢N场后恢复上一档
    auto_reset_days: int = 7    # 停注N天后自动重置


@dataclass
class BreakerState:
    """熔断状态（持久化）"""
    current_streak: int = 0          # 当前连败/连赢（负=连败）
    total_losses: int = 0
    total_wins: int = 0
    halted: bool = False
    halt_date: str = ""
    tier: int = 0                    # 当前档位 0=正常
    daily_pnl: float = 0.0
    daily_date: str = ""
    weekly_pnl: float = 0.0
    weekly_start: str = ""
    history: list[dict] = field(default_factory=list)


class CircuitBreaker:
    """
    熔断器。

    用法:
        cb = CircuitBreaker(state_path)
        multiplier = cb.get_multiplier(bankroll)
        # multiplier=1.0 正常, 0.5 降半, 0.0 停注
        cb.record_result(won=True, pnl=100, bankroll=10000)
    """

    def __init__(
        self,
        state_path: str | Path = "data/state/circuit_breaker.json",
        config: CircuitBreakerConfig | None = None,
    ):
        self.cfg = config or CircuitBreakerConfig()
        self.state_path = Path(state_path)
        self.state = self._load()

    def _load(self) -> BreakerState:
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return BreakerState(**raw)
        return BreakerState()

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_multiplier(self, bankroll: float) -> float:
        """返回当前注额乘数 [0.0, 1.0]"""
        s = self.state
        today = date.today().isoformat()

        # 检查自动重置
        if s.halted and s.halt_date:
            days_halted = (date.today() - date.fromisoformat(s.halt_date)).days
            if days_halted >= self.cfg.auto_reset_days:
                self._reset()
                return 1.0

        if s.halted:
            return 0.0

        # 日止损
        if s.daily_date == today and s.daily_pnl < 0:
            if abs(s.daily_pnl) >= bankroll * self.cfg.max_daily_loss_ratio:
                return 0.0

        # 周止损
        if s.weekly_pnl < 0:
            if abs(s.weekly_pnl) >= bankroll * self.cfg.max_weekly_loss_ratio:
                return 0.0

        # 连败阶梯 + 渐进恢复
        # 取 streak 隐含档位 和 记录档位 的较大值（恢复需要连赢才能降档）
        streak = abs(min(s.current_streak, 0))
        streak_tier = 0
        if streak >= self.cfg.halt_streak:
            return 0.0
        elif streak >= self.cfg.tier3_streak:
            streak_tier = 3
        elif streak >= self.cfg.tier2_streak:
            streak_tier = 2
        elif streak >= self.cfg.tier1_streak:
            streak_tier = 1

        effective_tier = max(streak_tier, s.tier)
        if effective_tier >= 3:
            return self.cfg.tier3_ratio
        if effective_tier == 2:
            return self.cfg.tier2_ratio
        if effective_tier == 1:
            return self.cfg.tier1_ratio
        return 1.0

    def get_tier(self) -> int:
        """当前档位 0=正常 1/2/3=降注 4=停注"""
        streak = abs(min(self.state.current_streak, 0))
        if self.state.halted or streak >= self.cfg.halt_streak:
            return 4
        if streak >= self.cfg.tier3_streak:
            return 3
        if streak >= self.cfg.tier2_streak:
            return 2
        if streak >= self.cfg.tier1_streak:
            return 1
        return 0

    def get_confidence_threshold(self) -> float:
        """返回当前置信阈值（连败越紧，阈值越高）

        自适应机制: 连败时自动收紧出票标准
          tier 0 (正常):  0.0  → 不过滤
          tier 1 (3连败): 0.70 → 只要高置信
          tier 2 (6连败): 0.85 → 只要很高置信
          tier 3 (12连败): 0.95 → 近乎确定才出
          tier 4 (停注):  1.0  → 全部拦截
        """
        tier = self.get_tier()
        if tier == 0:
            return 0.0
        if tier == 1:
            return self.cfg.tier1_confidence
        if tier == 2:
            return self.cfg.tier2_confidence
        if tier == 3:
            return self.cfg.tier3_confidence
        return 1.0  # tier 4: 停注

    def record_result(
        self, won: bool, pnl: float, bankroll: float
    ) -> None:
        """记录一场比赛结果并更新状态"""
        s = self.state
        today = date.today().isoformat()

        # 日PnL
        if s.daily_date != today:
            s.daily_date = today
            s.daily_pnl = 0.0
        s.daily_pnl += pnl

        # 周PnL
        week_start = self._week_start()
        if s.weekly_start != week_start:
            s.weekly_start = week_start
            s.weekly_pnl = 0.0
        s.weekly_pnl += pnl

        # 连败/连赢
        if won:
            s.total_wins += 1
            if s.current_streak < 0:
                # 从连败中恢复
                s.current_streak = 1
            else:
                s.current_streak += 1
            # 连赢恢复机制
            if s.current_streak >= self.cfg.recovery_streak and s.tier > 0:
                s.tier = max(0, s.tier - 1)
        else:
            s.total_losses += 1
            if s.current_streak > 0:
                s.current_streak = -1
            else:
                s.current_streak -= 1

        # 检查是否触发停注
        streak = abs(min(s.current_streak, 0))
        if streak >= self.cfg.halt_streak and not s.halted:
            s.halted = True
            s.halt_date = today

        # 连败时升档（只升不降，降档靠连赢恢复）
        if streak >= self.cfg.tier3_streak:
            s.tier = max(s.tier, 3)
        elif streak >= self.cfg.tier2_streak:
            s.tier = max(s.tier, 2)
        elif streak >= self.cfg.tier1_streak:
            s.tier = max(s.tier, 1)

        # 记录历史
        s.history.append({
            "date": today,
            "won": won,
            "pnl": round(pnl, 2),
            "streak": s.current_streak,
            "bankroll": round(bankroll, 2),
        })
        # 只保留最近200条
        if len(s.history) > 200:
            s.history = s.history[-200:]

        self.save()

    def _reset(self) -> None:
        """重置熔断状态（保留历史）"""
        s = self.state
        s.current_streak = 0
        s.halted = False
        s.halt_date = ""
        s.tier = 0
        s.daily_pnl = 0.0
        s.weekly_pnl = 0.0
        self.save()

    def force_reset(self) -> None:
        """外部强制重置"""
        self._reset()

    @staticmethod
    def _week_start() -> str:
        """本周一日期"""
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        return monday.isoformat()

    def status_report(self) -> dict:
        """状态摘要"""
        return {
            "current_streak": self.state.current_streak,
            "tier": self.get_tier(),
            "halted": self.state.halted,
            "daily_pnl": round(self.state.daily_pnl, 2),
            "weekly_pnl": round(self.state.weekly_pnl, 2),
            "total_wins": self.state.total_wins,
            "total_losses": self.state.total_losses,
            "win_rate": (
                self.state.total_wins / max(1, self.state.total_wins + self.state.total_losses)
            ),
        }
