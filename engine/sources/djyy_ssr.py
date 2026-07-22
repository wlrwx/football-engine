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
from pathlib import Path


class DJYYSSRSource:
    """DJYY SSR 数据源 — 提供真实赛前 xG 和赔率"""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._matches: list[dict] = []
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
            except Exception:
                pass
        self._loaded = True

    @property
    def matches(self) -> list[dict]:
        self._load()
        return self._matches

    def enrich_prediction(self, home_team: str, away_team: str) -> dict:
        """用 DJYY 数据增强一场预测，返回 {prematch_xg, odds, cn_names}"""
        self._load()
        for m in self._matches:
            # 中文队名匹配
            if (m.get("home_name_cn") == home_team and
                    m.get("away_name_cn") == away_team):
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

        # 赔率 (bet365 + Pinnacle) — regex 提取（RSC 嵌套转义无法 json.loads）
        odds_str = m.get("odds_comparison", "")
        if odds_str and isinstance(odds_str, str) and len(odds_str) > 10:
            odds_data = {}
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
