"""
同赔分析 — 历史相同/相似赔率下的赛果统计。

来源: football_frontend 同赔匹配
核心思想:
  - 给定当前赔率组合 (主胜/平/客胜)，在历史数据中找相似赔率
  - 统计这些"同赔"比赛的实际赛果分布
  - 如果历史上同赔比赛主胜率70%，但当前赔率隐含主胜率只有55%，
    则说明市场可能低估了主队
  - 容差: ±0.015 (欧赔精度通常到小数点后2位)
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SameOddsConfig:
    """同赔分析参数"""
    tolerance: float = 0.015       # 赔率匹配容差
    min_matches: int = 5           # 最少匹配场次才出结论
    max_matches: int = 200         # 最多使用匹配数
    weight_recent: bool = True     # 是否对近期比赛加权
    recent_half_life_days: int = 365  # 时间衰减半衰期


@dataclass
class OddsRecord:
    """历史赔率记录"""
    match_id: str
    date: str
    home_odds: float
    draw_odds: float
    away_odds: float
    result: str  # "H" / "D" / "A"
    league: str = ""


@dataclass
class SameOddsResult:
    """同赔分析结果"""
    matched_count: int = 0
    home_win_rate: float = 0.0
    draw_rate: float = 0.0
    away_win_rate: float = 0.0
    # 与当前赔率隐含概率的偏差
    home_bias: float = 0.0    # 正=历史支持主胜多于赔率暗示
    draw_bias: float = 0.0
    away_bias: float = 0.0
    confidence: float = 0.0   # 基于样本量的置信度
    matches: list[dict] = field(default_factory=list)


class SameOddsAnalyzer:
    """
    同赔分析器。

    用法:
        analyzer = SameOddsAnalyzer("data/historical/odds.csv")
        result = analyzer.analyze(home_odds=1.85, draw_odds=3.40, away_odds=4.20)
        # result.home_win_rate = 历史同赔主胜率
        # result.home_bias = 历史主胜率 - 赔率隐含主胜率
    """

    def __init__(
        self,
        odds_path: str | Path = "data/historical/odds.csv",
        config: SameOddsConfig | None = None,
    ):
        self.cfg = config or SameOddsConfig()
        self.records: list[OddsRecord] = []
        self._load(Path(odds_path))

    def _load(self, path: Path) -> None:
        """加载历史赔率数据"""
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rec = OddsRecord(
                        match_id=row.get("match_id", ""),
                        date=row.get("date", ""),
                        home_odds=float(row.get("home_odds", 0)),
                        draw_odds=float(row.get("draw_odds", 0)),
                        away_odds=float(row.get("away_odds", 0)),
                        result=row.get("result", ""),
                        league=row.get("league", ""),
                    )
                    if rec.home_odds > 0 and rec.result in ("H", "D", "A"):
                        self.records.append(rec)
                except (ValueError, TypeError):
                    continue

    def analyze(
        self,
        home_odds: float,
        draw_odds: float,
        away_odds: float,
        league: str | None = None,
    ) -> SameOddsResult:
        """
        查找历史同赔比赛并统计赛果。

        Args:
            home_odds: 当前主胜赔率
            draw_odds: 当前平局赔率
            away_odds: 当前客胜赔率
            league: 可选，限定联赛
        """
        tol = self.cfg.tolerance
        matched = []

        for rec in self.records:
            if league and rec.league != league:
                continue
            if (
                abs(rec.home_odds - home_odds) <= tol
                and abs(rec.draw_odds - draw_odds) <= tol
                and abs(rec.away_odds - away_odds) <= tol
            ):
                matched.append(rec)

        # 限制最大匹配数（优先近期）
        if len(matched) > self.cfg.max_matches:
            matched.sort(key=lambda r: r.date, reverse=True)
            matched = matched[: self.cfg.max_matches]

        if len(matched) < self.cfg.min_matches:
            return SameOddsResult(matched_count=len(matched))

        # 统计赛果
        h_count = sum(1 for r in matched if r.result == "H")
        d_count = sum(1 for r in matched if r.result == "D")
        a_count = sum(1 for r in matched if r.result == "A")
        total = len(matched)

        h_rate = h_count / total
        d_rate = d_count / total
        a_rate = a_count / total

        # 赔率隐含概率（去水后）
        raw_sum = 1 / home_odds + 1 / draw_odds + 1 / away_odds
        implied_h = (1 / home_odds) / raw_sum
        implied_d = (1 / draw_odds) / raw_sum
        implied_a = (1 / away_odds) / raw_sum

        # 偏差 = 历史实际 - 赔率隐含
        home_bias = h_rate - implied_h
        draw_bias = d_rate - implied_d
        away_bias = a_rate - implied_a

        # 置信度（基于样本量，Wilson思想简化版）
        confidence = min(1.0, total / 50) * (1 - 1.96 / (2 * total**0.5))
        confidence = max(0.0, confidence)

        return SameOddsResult(
            matched_count=total,
            home_win_rate=round(h_rate, 4),
            draw_rate=round(d_rate, 4),
            away_win_rate=round(a_rate, 4),
            home_bias=round(home_bias, 4),
            draw_bias=round(draw_bias, 4),
            away_bias=round(away_bias, 4),
            confidence=round(confidence, 4),
            matches=[
                {"id": r.match_id, "date": r.date, "result": r.result, "league": r.league}
                for r in matched[:20]  # 只返回前20条详情
            ],
        )

    def find_value_signals(
        self,
        home_odds: float,
        draw_odds: float,
        away_odds: float,
        threshold: float = 0.05,
    ) -> list[dict]:
        """
        找出正偏差信号（历史实际概率 > 赔率隐含概率超过阈值）。
        """
        result = self.analyze(home_odds, draw_odds, away_odds)
        signals = []

        if result.matched_count < self.cfg.min_matches:
            return signals

        if result.home_bias > threshold:
            signals.append({
                "selection": "home",
                "bias": result.home_bias,
                "historical_rate": result.home_win_rate,
                "implied_prob": round(1 / home_odds, 4),
            })
        if result.draw_bias > threshold:
            signals.append({
                "selection": "draw",
                "bias": result.draw_bias,
                "historical_rate": result.draw_rate,
                "implied_prob": round(1 / draw_odds, 4),
            })
        if result.away_bias > threshold:
            signals.append({
                "selection": "away",
                "bias": result.away_bias,
                "historical_rate": result.away_win_rate,
                "implied_prob": round(1 / away_odds, 4),
            })

        return signals

    def stats_summary(self) -> dict:
        """数据摘要"""
        return {
            "total_records": len(self.records),
            "leagues": len(set(r.league for r in self.records if r.league)),
            "date_range": (
                min(r.date for r in self.records) if self.records else "",
                max(r.date for r in self.records) if self.records else "",
            ),
        }
