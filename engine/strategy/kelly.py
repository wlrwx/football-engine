from __future__ import annotations
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

        # 价值区/送钱区判定门槛（2026-08-14：与 ev_report/league_report 同口径，
        # 小样本 ROI 不驱动禁投；可通过 config/strategy.json 调）
        vz = strategy.get("value_zone", {})
        self.vz_min_n = vz.get("min_n", 50)
        # 深冷层加权（2026-08-14 P1 基线：L5 深冷层是唯一稳定赚钱层——
        # 模型源 n=45 +19%、融合源 n=18 +44%。对"样本≥min_n 且 ROI>min_roi"
        # 的层给小额注额加成，仍受 max_single 200 封顶）
        lb = strategy.get("layer_boost", {})
        self.boost_min_n = lb.get("min_n", 15)
        self.boost_min_roi = lb.get("min_roi", 0.05)
        self.boost_mult = lb.get("multiplier", 0.15)

    def _load_value_zones(self, path: Path | None = None) -> dict:
        """加载 EV 价值区报告，返回 {赔率区间: {"roi", "n"}}（无报告返回空）。

        2026-08-14 两档门槛分工（与 ev_report/league_report 同口径）：
        - 返回所有 n≥boost_min_n 的层（供注额加成判断）；
        - 但"送钱区禁投/降权"只在调用侧按 n≥vz_min_n 生效——
          小样本层的负 ROI 是噪声，不驱动禁投（此前 L2-L4 全层 n=40-44
          被标送钱区 → 单关长期 0 注，属于小样本规则误伤）。
        """
        try:
            p = path or (Path(__file__).parent.parent.parent / "data" / "state" / "ev_report.json")
            if not p.exists():
                return {}
            d = json.loads(p.read_text(encoding="utf-8"))
            return {k: {"roi": v.get("roi", 0), "n": v.get("n", 0)}
                    for k, v in d.get("layers", {}).items()
                    if v.get("n", 0) >= self.boost_min_n}
        except Exception:
            return {}

    @staticmethod
    def _layer_of(odds: float) -> str:
        if odds < 1.5:
            return "L1 大热(<1.5)"
        if odds < 1.8:
            return "L2 热(1.5-1.8)"
        if odds < 2.2:
            return "L3 中(1.8-2.2)"
        if odds < 3.0:
            return "L4 冷(2.2-3.0)"
        return "L5 深冷(≥3.0)"

    def evaluate_candidates(
        self,
        predictions: list[dict],
        monthly_pnl: float = 0.0,
        daily_stake_so_far: float = 0.0,
    ) -> BettingPlan:
        """评估所有候选，生成投注计划"""
        plan = BettingPlan(date=predictions[0].get("date", "") if predictions else "")
        # 价值区过滤：历史 ROI 为负的赔率区间降权（老系统实证：L1/L2 大热是送钱区）
        value_zones = self._load_value_zones()
        if not value_zones:
            print("  ⚠ EV价值区报告缺失，跳过价值区过滤（用纯EV）")

        # 月度止损检查
        if monthly_pnl <= -self.monthly_stop_loss:
            return plan  # 空计划

        candidates = []
        rejected_value = 0
        for pred in predictions:
            # 只押模型预测方向（8/3 教训：预测 home 却押 away+draw，8549 元全输）
            direction = pred.get("direction")
            if not direction:
                _probs = (pred.get("home_win_prob", 0), pred.get("draw_prob", 0), pred.get("away_win_prob", 0))
                direction = ["home", "draw", "away"][_probs.index(max(_probs))]
            for sel in ["home", "draw", "away"]:
                if sel != direction:
                    continue  # 禁止押反方向，保证预测与投注一致
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

                # 价值区过滤：该赔率区间历史 ROI<-10% 直接拒绝（送钱区）
                # 2026-08-14：禁投只在 n≥vz_min_n 的层生效（_load_value_zones
                # 返回 n≥boost_min_n 的层，这里再按 vz_min_n 收口）——
                # 小样本层的负 ROI 是噪声，不驱动禁投。
                if value_zones:
                    _layer = self._layer_of(odds)
                    _zone = value_zones.get(_layer)
                    if _zone is not None and _zone["n"] >= self.vz_min_n and _zone["roi"] < -0.10:
                        rejected_value += 1
                        plan.rejected.append((
                            BetCandidate(
                                match_id=pred.get("match_id", ""),
                                selection=sel, model_prob=0, market_prob=0,
                                odds=odds, edge=0, ev=0,
                                risk_notes=[f"送钱区({_layer} ROI {_zone['roi']*100:.0f}%) 拒绝"],
                            ),
                            f"送钱区({_layer} ROI {_zone['roi']*100:.0f}%)",
                        ))
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

                # 历史 ROI 为负的赔率区间（非送钱线）降权 50%（同样只认 n≥vz_min_n）
                if value_zones:
                    _zone = value_zones.get(self._layer_of(odds))
                    if _zone is not None:
                        if _zone["n"] >= self.vz_min_n and -0.10 <= _zone["roi"] < 0:
                            stake *= 0.5
                        # 深冷层加权（2026-08-14 P1 基线）：样本≥min_n 且 ROI>min_roi
                        # 的层（当前即 L5 深冷）给 (1+boost) 倍注额加成，
                        # 仍受下方 max_single=200 封顶。
                        elif _zone["n"] >= self.boost_min_n and _zone["roi"] > self.boost_min_roi:
                            stake *= (1.0 + self.boost_mult)

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


# ---------------------------------------------------------------------------
# 投注层闸门（2026-08-29 新增，纯函数便于单测）
# 设计依据 scripts/ablation_replay.py：水位信号在概率层做 ±0.02 修正实测有害
# （关掉 +0.0013~0.0015），但它作为信号本身极强（命中时 0.693 vs 未命中 0.423）
# → 迁到投注层做注量闸门，不再污染概率。
# ---------------------------------------------------------------------------

DEFAULT_SIGNAL_CFG = {
    "enabled": True,
    "agree_stake_factor": 1.0,
    "conflict_stake_factor": 0.5,
}
DEFAULT_BAND_CFG = {
    "enabled": True,
    "odds_below": 1.8,
    "stake_factor": 0.0,
}


def market_signal_gate(direction: str, sina: dict | None, cfg: dict | None = None) -> tuple[float, str]:
    """水位信号 × 投注方向 一致性闸门。

    口径与 fetch_sina_odds.py 一致：compression = 初盘/即时盘，
    >1.05 = 该方赔率下降 = 资金涌入看好；<0.95 = 资金撤出看衰。

    Returns:
        (stake_factor, verdict)：verdict ∈ {"agree", "conflict", "unknown"}
        agree → 1.0（或配置加成）；conflict → conflict_stake_factor；unknown → 1.0
    """
    c = {**DEFAULT_SIGNAL_CFG, **(cfg or {})}
    if not c.get("enabled", True):
        return 1.0, "unknown"
    if not sina:
        return 1.0, "unknown"
    comp = (sina.get("compression") or {}) if isinstance(sina, dict) else {}
    if not comp:
        return 1.0, "unknown"
    if direction == "draw":
        # 平局方向无直接水位口径，不强判
        return 1.0, "unknown"
    own = float(comp.get(direction, 1.0) or 1.0)
    other = "away" if direction == "home" else "home"
    other_v = float(comp.get(other, 1.0) or 1.0)
    own_bull = own > 1.05
    own_bear = own < 0.95
    other_bull = other_v > 1.05
    if own_bull:
        # 己方被资金涌入 → 与市场同向
        return float(c.get("agree_stake_factor", 1.0)), "agree"
    if own_bear:
        # 己方赔率上升 = 资金撤出，逆资金下注
        return float(c.get("conflict_stake_factor", 0.5)), "conflict"
    if other_bull:
        # 己方无信号但资金涌向对手方
        return float(c.get("conflict_stake_factor", 0.5)), "conflict"
    return 1.0, "unknown"


def favorite_band_factor(odds: float, cfg: dict | None = None) -> float:
    """热门区注量因子：赔率过低的"稳胆"长期 ROI 为负
    （账本 L1 -10.3% / L2 -18.3%；2026-08-29 闸门实验 n=111，命中 69.4%
    低于 <1.8 档盈亏平衡，pnl 合计 -323.1 单位）→ 注量全免（×0），
    零注候选不出票。odds >= odds_below → 1.0。"""
    c = {**DEFAULT_BAND_CFG, **(cfg or {})}
    if not c.get("enabled", True):
        return 1.0
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return 1.0
    if 1.0 < o < float(c.get("odds_below", 1.8)):
        return float(c.get("stake_factor", 0.0))
    return 1.0
