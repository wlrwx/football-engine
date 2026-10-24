"""
串关（过关）方案生成器 — 竞彩实际玩法核心。

2026-08-08 新增：三票制是单场票，但竞彩玩家实际打票都是串关
（2串1/3串1/3串4 容错）。系统此前只有 parlay_report.py（历史回测），
无当日串关方案生成。strategy.json 已预留 max_parlay_stake/max_parlay_legs。

数学真相（账本 120 场校准，2026-08-08）：
- 方向命中率 43.3%；模型概率段校准：
  [0.50,0.55)=43.8% [0.55,0.60)=31.6%(塌陷区!) [0.60,0.65)=50% [0.65,0.70)=66.7% [0.70+)=50%
- 2串1 天然吃双重抽水（-10% 左右）；模型概率系统性高估 →
  按模型概率串关 = 送钱。唯一正确姿势：校准命中率算真实 EV，正 EV 才出串。

设计：
- 胆材：纯胜平负方向（排除让球/总进球等玩法），融合方向概率 ≥ min_prob(0.60)
  —— 0.55-0.60 是平局盲点塌陷区，**禁止入串**（three_ticket 同款纪律）
- 串法：2串1（全组合 EV 排序）/ 3串1 / 3串4 容错（错1场回血）
- 双 EV：model_ev（模型概率口径，展示用）+ cal_ev（账本校准口径，决策用）
- 推荐 = cal_ev > 0；否则标 ⚠ 负EV 不出注（页面展示但不推荐）
- 注额：串关池 = min(0.006×bankroll, max_parlay_stake)，1/4 Kelly×0.5
- 校准表从 review_ledger.jsonl 自动计算（样本<8 回退整体命中率），数据驱动
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

# 竞彩过关规则：每注 2 元起
STAKE_UNIT = 2.0


@dataclass
class ParlayConfig:
    min_prob: float = 0.60          # 腿最低融合方向概率（已弃用为硬门槛，仅作展示参考）
    max_odds: float = 4.50          # 单腿赔率上限（2026-08-13：3.0→4.5 放行正EV高赔腿）
    min_odds: float = 1.45          # 单腿赔率下限（2026-08-13：避开 1.0-1.5 庄家抽水最狠区，
                                    # 账本 1.0-1.5 段 53 场命中 58.5% 仍亏 -236——大热隐含概率
                                    # 77-83% 远高于实际，EV 必负；2.0-3.5 段校准后 +4%~+28%）
    value_edge: float = 1.05        # 价值门槛：校准命中率×赔率 ≥ 1.05（EV>5%）才入池
                                    # （2026-08-13 核心改动：原"模型概率≥60%"把腿全选进
                                    # 高置信塌陷区——70-80% 段实测仅 44%，且 1.2-1.5 大热
                                    # 校准 EV -36%~-55%，串关等于双重送钱）
    max_legs: int = 3               # 最高串数（受 strategy.json max_parlay_legs 约束）
    max_parlay_stake: float = 30.0  # 串关日总投入上限（strategy.json）
    kelly_discount: float = 0.5     # 串关波动大，Kelly 再打 5 折
    max_tickets: int = 5            # 最多展示几张串票（2026-08-10：3×2串1+1×3串1+1×3串4）
    cal_min_samples: int = 8        # 校准段最小样本（不足回退整体）
    cal_overall: float = 0.433      # 整体方向命中率（账本 120 场实测）
    cal_table: dict | None = None   # {下限: 命中率} 由账本自动计算
    # 市场腿（2026-08-08 新增）：融合概率被平局修正层层压低（8/4 后无腿可串），
    # 但账本实证"市场比模型准"（模型>市场命中 27.8% vs 模型<市场 52.3%）。
    # 市场公平概率 ≥ market_min_prob 的场次也入池（source='market'），
    # 用市场段实际命中率算期望，标注娱乐串（期望≈-15%~-40%，水钱）。
    market_min_prob: float = 0.55    # 市场腿最低市场公平概率（去水后）
    market_table: dict | None = None  # {下限: 市场段实际命中率} 账本实测


# 市场公平概率段 → 实际命中率（review_ledger 131 场实测，2026-08-08）
# [0.55,0.65): 47.1% (17场8中) | [0.65,0.75): 54.5% (11场6中) | [0.75+): 100% (2场2中，样本小)
# 合并 0.65+ = 61.5% (13场8中)
DEFAULT_MARKET_TABLE = {0.65: 0.615, 0.55: 0.471}


@dataclass
class ParlayLeg:
    match_id: str
    home_team: str
    away_team: str
    competition: str
    selection: str      # home / draw / away
    odds: float
    prob: float         # 融合方向概率（模型口径）
    cal_prob: float = 0.0   # 账本校准命中率（2026-08-08：模型概率系统性高估）
    market_prob: float = 0.0  # 市场公平概率（三方向赔率去水）
    source: str = "fusion"    # "fusion"（融合池）/ "market"（市场池）
    hit_prob: float = 0.0   # 该腿"实际命中率"：fusion=账本校准 / market=市场段实测


@dataclass
class ParlayTicket:
    parlay_type: str    # "2串1" / "3串1" / "3串4"
    legs: list[ParlayLeg] = field(default_factory=list)
    total_odds: float = 0.0     # 全中总赔率
    model_ev: float = 0.0       # 模型口径期望盈利（元）
    cal_ev: float = 0.0         # 实际口径期望盈利（元）——决策依据
    cal_roi: float = 0.0        # 实际 ROI
    hit_prob_cal: float = 0.0   # 实际全中概率
    recommended: bool = False   # cal_ev>0 才推荐
    stake: float = 0.0          # 投入（元）
    n_bets: int = 0             # 注数
    source: str = "calibrated"  # "calibrated"（校准正EV ⭐）/ "market"（娱乐串 🎯）
    market_ev: float = 0.0      # 市场口径期望盈利（元，展示用）
    market_roi: float = 0.0     # 市场口径 ROI
    potential: float = 0.0      # 理论最高奖金（元）
    worst_win: float = 0.0      # 最差命中回报（2串1/3串1=0；3串4=错1场中2串1）
    note: str = ""              # 容错/说明

    def to_dict(self) -> dict:
        return {
            "type": self.parlay_type,
            "legs": [{
                "match": l.match_id, "home": l.home_team, "away": l.away_team,
                "league": l.competition, "sel": l.selection,
                "odds": round(l.odds, 2), "prob": round(l.prob, 3),
                "cal_prob": round(l.cal_prob, 3),
                "market_prob": round(l.market_prob, 3),
                "source": l.source,
                "hit_prob": round(l.hit_prob, 3) if l.hit_prob else None,
            } for l in self.legs],
            "total_odds": round(self.total_odds, 2),
            "model_ev": round(self.model_ev, 2) if self.model_ev is not None else None,
            "cal_ev": round(self.cal_ev, 2),
            "cal_roi": round(self.cal_roi, 4),
            "market_ev": round(self.market_ev, 2) if self.market_ev is not None else None,
            "market_roi": round(self.market_roi, 4) if self.market_roi is not None else None,
            "hit_prob_cal": round(self.hit_prob_cal, 3),
            "recommended": self.recommended,
            "stake": round(self.stake, 2),
            "n_bets": self.n_bets,
            "potential": round(self.potential, 2),
            "worst_win": round(self.worst_win, 2),
            "note": self.note,
            "source": self.source,
        }


def load_calibration(
    ledger_path: str | Path = "data/state/review_ledger.jsonl",
    overall: float = 0.433,
    min_samples: int = 8,
) -> dict:
    """从结算账本自动计算概率段→命中率校准表。

    口径：final_prob[best_selection]（融合概率 argmax）按段统计实际方向命中率。
    样本不足的段回退整体命中率（保守：宁可低估不可高估，串关亏钱伤士气）。
    """
    recs = []
    try:
        with open(ledger_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        return {}
    if len(recs) < 20:
        return {}

    bins = [(0.35, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.55),
            (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 1.01)]
    table = {}
    for lo, hi in bins:
        cnt = hit = 0
        for r in recs:
            fp = r.get("final_prob") or []
            bs = r.get("best_selection")
            if bs is None or bs >= len(fp):
                continue
            p = fp[bs]
            if lo <= p < hi:
                cnt += 1
                if r.get("hit"):
                    hit += 1
        if cnt >= min_samples:
            table[lo] = round(hit / cnt, 4)
        # 样本不足段不填 → 回退 overall
    return {"table": table, "overall": round(sum(1 for r in recs if r.get("hit")) / len(recs), 4),
            "n": len(recs), "min_samples": min_samples}


def allocate_parlay_stakes(recs: list, weights: list[float], pool_cap: float) -> None:
    """把串关池按权重分配给推荐串票，**硬保证**总注额不超过 pool_cap。

    2026-10-23 修复（P2#18）：原实现每票独立 `min(raw, pool_cap*0.6)`，
    多票叠加可达池的 70%+（0.6 + 0.1 模式）；且 `max(..., STAKE_UNIT)` 在
    raw < 2 元时会进一步抬高总额。改为从**剩余池**按剩余权重比例滚动
    分配，每票以剩余池为硬上限。
    """
    remaining = round(float(pool_cap), 2)
    for i, (t, w) in enumerate(zip(recs, weights)):
        rest = sum(weights[i:])
        raw = remaining * (w / rest) if rest > 0 else 0.0
        stake = min(raw, pool_cap * 0.6, remaining)
        stake = max(stake, min(STAKE_UNIT, remaining))
        t.stake = round(float(stake), 2)
        remaining = round(remaining - t.stake, 2)
        # 重算注额口径 EV（2026-08-08 修复：potential/cal_ev 此前仍按 2 元
        # 口径，与实际 stake 不符；cal_roi 为每元回报率）
        t.potential = round(t.total_odds * t.stake, 2)
        t.cal_ev = round(t.stake * t.cal_roi, 2)


class ParlayBuilder:
    """当日串关方案生成器（校准 EV 驱动）"""

    def __init__(
        self,
        bankroll: float = 5000.0,
        limits: dict | None = None,
        config: ParlayConfig | None = None,
        calibration: dict | None = None,
        league_forbid: set | None = None,
    ):
        self.bankroll = bankroll
        self.limits = limits or {}
        self.cfg = config or ParlayConfig()
        _legs = self.limits.get("max_parlay_legs")
        if _legs:
            self.cfg.max_legs = min(int(_legs), 3)
        _stake = self.limits.get("max_parlay_stake")
        if _stake:
            self.cfg.max_parlay_stake = float(_stake)
        # 校准表：账本驱动
        self.cal = calibration or {}
        self.last_pool_n = 0  # 最近一次 build 的可串池腿数（页面空仓文案用）
        self.cal_table = self.cal.get("table") or self.cfg.cal_table or {}
        self.cal_overall = self.cal.get("overall") or self.cfg.cal_overall
        # 送钱区联赛禁投集合（2026-08-14：串关此前绕过联赛禁投，选芬超平局腿）
        self.league_forbid = league_forbid or set()

    # ---------- 校准概率 ----------
    def _cal_prob(self, p: float) -> float:
        """模型概率 → 校准命中率（分段查表，样本不足/无表回退整体）"""
        if not self.cal_table:
            return self.cal_overall
        lo = None
        for k in sorted(self.cal_table):
            if p >= k:
                lo = k
            else:
                break
        if lo is None:
            return self.cal_overall
        return self.cal_table[lo]

    # ---------- 胆池 ----------
    def _market_prob(self, pred: dict, sel: str) -> float:
        """三方向赔率去水算市场公平概率（竞彩赔率含约 5-8% 水钱）"""
        key = f"{sel}_odds"
        odds = pred.get(key) or 0
        if not odds or odds <= 1.0:
            return 0.0
        inv = {"home": 1.0 / (pred.get("home_odds") or 0) if pred.get("home_odds") else 0.0,
               "draw": 1.0 / (pred.get("draw_odds") or 0) if pred.get("draw_odds") else 0.0,
               "away": 1.0 / (pred.get("away_odds") or 0) if pred.get("away_odds") else 0.0}
        total_inv = sum(inv.values())
        if total_inv <= 0:
            return 0.0
        return (1.0 / odds) / total_inv

    def _leg_hit_prob(self, leg: ParlayLeg) -> float:
        """腿的'实际命中率'：fusion 腿用账本校准表，market 腿用市场段实测表"""
        if leg.hit_prob:
            return leg.hit_prob
        if leg.source == "market":
            mtab = self.cfg.market_table or DEFAULT_MARKET_TABLE
            for lo in sorted(mtab, reverse=True):
                if leg.market_prob >= lo:
                    return mtab[lo]
            return self.cfg.cal_overall
        return leg.cal_prob or self.cfg.cal_overall

    def _build_pool(
        self, candidates: list[dict], predictions: list[dict] | None = None
    ) -> list[ParlayLeg]:
        """可串腿：纯胜平负。
        - fusion 腿：融合方向概率 ≥ min_prob（避开 0.55-0.60 塌陷区）
        - market 腿：市场公平概率 ≥ market_min_prob（2026-08-08 新增，
          融合概率被平局修正层层压低后无腿可串，但账本实证市场比模型准）

        队名优先取 candidates（main.py 构造时若带），否则用 match_id 反查
        predictions（2026-08-08 修复：main.py candidates 未带队名，串票腿曾显示空队名）。
        """
        pred_map = {p.get("match_id"): p for p in (predictions or [])}
        # parlay 胆材池 = 全部场次方向（2026-08-08 修复：main.py 的 candidates
        # 只含正 EV 价值场次，8/4 后融合概率被压低 edge 几乎全负 → candidates
        # 缺热门场 → 串关无腿。parlay 独立构建候选，不受单关价值过滤限制）
        seen = set()
        entries: list[dict] = []
        for c in candidates:
            mid = c.get("match_id", "")
            if mid in seen:
                continue
            seen.add(mid)
            entries.append(c)
        for p in (predictions or []):
            mid = p.get("match_id", "")
            if mid in seen:
                continue
            d = p.get("direction")
            if not d:
                continue
            o = p.get(f"{d}_odds") or 0
            prob = p.get("direction_prob") or p.get(f"{d}_win_prob", 0)
            if not o or o <= 1.0 or not prob:
                continue
            seen.add(mid)
            entries.append({"match_id": mid, "selection": d, "odds": o, "prob": prob})
        pool: list[ParlayLeg] = []
        for c in entries:
            sel = c.get("selection", "")
            if sel not in ("home", "draw", "away"):
                continue
            match_id = c.get("match_id", "")
            pred = pred_map.get(match_id, {})
            # 送钱区联赛禁投：串关腿与单关同口径（2026-08-14 修复，此前绕过禁投选芬超平局）
            if self.league_forbid and pred.get("competition") in self.league_forbid:
                continue
            odds = c.get("odds", 0) or 0
            prob = c.get("prob", 0) or 0
            if odds > self.cfg.max_odds:
                continue
            if odds < self.cfg.min_odds:
                continue
            market_prob = self._market_prob(pred, sel)
            cal_prob = self._cal_prob(prob)
            # 2026-08-13 核心改动：价值选腿替代"模型高置信"选腿。
            # 旧逻辑（prob≥0.60）把腿全选进高置信塌陷区——账本 70-80% 段实测仅 44%，
            # 1.2-1.5 大热校准 EV -36%~-55%。新逻辑要求"校准命中率×赔率≥value_edge"，
            # 只有正 EV 价值腿才入池（2.0-3.5 段校准后 +4%~+28%）。
            edge_ok = cal_prob * odds >= self.cfg.value_edge
            if edge_ok:
                leg = ParlayLeg(
                    match_id=match_id,
                    home_team=c.get("home_team") or pred.get("home_team", ""),
                    away_team=c.get("away_team") or pred.get("away_team", ""),
                    competition=c.get("competition") or pred.get("competition", ""),
                    selection=sel, odds=odds, prob=prob,
                    cal_prob=cal_prob,
                    market_prob=market_prob, source="fusion",
                )
                leg.hit_prob = leg.cal_prob
                pool.append(leg)
            # 市场腿（市场口径门槛；账本实证市场比模型准）——同样要过价值门槛，
            # 市场公平概率≥0.65 段实测 61.5%，若×赔率也够正 EV 才入池
            elif market_prob >= self.cfg.market_min_prob and market_prob * odds >= self.cfg.value_edge:
                leg = ParlayLeg(
                    match_id=match_id,
                    home_team=c.get("home_team") or pred.get("home_team", ""),
                    away_team=c.get("away_team") or pred.get("away_team", ""),
                    competition=c.get("competition") or pred.get("competition", ""),
                    selection=sel, odds=odds, prob=prob,
                    cal_prob=cal_prob,
                    market_prob=market_prob, source="market",
                )
                leg.hit_prob = self._leg_hit_prob(leg)  # 市场段实测命中率
                pool.append(leg)
        pool.sort(key=lambda l: max(l.prob, l.market_prob), reverse=True)
        return pool

    # ---------- 串票构造 ----------
    def _finish(self, t: ParlayTicket) -> ParlayTicket:
        """按腿实际命中率（fusion→账本校准 / market→市场段实测）填 EV 口径"""
        p_cal = 1.0
        p_mod = 1.0
        p_mkt = 1.0
        for l in t.legs:
            p_cal *= self._leg_hit_prob(l)
            p_mod *= l.prob
            p_mkt *= (l.market_prob or l.prob)
        o = t.total_odds
        t.hit_prob_cal = p_cal
        t.model_ev = t.potential * p_mod - t.stake
        t.cal_ev = t.potential * p_cal - t.stake
        t.cal_roi = o * p_cal - 1.0
        t.market_ev = t.potential * p_mkt - t.stake
        t.market_roi = o * p_mkt - 1.0
        # 推荐 = 校准 EV > 0（2026-08-14 起方向概率已修正，cal_ev 不再虚高）。
        # market_ev 仍落盘展示作"市场口径"参考（几乎必然为负=双重抽水），
        # 但不作为硬性否决——用户要串关，就诚实地把 cal/market 两种口径都摆出来。
        t.recommended = t.cal_ev > 0
        t.source = "calibrated" if all(l.source == "fusion" for l in t.legs) else "market"
        return t

    def _make_2in1(self, l1: ParlayLeg, l2: ParlayLeg) -> ParlayTicket:
        o = l1.odds * l2.odds
        stake = STAKE_UNIT
        t = ParlayTicket(
            parlay_type="2串1", legs=[l1, l2], total_odds=o,
            stake=stake, n_bets=1, potential=stake * o,
            worst_win=0.0, note="两场全中才赢",
        )
        return self._finish(t)

    def _make_3in1(self, legs: list[ParlayLeg]) -> ParlayTicket:
        o = 1.0
        for l in legs:
            o *= l.odds
        stake = STAKE_UNIT
        t = ParlayTicket(
            parlay_type="3串1", legs=legs, total_odds=o,
            stake=stake, n_bets=1, potential=stake * o,
            worst_win=0.0, note="三场全中才赢",
        )
        return self._finish(t)

    def _make_3in4(self, legs: list[ParlayLeg]) -> ParlayTicket:
        """3串4 容错：3 注 2串1 + 1 注 3串1 = 4 注，投入 8 元。错 1 场仍回血。"""
        l1, l2, l3 = legs
        o1, o2, o3 = l1.odds, l2.odds, l3.odds
        p1c, p2c, p3c = self._leg_hit_prob(l1), self._leg_hit_prob(l2), self._leg_hit_prob(l3)
        p1m, p2m, p3m = (l1.market_prob or l1.prob), (l2.market_prob or l2.prob), (l3.market_prob or l3.prob)
        n_bets = 4
        stake = STAKE_UNIT * n_bets
        # 每注期望回报（实际命中率口径）
        e12 = STAKE_UNIT * o1 * o2 * p1c * p2c
        e13 = STAKE_UNIT * o1 * o3 * p1c * p3c
        e23 = STAKE_UNIT * o2 * o3 * p2c * p3c
        e123 = STAKE_UNIT * o1 * o2 * o3 * p1c * p2c * p3c
        exp_return = e12 + e13 + e23 + e123
        # 市场口径（展示用）
        m12 = STAKE_UNIT * o1 * o2 * p1m * p2m
        m13 = STAKE_UNIT * o1 * o3 * p1m * p3m
        m23 = STAKE_UNIT * o2 * o3 * p2m * p3m
        m123 = STAKE_UNIT * o1 * o2 * o3 * p1m * p2m * p3m
        market_return = m12 + m13 + m23 + m123
        potential = STAKE_UNIT * (o1 * o2 + o1 * o3 + o2 * o3 + o1 * o2 * o3)
        worst = STAKE_UNIT * min(o1 * o2, o1 * o3, o2 * o3)
        t = ParlayTicket(
            parlay_type="3串4", legs=legs, total_odds=potential / stake,
            stake=stake, n_bets=n_bets, potential=potential, worst_win=worst,
            note=f"容错：错 1 场仍中 1 注 2串1（回 ¥{worst:.0f}），全中 ¥{potential:.0f}",
        )
        # 3串4 的期望/ROI 按全注口径单独算
        t.hit_prob_cal = p1c * p2c * p3c
        t.model_ev = None  # 3串4 模型口径不展示（用实际口径）
        t.cal_ev = exp_return - stake
        t.cal_roi = exp_return / stake - 1.0
        t.market_ev = market_return - stake
        t.market_roi = market_return / stake - 1.0
        t.recommended = t.cal_ev > 0
        t.source = "calibrated" if all(l.source == "fusion" for l in t.legs) else "market"
        return t

    # ---------- 主入口 ----------
    def build(
        self,
        candidates: list[dict],
        ticket_plan=None,
        predictions: list[dict] | None = None,
    ) -> list[ParlayTicket]:
        pool = self._build_pool(candidates, predictions)
        self.last_pool_n = len(pool)
        if not pool:
            return []
        n = min(len(pool), self.cfg.max_legs)
        pool = pool[:n]

        tickets: list[ParlayTicket] = []
        # 2串1：top6 场两两组合，按实际口径 ROI 取 top3
        # （2026-08-10 用户："胜负也像比分串一样：3个2串1+1个3串1+1个3串4"；
        #  与比分串结构对齐，不再被 max_tickets 截断只留推荐票）
        top6 = pool[:6]
        two_in_one = [self._make_2in1(l1, l2) for l1, l2 in combinations(top6, 2)]
        two_in_one.sort(
            key=lambda t: (t.cal_roi if t.cal_roi is not None else -9.0),
            reverse=True,
        )
        tickets.extend(two_in_one[:3])

        if n >= 3 and self.cfg.max_legs >= 3:
            top3 = pool[:3]
            tickets.append(self._make_3in1(list(top3)))
            tickets.append(self._make_3in4(list(top3)))

        if not tickets:
            return []

        # 固定结构排序：2串1 → 3串1 → 3串4(容错)，同组按实际 ROI 降序
        # （与比分串展示一致，用户按玩法分组看）
        order = {"2串1": 0, "3串1": 1, "3串4": 2}
        tickets.sort(
            key=lambda t: (
                order.get(t.parlay_type, 9),
                -(t.cal_roi if t.cal_roi is not None else -9.0),
            )
        )
        tickets = tickets[: self.cfg.max_tickets]

        # 注额：推荐串分配串关池（min(0.006×bankroll, max_parlay_stake)）
        pool_cap = min(self.bankroll * 0.006, self.cfg.max_parlay_stake)
        recs = [t for t in tickets if t.recommended]
        for t in tickets:
            if not t.recommended:
                # 娱乐串：1 注小注（2026-08-08：用户要看到实际可打的串；
                # 期望为负但小注参与，页面明确标注）。
                # 2026-08-10 修复：容错票（3串4=4注）保持 n_bets×2 元，
                # 不能被覆盖成 1 注 2 元（此前 3串4 被 max_tickets 截断
                # 从不展示，注额 bug 被掩盖）。
                t.stake = STAKE_UNIT * max(t.n_bets, 1)
                t.potential = round(t.total_odds * t.stake, 2)
                t.cal_ev = round(t.stake * t.cal_roi, 2)
        if recs:
            weights = []
            for t in recs:
                denom = max(t.total_odds - 1.0, 0.1)
                kelly_f = max(t.cal_ev / (t.stake * denom), 0.0)
                weights.append(kelly_f * self.cfg.kelly_discount)
            wsum = sum(weights)
            if wsum <= 0:
                weights = [1.0 / len(recs)] * len(recs)
                wsum = 1.0
            allocate_parlay_stakes(recs, weights, pool_cap)
        return tickets


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else "2026-07-26"
    cal = load_calibration()
    print(f"校准表: {cal.get('n', 0)} 场 | 整体 {cal.get('overall', 0.433):.1%} | "
          f"分段 { {f'{k:.2f}+': f'{v:.0%}' for k, v in (cal.get('table') or {}).items()} }")
    preds = json.load(open(f"data/daily/{day}/predictions.json"))
    if isinstance(preds, dict):
        preds = preds.get("predictions", [])
    cands = []
    for p in preds:
        d = p.get("direction")
        if not d:
            continue
        prob = p.get("direction_prob") or p.get(f"{d}_win_prob", 0)
        odds = p.get(f"{d}_odds", 0)
        if not odds:
            continue
        cands.append({
            "match_id": p["match_id"],
            "home_team": p.get("home_team", ""),
            "away_team": p.get("away_team", ""),
            "competition": p.get("competition", ""),
            "selection": d, "odds": odds, "prob": prob,
        })
    b = ParlayBuilder(bankroll=5000, limits={"max_parlay_legs": 3, "max_parlay_stake": 30},
                      calibration=cal)
    tickets = b.build(cands, None, preds)
    pool = b._build_pool(cands, preds)
    print(f"{day}: 候选 {len(cands)} 场, 可串 {len(pool)} 腿, 出 {len(tickets)} 张串票")
    for t in tickets:
        legs = " + ".join(f"{l.home_team[:4]}({l.selection[:1]})@{l.odds:.2f}" for l in t.legs)
        flag = "⭐推荐" if t.recommended else "⚠负EV"
        print(f"  [{t.parlay_type}]{flag} {legs} | 总赔率{t.total_odds:.2f} "
              f"校准命中率{t.hit_prob_cal:.0%} 校准EV{t.cal_ev:+.1f}元 ROI{t.cal_roi:+.0%} "
              f"投入¥{t.stake:.0f} 最高¥{t.potential:.0f}")
