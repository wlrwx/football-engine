from __future__ import annotations
"""逆向赔率引擎 - 从赔率结构反推博彩公司意图
移植自 JCZQ-Reverse-Odds-Engine-V6
"""
from dataclasses import dataclass, field


@dataclass
class ReverseOddsInput:
    """逆向分析输入"""
    # 胜平负赔率（当前）
    had_odds: tuple[float, float, float] = (0, 0, 0)
    # 胜平负赔率（初始/开盘）
    had_odds_initial: tuple[float, float, float] | None = None
    # 总进球赔率
    ttg_odds: list[float] | None = None
    # 比分赔率（部分关键比分）
    score_odds: dict[str, float] | None = None  # {"2:1": 8.5, "1:0": 6.0, ...}
    # "胜其它"/"平其它"/"负其它" 赔率
    other_odds: tuple[float, float, float] | None = None  # (win_other, draw_other, lose_other)
    other_odds_initial: tuple[float, float, float] | None = None


@dataclass
class DirectionResult:
    """方向引擎输出"""
    direction: str = ""  # "home" / "draw" / "away"
    strength: float = 0.0  # 0-100
    draw_pressure: str = ""  # "high" / "medium" / "low"
    upset_risk: float = 0.0  # 0-100
    probabilities: tuple[float, float, float] = (0, 0, 0)


@dataclass
class CompressionResult:
    """压缩引擎输出"""
    win_other_cr: float = 0.0  # 压缩比
    draw_other_cr: float = 0.0
    lose_other_cr: float = 0.0
    level: str = ""  # "strong" / "medium" / "weak"
    confidence: float = 0.0  # 0-100


@dataclass
class GoalResult:
    """进球引擎输出"""
    target_goals: int = 0
    confidence: float = 0.0
    distribution: list[tuple[int, float]] = field(default_factory=list)


@dataclass
class ScoreCandidate:
    """比分候选"""
    score: str  # "2:1"
    confidence: float = 0.0
    odds: float = 0.0
    direction_match: bool = False
    goal_match: bool = False


@dataclass
class ReverseAnalysis:
    """完整逆向分析结果"""
    direction: DirectionResult = field(default_factory=DirectionResult)
    compression: CompressionResult = field(default_factory=CompressionResult)
    goals: GoalResult = field(default_factory=GoalResult)
    top_scores: list[ScoreCandidate] = field(default_factory=list)
    overall_confidence: float = 0.0
    signal_summary: str = ""


class ReverseOddsEngine:
    """
    逆向赔率引擎。
    不预测比赛结果，而是解读赔率结构传递的信号。
    与正向模型（Dixon-Coles/Monte Carlo）互补。

    4层级联漏斗：方向 → 压缩 → 进球 → 比分
    """

    def analyze(self, data: ReverseOddsInput) -> ReverseAnalysis:
        """执行完整逆向分析"""
        result = ReverseAnalysis()

        # Layer 1: 方向
        result.direction = self._direction_engine(data)

        # Layer 2: 压缩
        result.compression = self._compression_engine(data)

        # Layer 3: 进球
        result.goals = self._goal_engine(data)

        # Layer 4: 比分
        result.top_scores = self._score_engine(data, result.direction, result.goals)

        # 综合置信度
        result.overall_confidence = self._overall_confidence(result)
        result.signal_summary = self._summarize(result)

        return result

    def _direction_engine(self, data: ReverseOddsInput) -> DirectionResult:
        """Layer 1: 方向判断"""
        odds = data.had_odds
        if not odds or any(o <= 1.0 for o in odds):
            return DirectionResult()

        # 去水概率
        implied = [1.0 / o for o in odds]
        total = sum(implied)
        probs = tuple(p / total for p in implied)

        # 方向 = 最高概率
        labels = ["home", "draw", "away"]
        max_idx = probs.index(max(probs))
        direction = labels[max_idx]

        # 方向强度 = 最高 - 次高
        sorted_probs = sorted(probs, reverse=True)
        strength = (sorted_probs[0] - sorted_probs[1]) * 100

        # 平局压力
        draw_prob = probs[1]
        if draw_prob > 0.30:
            draw_pressure = "high"
        elif draw_prob > 0.20:
            draw_pressure = "medium"
        else:
            draw_pressure = "low"

        # 冷门风险公式
        upset_risk = (
            (100 - strength) * 0.4
            + draw_prob * 100 * 0.3
            + probs[2] * 100 * 0.3
        )

        return DirectionResult(
            direction=direction,
            strength=round(strength, 1),
            draw_pressure=draw_pressure,
            upset_risk=round(upset_risk, 1),
            probabilities=probs,
        )

    def _compression_engine(self, data: ReverseOddsInput) -> CompressionResult:
        """
        Layer 2: 压缩比分析。
        核心洞察：初始赔率/当前赔率 在"其它"选项上的比值
        揭示博彩公司把概率集中到了哪里。
        CR >= 5 = 强压缩（高信心）
        CR 2-5 = 中压缩
        CR < 2 = 弱压缩
        """
        if not data.other_odds or not data.other_odds_initial:
            return CompressionResult(level="unknown", confidence=0)

        crs = []
        for current, initial in zip(data.other_odds, data.other_odds_initial):
            if current > 0 and initial > 0:
                crs.append(initial / current)
            else:
                crs.append(1.0)

        win_cr, draw_cr, lose_cr = crs[0], crs[1], crs[2]
        max_cr = max(crs)

        if max_cr >= 5:
            level = "strong"
            confidence = min(100, max_cr * 15)
        elif max_cr >= 2:
            level = "medium"
            confidence = max_cr * 14
        else:
            level = "weak"
            confidence = max_cr * 20

        return CompressionResult(
            win_other_cr=round(win_cr, 2),
            draw_other_cr=round(draw_cr, 2),
            lose_other_cr=round(lose_cr, 2),
            level=level,
            confidence=round(confidence, 1),
        )

    def _goal_engine(self, data: ReverseOddsInput) -> GoalResult:
        """Layer 3: 总进球判断（最低赔率 = 最高概率 = 最被支持的进球数）"""
        if not data.ttg_odds:
            return GoalResult()

        # 找最低赔率对应的进球数
        min_idx = data.ttg_odds.index(min(data.ttg_odds))
        target_goals = min_idx  # 0-indexed: 0球, 1球, 2球, ...

        # 置信度：最低赔率 vs 平均赔率的差距
        avg_odds = sum(data.ttg_odds) / len(data.ttg_odds)
        min_odds = data.ttg_odds[min_idx]
        confidence = min(100, (avg_odds / max(min_odds, 1.0) - 1) * 50)

        # 分布
        implied = [1.0 / o for o in data.ttg_odds]
        total = sum(implied)
        dist = [(i, round(p / total, 4)) for i, p in enumerate(implied)]
        dist.sort(key=lambda x: x[1], reverse=True)

        return GoalResult(
            target_goals=target_goals,
            confidence=round(confidence, 1),
            distribution=dist[:5],
        )

    def _score_engine(
        self,
        data: ReverseOddsInput,
        direction: DirectionResult,
        goals: GoalResult,
    ) -> list[ScoreCandidate]:
        """Layer 4: 比分候选（级联过滤 + 加权评分）"""
        if not data.score_odds:
            return []

        candidates = []
        for score_str, odds in data.score_odds.items():
            parts = score_str.split(":")
            if len(parts) != 2:
                continue
            h, a = int(parts[0]), int(parts[1])
            total_goals = h + a

            # 方向过滤
            if direction.direction == "home" and h <= a:
                continue
            elif direction.direction == "away" and a <= h:
                continue
            elif direction.direction == "draw" and h != a:
                continue

            # 进球过滤（±1）
            goal_match = abs(total_goals - goals.target_goals) <= 1

            # 4维评分（各25%）
            dir_score = min(100, direction.strength)
            comp_score = {"strong": 100, "medium": 70, "weak": 40}.get(
                "strong", 40
            )
            goal_score = 100 if total_goals == goals.target_goals else (70 if goal_match else 40)
            heat_score = max(0, 100 - odds * 10)

            confidence = (dir_score + comp_score + goal_score + heat_score) / 4

            candidates.append(ScoreCandidate(
                score=score_str,
                confidence=round(confidence, 1),
                odds=odds,
                direction_match=True,
                goal_match=goal_match,
            ))

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates[:3]

    def _overall_confidence(self, result: ReverseAnalysis) -> float:
        """综合置信度"""
        scores = [
            result.direction.strength,
            result.compression.confidence,
            result.goals.confidence,
        ]
        if result.top_scores:
            scores.append(result.top_scores[0].confidence)
        return round(sum(scores) / len(scores), 1) if scores else 0

    def _summarize(self, result: ReverseAnalysis) -> str:
        """信号摘要"""
        parts = []
        d = result.direction
        if d.direction:
            dir_cn = {"home": "主胜", "draw": "平局", "away": "客胜"}[d.direction]
            parts.append(f"方向:{dir_cn}(强度{d.strength:.0f})")
        if d.upset_risk > 50:
            parts.append(f"⚠冷门风险{d.upset_risk:.0f}")
        c = result.compression
        if c.level != "unknown":
            parts.append(f"压缩:{c.level}(CR={max(c.win_other_cr, c.draw_other_cr, c.lose_other_cr):.1f})")
        g = result.goals
        if g.target_goals > 0:
            parts.append(f"进球:{g.target_goals}球")
        if result.top_scores:
            parts.append(f"比分:{result.top_scores[0].score}")
        return " | ".join(parts)
