"""Kelly 准则 + 风控"""
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BetCandidate:
    """投注候选"""
    match_id: str
    selection: str  # "home" / "draw" / "away"
    model_prob: float
    market_prob: float
    odds: float
    edge: float  # model_prob - market_prob
    ev: float  # expected value
    kelly_fraction: float = 0.0
    stake: float = 0.0
    is_parlay: bool = False
    risk_notes: list[str] = field(default_factory=list)


@dataclass
class BettingPlan:
    """投注计划"""
    date: str
    singles: list[BetCandidate] = field(default_factory=list)
    parlays: list[list[BetCandidate]] = field(default_factory=list)
    total_stake: float = 0.0
    monthly_used: float = 0.0
    rejected: list[tuple[BetCandidate, str]] = field(default_factory=list)


class KellyStrategy:
    """
    Quarter-Kelly 准则 + 硬风控。
    借鉴 sporttery-prediction 的风控体系。
    """

    def __init__(self, config_path: Path | None = None):
        if config_path and config_path.exists():
            cfg = json.loads(config_path.read_text())
        else:
            cfg = {}

        strategy = cfg.get("strategy", cfg)
        self.kelly_fraction = strategy.get("kelly_fraction", 0.25)
        self.bankroll = strategy.get("reference_bankroll", 5000)
        self.stake_unit = strategy.get("stake_unit", 2)

        limits = strategy.get("limits", {})
        self.max_single = limits.get("max_single_stake", 200)
        self.max_match_exposure = limits.get("max_match_exposure", 200)
        self.max_daily = limits.get("max_daily_stake", 500)
        self.max_monthly = limits.get("max_monthly_budget", 5000)
        self.monthly_stop_loss = limits.get("monthly_stop_loss", 5000)
        self.max_parlay_stake = limits.get("max_parlay_stake", 30)
        self.max_parlay_legs = limits.get("max_parlay_legs", 2)

        gates = strategy.get("edge_gates", {})
        self.min_edge = gates.get("min_probability_edge", 0.03)
        self.min_ev = gates.get("min_ev", 0.03)

    def evaluate_candidates(
        self,
        predictions: list[dict],
        monthly_pnl: float = 0.0,
        daily_stake_so_far: float = 0.0,
    ) -> BettingPlan:
        """评估所有候选，生成投注计划"""
        plan = BettingPlan(date=predictions[0].get("date", "") if predictions else "")

        # 月度止损检查
        if monthly_pnl <= -self.monthly_stop_loss:
            return plan  # 空计划

        candidates = []
        for pred in predictions:
            for sel in ["home", "draw", "away"]:
                prob_key = f"{sel}_prob" if sel != "home" else "home_win_prob"
                if sel == "home":
                    prob_key = "home_win_prob"
                elif sel == "draw":
                    prob_key = "draw_prob"
                else:
                    prob_key = "away_win_prob"

                model_prob = pred.get(prob_key, 0)
                odds_key = f"{sel}_odds"
                odds = pred.get(odds_key)
                if not odds or odds <= 1.0:
                    continue

                market_prob = 1.0 / odds
                edge = model_prob - market_prob
                ev = model_prob * odds - 1.0

                if edge < self.min_edge or ev < self.min_ev:
                    continue

                # Kelly 公式
                b = odds - 1.0
                full_kelly = max(0, (b * model_prob - (1 - model_prob)) / b)
                stake = self.bankroll * full_kelly * self.kelly_fraction

                # 取整到投注单位
                stake = int(stake / self.stake_unit) * self.stake_unit
                if stake < self.stake_unit:
                    continue

                candidate = BetCandidate(
                    match_id=pred.get("match_id", ""),
                    selection=sel,
                    model_prob=model_prob,
                    market_prob=market_prob,
                    odds=odds,
                    edge=edge,
                    ev=ev,
                    kelly_fraction=full_kelly,
                    stake=stake,
                )
                candidates.append(candidate)

        # 按 EV 排序
        candidates.sort(key=lambda c: c.ev, reverse=True)

        # 应用风控
        match_exposure: dict[str, float] = {}
        daily_remaining = self.max_daily - daily_stake_so_far

        for c in candidates:
            # 单注上限
            c.stake = min(c.stake, self.max_single)
            # 单场暴露上限
            exposure = match_exposure.get(c.match_id, 0)
            c.stake = min(c.stake, self.max_match_exposure - exposure)
            # 日限额
            c.stake = min(c.stake, daily_remaining)
            # 月限额
            c.stake = min(c.stake, self.max_monthly - plan.monthly_used)

            if c.stake < self.stake_unit:
                plan.rejected.append((c, "stake_below_minimum"))
                continue

            plan.singles.append(c)
            plan.total_stake += c.stake
            plan.monthly_used += c.stake
            match_exposure[c.match_id] = match_exposure.get(c.match_id, 0) + c.stake
            daily_remaining -= c.stake

        return plan

    def build_parlays(self, singles: list[BetCandidate]) -> list[list[BetCandidate]]:
        """从单注中构建串关（2 串 1）"""
        parlays = []
        used_matches = set()

        # 按 edge 排序，取不同比赛的 top 候选
        eligible = [s for s in singles if s.match_id not in used_matches]
        eligible.sort(key=lambda c: c.edge, reverse=True)

        for i in range(len(eligible)):
            for j in range(i + 1, len(eligible)):
                a, b = eligible[i], eligible[j]
                if a.match_id == b.match_id:
                    continue
                if a.match_id in used_matches or b.match_id in used_matches:
                    continue

                combined_odds = a.odds * b.odds
                combined_prob = a.model_prob * b.model_prob
                ev = combined_prob * combined_odds - 1.0

                if ev > self.min_ev:
                    stake = min(self.max_parlay_stake, self.stake_unit * 5)
                    a_copy = BetCandidate(
                        match_id=a.match_id, selection=a.selection,
                        model_prob=a.model_prob, market_prob=a.market_prob,
                        odds=a.odds, edge=a.edge, ev=a.ev,
                        stake=stake, is_parlay=True,
                    )
                    b_copy = BetCandidate(
                        match_id=b.match_id, selection=b.selection,
                        model_prob=b.model_prob, market_prob=b.market_prob,
                        odds=b.odds, edge=b.edge, ev=b.ev,
                        stake=stake, is_parlay=True,
                    )
                    parlays.append([a_copy, b_copy])
                    used_matches.add(a.match_id)
                    used_matches.add(b.match_id)
                    break

        return parlays
