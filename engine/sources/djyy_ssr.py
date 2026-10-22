"""DJYY SSR 数据源 - 从 djyydata.com RSC 提取真实 xG 和赔率

数据来源: djyydata.com Next.js SSR 渲染的 RSC Flight Data
可获取: 赛前 xG、bet365/Pinnacle 赔率、中文队名、角球、红牌等

用法:
    src = DJYYSSRSource(Path("data/djyy_matches.json"))
    enrichment = src.enrich_prediction("首尔FC", "浦项制铁")
    # → {home_xg: 1.39, away_xg: 1.29, home_odds: 1.80, ...}
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .team_match import match_pair

# duty 端按同一阈值决定是否补抓快照；这里只负责让过期在预测日志里可见。
SNAPSHOT_MAX_AGE_H = 10.0


class DJYYSSRSource:
    """DJYY SSR 数据源 — 提供真实赛前 xG 和赔率"""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._matches: list[dict] = []
        self._extracted_at: str | None = None
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text())
                if isinstance(data, list):
                    self._matches = data
                elif isinstance(data, dict) and "matches" in data:
                    self._matches = data["matches"]
                    self._extracted_at = data.get("extracted_at")
            except Exception:
                pass
        self._loaded = True

    @property
    def extracted_at(self) -> str | None:
        self._load()
        return self._extracted_at

    def age_hours(self, now: datetime | None = None) -> float | None:
        """快照年龄（小时）；无 extracted_at 或解析失败返回 None。"""
        raw = self.extracted_at
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now = now or datetime.now(timezone.utc)
            return (now - ts).total_seconds() / 3600.0
        except (ValueError, TypeError):
            return None

    @property
    def matches(self) -> list[dict]:
        self._load()
        return self._matches

    def enrich_prediction(self, home_team: str, away_team: str) -> dict:
        """用 DJYY 数据增强一场预测，返回 {prematch_xg, odds, cn_names}

        匹配策略（多级，解决跨源译名不一致）:
            1. 中文队名精确/别名/子串匹配（match_pair）
            2. 英文队名精确匹配
        """
        self._load()
        for m in self._matches:
            # 中文队名匹配（精确 + 别名 + 子串，见 team_match.match_pair）
            if match_pair(
                m.get("home_name_cn", ""), m.get("away_name_cn", ""),
                home_team, away_team,
            ):
                return self._extract_enrichment(m)
            # 英文名匹配
            if (m.get("home_name") == home_team and
                    m.get("away_name") == away_team):
                return self._extract_enrichment(m)
        return {}

    def _extract_enrichment(self, m: dict) -> dict:
        result = {}

        # 赛前 xG (DJYY/FootyStats 真实数据)
        hpx = m.get("home_prematch_xg")
        apx = m.get("away_prematch_xg")
        if hpx and apx:
            try:
                result["home_xg_djyy"] = float(hpx)
                result["away_xg_djyy"] = float(apx)
            except (ValueError, TypeError):
                pass

        # 赛后实际 xG (已完赛)
        hx = m.get("home_xg")
        ax = m.get("away_xg")
        if hx and ax:
            try:
                hxf = float(hx)
                axf = float(ax)
                if hxf > 0 or axf > 0:
                    result["home_xg_actual"] = hxf
                    result["away_xg_actual"] = axf
            except (ValueError, TypeError):
                pass

        # 比分 (已完赛)
        hg = m.get("home_goals")
        ag = m.get("away_goals")
        if hg is not None and ag is not None:
            try:
                result["home_score_djyy"] = int(hg)
                result["away_score_djyy"] = int(ag)
            except (ValueError, TypeError):
                pass

        # 赔率 (bet365 + Pinnacle) — 新版 JSON 字段优先，旧版 odds_comparison 字符串兜底
        odds_data = {}
        # 新版: fetch_djyy_ssr.py v2 (2026-07-31 改版) 直接输出结构化字段
        if m.get("home_odds_djyy") or m.get("draw_odds_djyy") or m.get("away_odds_djyy"):
            odds_data = {
                "Home": {"Pinnacle": m.get("home_odds_djyy")},
                "Draw": {"Pinnacle": m.get("draw_odds_djyy")},
                "Away": {"Pinnacle": m.get("away_odds_djyy")},
            }
        else:
            # 旧版: RSC 数据中的 odds_comparison 字符串
            odds_str = m.get("odds_comparison", "")
            if odds_str and isinstance(odds_str, str) and len(odds_str) > 10:
                # 尝试 JSON 解析
                try:
                    parsed = json.loads(odds_str)
                    ft = parsed.get("FT Result", parsed)
                    for section in ["Home", "Draw", "Away"]:
                        if section in ft and isinstance(ft[section], dict):
                            for book in ["Pinnacle", "bet365"]:
                                if book in ft[section]:
                                    val = ft[section][book]
                                    if val:
                                        odds_data.setdefault(section, {})[book] = str(val)
                except (json.JSONDecodeError, TypeError):
                    # JSON 解析失败，regex 兜底
                    for section in ["Home", "Draw", "Away"]:
                        for book in ["Pinnacle", "bet365"]:
                            pat = re.search(
                                section + r'[^}]*?' + book + r'[^0-9]*(\d+\.\d+)',
                                odds_str,
                            )
                            if pat:
                                odds_data.setdefault(section, {})[book] = pat.group(1)
        home_odds_d = odds_data.get("Home", {})
        draw_odds_d = odds_data.get("Draw", {})
        away_odds_d = odds_data.get("Away", {})
        if home_odds_d or draw_odds_d or away_odds_d:
            result["home_odds_djyy"] = float(
                home_odds_d.get("Pinnacle") or home_odds_d.get("bet365") or 0)
            result["draw_odds_djyy"] = float(
                draw_odds_d.get("Pinnacle") or draw_odds_d.get("bet365") or 0)
            result["away_odds_djyy"] = float(
                away_odds_d.get("Pinnacle") or away_odds_d.get("bet365") or 0)
            result["odds_source"] = "DJYY/Pinnacle"

        # DJYY 模型概率 (新版 JSON 字段)
        djyy_prob = m.get("djyy_model_prob")
        if isinstance(djyy_prob, dict) and djyy_prob.get("home") is not None:
            try:
                result["djyy_model_prob"] = {
                    "home": float(djyy_prob["home"]),
                    "draw": float(djyy_prob.get("draw") or 0),
                    "away": float(djyy_prob.get("away") or 0),
                }
            except (ValueError, TypeError):
                pass
        # top_scores / totals (新版 JSON 字段)
        if m.get("top_scores"):
            result["top_scores_djyy"] = m.get("top_scores")
        if m.get("totals"):
            result["totals_djyy"] = m.get("totals")

        # 角球
        hc = m.get("home_corners")
        ac = m.get("away_corners")
        if hc is not None and ac is not None:
            try:
                result["home_corners"] = int(hc)
                result["away_corners"] = int(ac)
            except (ValueError, TypeError):
                pass

        # 中文队名确认
        result["home_name_cn"] = m.get("home_name_cn", "")
        result["away_name_cn"] = m.get("away_name_cn", "")

        # 联赛
        league = m.get("league")
        if isinstance(league, dict):
            result["league_name_en"] = league.get("name", "")

        return result
