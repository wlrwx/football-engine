"""ESPN 数据源 - 备用数据源"""
import time
from datetime import date, datetime

import requests

from .base import DataSource, Fixture, MatchResult

ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/soccer"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# ESPN 联赛 slug 映射
LEAGUE_SLUGS = {
    "eng.1": "英超",
    "esp.1": "西甲",
    "ita.1": "意甲",
    "ger.1": "德甲",
    "fra.1": "法甲",
    "uefa.champions": "欧冠",
    "uefa.europa": "欧联",
    "fifa.world": "世界杯",
}


class EspnSource(DataSource):
    """ESPN Scoreboard API - 备用数据源（无赔率）"""

    @property
    def name(self) -> str:
        return "espn"

    @property
    def priority(self) -> int:
        return 9  # 最终兜底

    def fetch_fixtures(self, target_date: date) -> list[Fixture]:
        """从 ESPN 获取赛程（无赔率）"""
        fixtures = []
        date_str = target_date.strftime("%Y%m%d")

        for slug, league_name in LEAGUE_SLUGS.items():
            url = f"{ESPN_API}/{slug}/scoreboard"
            params = {"dates": date_str}

            try:
                resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue

            for event in data.get("events", []):
                comps = event.get("competitions", [])
                if not comps:
                    continue
                comp = comps[0]
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue

                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})

                fixture = Fixture(
                    match_id=event.get("id", ""),
                    competition=league_name,
                    home_team=home.get("team", {}).get("displayName", ""),
                    away_team=away.get("team", {}).get("displayName", ""),
                    kickoff=event.get("date", ""),
                    source=self.name,
                )
                fixtures.append(fixture)

            time.sleep(0.3)  # 限速

        return fixtures

    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """从 ESPN 获取结果"""
        results = []
        date_str = target_date.strftime("%Y%m%d")

        for slug, league_name in LEAGUE_SLUGS.items():
            url = f"{ESPN_API}/{slug}/scoreboard"
            params = {"dates": date_str}

            try:
                resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue

            for event in data.get("events", []):
                if event.get("status", {}).get("type", {}).get("completed") is not True:
                    continue
                comps = event.get("competitions", [])
                if not comps:
                    continue
                comp = comps[0]
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue

                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})

                results.append(MatchResult(
                    match_id=event.get("id", ""),
                    home_score=int(home.get("score", 0)),
                    away_score=int(away.get("score", 0)),
                    home_team=home.get("team", {}).get("displayName", ""),
                    away_team=away.get("team", {}).get("displayName", ""),
                    competition=league_name,
                    match_date=target_date.isoformat(),
                ))

            time.sleep(0.3)

        return results
