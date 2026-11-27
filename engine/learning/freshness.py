#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据新鲜度护栏（2026-08-06 新增，借鉴 MBS 概率系统方法论）

背景：MBS 概率系统对"长时间无正式比赛"的球队（季前赛、杯赛间歇、跨联赛
状态转移）明确降权 + 扩大不确定区间。我们系统此前完全没有此机制——
8/5 欧冠资格赛（里昂/奥林匹亚科斯等 79 天无正式比赛）翻车即同类问题。

实现：
1. 从 match_history.db 查询每队最后一场正式比赛日期
2. 预测时计算 days_since_last_match
3. 超过阈值 → 标记 freshness_risk 级别（watch/alert），并把方向概率向
   均势收缩（不确定性扩散），避免"旧状态当新状态"式高置信误判
4. 账本记录 freshness 信息，复盘按 risk 分层统计命中率 → 数据验证护栏有效性
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# 阈值（天）：与 MBS "79天无正式比赛→明确降权" 同类经验对齐
WATCH_DAYS = 30   # ≥30天无正式比赛 → 观察级（概率向均势轻微收缩）
ALERT_DAYS = 60   # ≥60天无正式比赛 → 预警级（概率明显收缩 + 页面警示）

# 均势参考概率（收缩目标，默认）
EVEN_PROBS = (1 / 3, 1 / 3, 1 / 3)

# 竞彩联赛名 → DJYY league_matrix.json 名称映射（缺失时按原名匹配）
LEAGUE_NAME_MAP = {
    "K1联赛": "韩K联", "韩K联": "韩K联", "韩职": "韩K联", "K联赛": "韩K联",
    "巴甲": "巴西甲", "巴西甲": "巴西甲",
    "美职联": "美职联", "美职": "美职联",
    "瑞典超": "瑞典超", "瑞超": "瑞典超",
    "挪超": "挪超", "挪威超": "挪超",
    "丹麦超": "丹超", "丹超": "丹超",
    "瑞士超": "瑞超", "瑞甲": "瑞超",
    "奥甲": "奥甲", "奥地利甲": "奥甲",
    "苏超": "苏超", "苏格兰超": "苏超",
    "中超": "中超", "中国超级联赛": "中超",
    "日职": "日职联", "日职联": "日职联",
    "沙职": "沙特联", "沙特超": "沙特联", "沙特联": "沙特联",
    "阿甲": "阿根廷联", "阿超": "阿根廷联", "阿根廷联": "阿根廷联",
    "墨超": "墨西哥联", "墨西哥联": "墨西哥联",
    "波兰甲": "波超", "波超": "波超",
}


@dataclass
class FreshnessInfo:
    """单场新鲜度信息"""
    home_days: int | None = None      # 主队距最后正式比赛天数
    away_days: int | None = None      # 客队距最后正式比赛天数
    risk: str = "ok"                  # ok / watch / alert
    shrink: float = 0.0               # 概率收缩强度 0.0~1.0（1.0=完全均势）

    def to_dict(self) -> dict:
        return {
            "home_days": self.home_days,
            "away_days": self.away_days,
            "risk": self.risk,
            "shrink": round(self.shrink, 3),
        }


class FreshnessTracker:
    """从 match_history.db 查询球队最后正式比赛日期"""

    def __init__(self, db_path: Path, league_matrix_path: Path | None = None):
        self.db_path = db_path
        self.league_matrix_path = league_matrix_path
        self._cache: dict[str, str] | None = None  # team -> last date
        self._baselines: dict[str, tuple[float, float, float]] | None = None

    def league_baseline(self, competition: str | None) -> tuple[float, float, float] | None:
        """联赛基线概率 (主胜, 平局, 客胜) —— 从 DJYY league_matrix.json 读取

        冷启动收缩目标（借鉴 MBS：无近期缓存球队向"联赛中性值"收缩，而非 1/3 均势）：
        - 联赛主胜/平局比例是更合理的"中性值"——瑞典超主胜 40.8% 的队伍
          收缩后应趋近 40.8%/25.2%/34.0%，而不是 33.3%/33.3%/33.3%
        - 无矩阵数据或联赛不在矩阵 → 返回 None（调用方回退 1/3 均势）
        """
        if not competition:
            return None
        if self._baselines is None:
            self._baselines = {}
            if self.league_matrix_path and self.league_matrix_path.exists():
                try:
                    import json
                    data = json.loads(self.league_matrix_path.read_text(encoding="utf-8"))
                    for lg in data.get("leagues", []):
                        name = lg.get("name_zh", "")
                        h = lg.get("home_win_pct")
                        d = lg.get("draw_pct")
                        if name and h is not None and d is not None:
                            h, d = float(h) / 100.0, float(d) / 100.0
                            self._baselines[name] = (h, d, max(0.0, 1.0 - h - d))
                except Exception:
                    pass
        name = LEAGUE_NAME_MAP.get(competition, competition)
        return self._baselines.get(name)

    def _load(self) -> dict[str, list[str]]:
        """team(归一化) -> 该队所有比赛日期 ISO 列表（升序）"""
        if self._cache is not None:
            return self._cache
        cache: dict[str, list[str]] = {}
        if not self.db_path.exists():
            self._cache = cache
            return cache
        try:
            conn = sqlite3.connect(self.db_path)
            conn.text_factory = str
            rows = conn.execute(
                "SELECT home_team, away_team, date FROM match_history "
                "WHERE date IS NOT NULL ORDER BY date"
            ).fetchall()
            conn.close()
            from ..team_aliases import normalize_team
            for home, away, d in rows:
                if not d:
                    continue
                # 取日期前缀（可能带时间）
                d = str(d)[:10]
                if home:
                    cache.setdefault(normalize_team(home), []).append(d)
                if away:
                    cache.setdefault(normalize_team(away), []).append(d)
        except Exception:
            pass
        self._cache = cache
        return cache

    def days_since(self, team: str, on_date: date) -> int | None:
        """球队在 on_date 当天距离【on_date 之前】最后一场正式比赛的天数

        只取 < on_date 的记录：预测时点的视角（当天比赛尚未发生，库里若有当天记录
        是结算后的回放，会污染回测）。实盘预测发生在结算前，库里本来就没有当天记录。
        """
        from ..team_aliases import normalize_team
        dates = self._load().get(normalize_team(team))
        if not dates:
            return None
        last = None
        for d in dates:
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                continue
            if dd < on_date:
                last = dd  # dates 升序，取最后一个 < on_date 的
        if last is None:
            return None
        delta = (on_date - last).days
        return max(delta, 0)

    def evaluate(self, home_team: str, away_team: str, on_date: date) -> FreshnessInfo:
        """评估一场比赛的新鲜度

        None（无近期记录）= 新鲜度未知 → watch 温和收缩：
        - 我们只预测竞彩场次，库里"无记录"可能是"真·长时间无正式比赛"（里昂/奥林匹亚科斯
          79 天无赛即此情形），也可能是"球队有比赛但我们没预测覆盖"（如对手 7/30 的欧冠）
        - 无证据时既不能当正常（会重蹈 8/5 欧冠翻车），也不能当 alert 强收缩（会误伤
          有近期比赛但未覆盖的队）→ 取中间 watch，温和向均势收缩
        """
        hd = self.days_since(home_team, on_date)
        ad = self.days_since(away_team, on_date)
        days = [d for d in (hd, ad) if d is not None]
        unknown = (hd is None) or (ad is None)

        # 任一队有近期记录且超阈值 → 按最久的那队定级
        if days:
            max_days = max(days)
            if max_days >= ALERT_DAYS:
                risk = "alert"
                # 收缩强度：60天→0.35，120天→0.55，封顶0.7（永远不完全归零）
                shrink = min(0.70, 0.35 + (max_days - ALERT_DAYS) / 200.0)
                return FreshnessInfo(home_days=hd, away_days=ad, risk=risk, shrink=shrink)
            if max_days >= WATCH_DAYS:
                return FreshnessInfo(home_days=hd, away_days=ad, risk="watch", shrink=0.20)

        # 任一队无近期记录（新鲜度未知）→ 温和降权：
        # 我们只预测竞彩场次，"无记录"可能是真·长时间无正式比赛（里昂/奥林匹亚科斯
        # 79 天无赛即此情形），也可能是球队有比赛但我们没预测覆盖 → 取中间 watch
        if unknown:
            return FreshnessInfo(home_days=hd, away_days=ad, risk="watch", shrink=0.15)

        return FreshnessInfo(home_days=hd, away_days=ad, risk="ok", shrink=0.0)

    def apply(self, probs: list[float], shrink: float, baseline: tuple | None = None) -> list[float]:
        """按收缩强度把概率向基线（默认 1/3 均势）拉（不确定性扩散）

        baseline: 联赛基线 (主, 平, 客)。无基线 → 均势 1/3（2026-08-06 升级：
        有联赛矩阵数据时向联赛中性值收缩，更符合联赛自身主客分布）
        """
        if shrink <= 0 or len(probs) != 3:
            return probs
        target = baseline if baseline and len(baseline) == 3 else EVEN_PROBS
        out = [
            probs[i] * (1 - shrink) + target[i] * shrink
            for i in range(3)
        ]
        s = sum(out) or 1.0
        return [x / s for x in out]
