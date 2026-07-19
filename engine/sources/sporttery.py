"""竞彩官方数据源 - 核心权威数据（中国体育彩票）

关键坑（已踩）：
- 不要加 poolCode 参数！加了触发 403。只传 channel=c，一次性返回全部盘口。
- 使用桌面 Chrome UA + Referer sporttery.cn
- 响应结构: value → matchInfoList[按天] → subMatchList[每场]
- 匹配键用 matchNumStr（如"周日104"）
- 盘口: had(胜平负) / hhad(让球) / ttg(总进球) / crs(波胆) / hafu(半全场)
"""
import json
import time
from datetime import date, datetime
from typing import Optional

import requests

from .base import DataSource, Fixture, MatchResult, OddsSnapshot


SPORTTERY_API = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry"

# 桌面 Chrome UA — 不要用移动端，也不要加 poolCode
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.sporttery.cn/",
}

# 关闭 trust_env 解决 macOS 代理坑
_SESSION = requests.Session()
_SESSION.trust_env = False


class SportterySource(DataSource):
    """竞彩官方 API — 优先级最高的权威数据源"""

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
                resp = _SESSION.get(url, params=params, headers=HEADERS, timeout=15)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, json.JSONDecodeError):
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        return {}

    def fetch_fixtures(self, target_date: date) -> list[Fixture]:
        """获取竞彩赛程 + 全盘口

        只传 channel=c，不加 poolCode！
        一次性返回: had / hhad / ttg / crs / hafu
        """
        params = {"channel": "c"}  # 关键：只传 channel，千万别加 poolCode

        try:
            data = self._fetch_json(SPORTTERY_API, params)
        except Exception:
            return []

        fixtures = []
        match_info_list = data.get("value", {}).get("matchInfoList", [])

        for day_group in match_info_list:
            # matchInfoList 按天分组，每天下有 subMatchList
            sub_matches = day_group.get("subMatchList", [])
            for item in sub_matches:
                match_num = item.get("matchNumStr", "") or str(item.get("matchNum", ""))
                home = item.get("homeTeamAbbName", "") or item.get("homeTeamName", "")
                away = item.get("awayTeamAbbName", "") or item.get("awayTeamName", "")
                league = item.get("leagueAbbName", "") or item.get("leagueName", "")
                match_time = item.get("matchTime", "")
                match_date_str = item.get("matchDate", target_date.isoformat())
                kickoff = f"{match_date_str} {match_time}" if match_time else ""

                # 胜平负 (had): {h, d, a}
                had = item.get("had", {})
                # 让球盘 (hhad): {goalLine, h, d, a}
                hhad = item.get("hhad", {})

                handicap = self._safe_float(hhad.get("goalLine"))

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

                # 附加原始盘口（供下游模型使用）
                fixture._raw_ttg = item.get("ttg", {})    # 总进球 {s0..s7}
                fixture._raw_crs = item.get("crs", {})    # 波胆 {s00s00=0:0...}
                fixture._raw_hafu = item.get("hafu", {})  # 半全场 {aa, ah...}

                fixtures.append(fixture)

        return fixtures

    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """获取比赛结果"""
        url = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchResultV1.qry"
        params = {"channel": "c"}

        try:
            data = self._fetch_json(url, params)
        except Exception:
            return []

        results = []
        for item in data.get("value", {}).get("matchResultList", []):
            match_date_str = item.get("matchDate", "")
            if match_date_str and match_date_str != target_date.isoformat():
                continue

            match_num = item.get("matchNumStr", "") or str(item.get("matchNum", ""))
            results.append(MatchResult(
                match_id=f"{target_date.isoformat()}_{match_num}",
                home_score=self._safe_int(item.get("homeScore")),
                away_score=self._safe_int(item.get("awayScore")),
                home_team=item.get("homeTeamAbbName", "") or item.get("homeTeamName", ""),
                away_team=item.get("awayTeamAbbName", "") or item.get("awayTeamName", ""),
                competition=item.get("leagueAbbName", "") or item.get("leagueName", ""),
                match_date=target_date.isoformat(),
            ))

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

    def health_check(self) -> bool:
        """检查竞彩API是否可达"""
        try:
            data = self._fetch_json(SPORTTERY_API, {"channel": "c"}, retries=1)
            return "value" in data
        except Exception:
            return False

    @staticmethod
    def _safe_float(val) -> Optional[float]:
        try:
            return float(val) if val else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _safe_int(val) -> int:
        try:
            return int(val) if val is not None else 0
        except (ValueError, TypeError):
            return 0
