"""
CPPI + Taleb杠铃策略 — 安全垫 + 棘轮保护。

来源: wc26-board / 组合投资理论
CPPI (Constant Proportion Portfolio Insurance):
  - 将资金分为"安全资产"(保底)和"风险资产"(投注)
  - 风险敞口 = multiplier × (当前资产 - 保底底线)
  - 保底底线随盈利上移（棘轮），不随亏损下移

Taleb杠铃:
  - 90%资金极度保守（不投/只投稳胆）
  - 10%资金极度激进（高赔长串）
  - 避免"中间地带"的隐性风险
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CPPIConfig:
    """CPPI参数"""
    floor_ratio: float = 0.70       # 保底底线 = 峰值资产 × floor_ratio
    multiplier: float = 3.0         # 风险乘数（敞口 = m × cushion）
    max_risk_ratio: float = 0.30    # 单轮最大风险敞口占总资产比
    ratchet: bool = True            # 棘轮：底线只升不降
    # Taleb杠铃
    barbell_safe_ratio: float = 0.90   # 保守部分占比
    barbell_risk_ratio: float = 0.10   # 激进部分占比


@dataclass
class CPPIState:
    """CPPI状态（持久化）"""
    peak_bankroll: float = 0.0      # 历史峰值
    floor: float = 0.0              # 当前保底底线
    current_bankroll: float = 0.0
    total_risk_exposure: float = 0.0
    history: list[dict] | None = None

    def __post_init__(self):
        if self.history is None:
            self.history = []


class CPPIStrategy:
    """
    CPPI + 杠铃资金管理。

    用法:
        cppi = CPPIStrategy(state_path, initial_bankroll=10000)
        risk_budget = cppi.get_risk_budget()
        # 分配: safe_pool 投稳胆, aggressive_pool 投高赔
        cppi.update(new_bankroll=10200)
    """

    def __init__(
        self,
        state_path: str | Path = "data/state/cppi.json",
        initial_bankroll: float = 10000.0,
        config: CPPIConfig | None = None,
    ):
        self.cfg = config or CPPIConfig()
        self.state_path = Path(state_path)
        self.state = self._load(initial_bankroll)

    def _load(self, initial: float) -> CPPIState:
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return CPPIState(**raw)
        return CPPIState(
            peak_bankroll=initial,
            floor=initial * self.cfg.floor_ratio,
            current_bankroll=initial,
        )

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_risk_budget(self) -> dict:
        """
        计算当前可承受的风险预算。

        返回:
            {
                "cushion": 安全垫金额,
                "risk_exposure": 最大风险敞口,
                "safe_pool": 保守资金池（杠铃保守端）,
                "aggressive_pool": 激进资金池（杠铃激进端）,
                "floor": 保底底线,
                "peak": 历史峰值,
            }
        """
        s = self.state
        cushion = max(s.current_bankroll - s.floor, 0)
        raw_exposure = self.cfg.multiplier * cushion
        max_exposure = s.current_bankroll * self.cfg.max_risk_ratio
        risk_exposure = min(raw_exposure, max_exposure)

        # 杠铃分配
        safe_pool = risk_exposure * self.cfg.barbell_safe_ratio
        aggressive_pool = risk_exposure * self.cfg.barbell_risk_ratio

        return {
            "cushion": round(cushion, 2),
            "risk_exposure": round(risk_exposure, 2),
            "safe_pool": round(safe_pool, 2),
            "aggressive_pool": round(aggressive_pool, 2),
            "floor": round(s.floor, 2),
            "peak": round(s.peak_bankroll, 2),
            "current": round(s.current_bankroll, 2),
            "cushion_ratio": round(cushion / max(s.current_bankroll, 1), 4),
        }

    def update(self, new_bankroll: float) -> None:
        """
        更新资产并调整底线（棘轮）。
        每轮结算后调用。
        """
        s = self.state
        s.current_bankroll = new_bankroll

        # 更新峰值
        if new_bankroll > s.peak_bankroll:
            s.peak_bankroll = new_bankroll

        # 棘轮：底线只升不降
        new_floor = s.peak_bankroll * self.cfg.floor_ratio
        if self.cfg.ratchet:
            s.floor = max(s.floor, new_floor)
        else:
            s.floor = new_floor

        # 记录历史
        s.history.append({
            "bankroll": round(new_bankroll, 2),
            "peak": round(s.peak_bankroll, 2),
            "floor": round(s.floor, 2),
        })
        if len(s.history) > 365:
            s.history = s.history[-365:]

        self.save()

    def is_protected(self) -> bool:
        """是否触发保护（资产接近底线）"""
        s = self.state
        cushion = s.current_bankroll - s.floor
        return cushion <= s.current_bankroll * 0.02  # 安全垫 < 2%

    def status_report(self) -> dict:
        """状态摘要"""
        budget = self.get_risk_budget()
        return {
            **budget,
            "protected": self.is_protected(),
            "drawdown_from_peak": round(
                1 - self.state.current_bankroll / max(self.state.peak_bankroll, 1), 4
            ),
        }
