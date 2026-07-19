"""竞彩官方数据源 - 主数据源"""
import hashlib
import json
import time
from datetime import date, datetime

import requests

from .base import DataSource, Fixture, MatchResult, OddsSnapshot, ImportManifest


SPORTTERY_API = "https://webapi.sporttery.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://m.sporttery.cn/",
    "Accept": "application/json",
}


class SportterySource(DataSource):
    """竞彩官方 API"""

    @property
    def name(self) -> str:
        return "sporttery"

    @property
    def priority(self) -> int:
        return 1

    def _fetch_json(self, url: str, params: dict = None, retries: int = 3) -> dict:
        """带重试的 JSON 请求"""
        for attempt in range(retries):
            try:
                resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, json.JSONDecodeError) as e:
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        return {}

    def fetch_fixtures(self, target_date: date) -> list[Fixture]:
        """从竞彩官方获取赛程"""
        url = f"{SPORTTERY_API}/gateway/jc/football/getMatchCalculatorV1.qry"
        params = {"poolCode": "HAD,HHAD,TTG", "matchDay": target_date.isoformat()}

        try:
            data = self._fetch_json(url, params)
        except Exception:
            return []

        fixtures = []
        # 实际结构: value.matchInfoList[].subMatchList[] 才是比赛
        match_info_list = data.get("value", {}).get("matchInfoList", [])
        for day_group in match_info_list:
            sub_matches = day_group.get("subMatchList", [])
            for item in sub_matches:
                match_num = item.get("matchNumStr", "") or str(item.get("matchNum", ""))
                home = item.get("homeTeamAbbName", "") or item.get("homeTeamAllName", "")
                away = item.get("awayTeamAbbName", "") or item.get("awayTeamAllName", "")
                league = item.get("leagueAbbName", "") or item.get("leagueAllName", "")
                match_time = item.get("matchTime", "")
                match_date = item.get("matchDate", target_date.isoformat())
                kickoff = f"{match_date} {match_time}" if match_time else ""

                # 提取赔率
                had = item.get("had", {})
                hhad = item.get("hhad", {})

                # 让球数: goalLine 字段 (如 "+1", "-1")
                handicap_str = hhad.get("goalLine", "")
                handicap = None
                if handicap_str:
                    try:
                        handicap = float(handicap_str)
                    except (ValueError, TypeError):
                        pass

                fixture = Fixture(
                    match_id=f"{target_date.isoformat()}_{match_num}",
                    competition=league,
                    home_team=home,
                    away_team=away,
                    kickoff=kickoff,
                    home_odds=self._safe_float(had.get("h")),
                    draw_odds=self._safe_float(had.get("d")),
                    away_odds=self._safe_float(had.get("a")),
                    handicap=handicap,
                    handicap_home_odds=self._safe_float(hhad.get("h")),
                    handicap_draw_odds=self._safe_float(hhad.get("d")),
                    handicap_away_odds=self._safe_float(hhad.get("a")),
                    source=self.name,
                )
                fixtures.append(fixture)

        return fixtures

    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """获取比赛结果"""
        url = f"{SPORTTERY_API}/gateway/jc/football/getMatchResultV1.qry"
        params = {"matchDay": target_date.isoformat()}

        try:
            data = self._fetch_json(url, params)
        except Exception:
            return []

        results = []
        for item in data.get("value", {}).get("matchResultList", []):
            result = MatchResult(
                match_id=f"{target_date.isoformat()}_{item.get('matchNum', '')}",
                home_score=int(item.get("homeScore", 0)),
                away_score=int(item.get("awayScore", 0)),
                home_team=item.get("homeTeamName", ""),
                away_team=item.get("awayTeamName", ""),
                competition=item.get("leagueName", ""),
                match_date=target_date.isoformat(),
            )
            results.append(result)

        return results

    def fetch_odds_snapshot(self, target_date: date) -> list[OddsSnapshot]:
        """获取当前赔率快照"""
        fixtures = self.fetch_fixtures(target_date)
        now = datetime.now().isoformat()
        snapshots = []
        for f in fixtures:
            if f.home_odds and f.draw_odds and f.away_odds:
                snapshots.append(OddsSnapshot(
                    match_id=f.match_id,
                    timestamp=now,
                    home_odds=f.home_odds,
                    draw_odds=f.draw_odds,
                    away_odds=f.away_odds,
                    source=self.name,
                ))
        return snapshots

    @staticmethod
    def _safe_float(val) -> float | None:
        try:
            return float(val) if val else None
        except (ValueError, TypeError):
            return None
