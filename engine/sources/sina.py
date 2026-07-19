"""新浪竞彩数据源 - 海外IP可用的体彩赔率替代

API: mix.lottery.sina.com.cn/gateway/index/entry
参数: cat1=jczqMatches&gameTypes=spf&date=YYYY-MM-DD&isAll=1&dpc=1
返回: 完整竞彩赔率(spf/rqspf/bf/bqc/jq)，与体彩官方数据一致。

优势: 无WAF，海外IP(GitHub Actions)直接访问。
用途: 体彩webapi被EdgeOne拦截时的自动降级源。
"""
import json
from datetime import date, datetime
from typing import Optional

import requests

from .base import DataSource, Fixture, MatchResult, OddsSnapshot


API_URL = "https://mix.lottery.sina.com.cn/gateway/index/entry"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://lottery.sina.com.cn/",
}

_SESSION: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(HEADERS)
        _SESSION.trust_env = False  # 防macOS代理干扰
    return _SESSION


class SinaSource(DataSource):
    """新浪竞彩数据源

    提供与体彩官方完全一致的竞彩赔率数据:
    - spf: 胜平负
    - rqspf: 让球胜平负
    - bf: 比分
    - bqc: 半全场
    - jq: 总进球
    """

    @property
    def name(self) -> str:
        return "sina"

    @property
    def priority(self) -> int:
        return 2  # 体彩(1)之后，500万(3)之前

    def fetch_fixtures(self, target_date: date) -> list[Fixture]:
        """获取指定日期的竞彩足球赛程+赔率"""
        params = {
            "format": "json",
            "__caller__": "web",
            "__version__": "1.0.0",
            "__verno__": "1",
            "cat1": "jczqMatches",
            "gameTypes": "spf",
            "date": target_date.isoformat(),
            "isPrized": "",
            "isAll": "1",
            "dpc": "1",
        }

        resp = _get_session().get(API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        matches = data.get("result", {}).get("data", [])
        if not matches:
            return []

        fixtures = []
        for m in matches:
            fixture = self._parse_match(m)
            if fixture:
                fixtures.append(fixture)

        return fixtures

    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """获取已开奖的比赛结果"""
        params = {
            "format": "json",
            "__caller__": "web",
            "__version__": "1.0.0",
            "__verno__": "1",
            "cat1": "jczqMatches",
            "gameTypes": "spf",
            "date": target_date.isoformat(),
            "isPrized": "1",  # 只看已开奖
            "isAll": "1",
            "dpc": "1",
        }

        resp = _get_session().get(API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        matches = data.get("result", {}).get("data", [])
        results = []
        for m in matches:
            score1 = m.get("score1", "")
            score2 = m.get("score2", "")
            if score1 == "" or score2 == "":
                continue
            try:
                results.append(MatchResult(
                    match_id=m.get("matchNo", ""),
                    home_score=int(score1),
                    away_score=int(score2),
                    home_team=m.get("team1", ""),
                    away_team=m.get("team2", ""),
                    competition=m.get("league", ""),
                    match_date=target_date.isoformat(),
                ))
            except (ValueError, TypeError):
                continue

        return results

    def health_check(self) -> bool:
        """检查API可用性"""
        try:
            params = {
                "format": "json",
                "__caller__": "web",
                "__version__": "1.0.0",
                "__verno__": "1",
                "cat1": "syncClock",
                "t": str(int(datetime.now().timestamp() * 1000)),
            }
            resp = _get_session().get(API_URL, params=params, timeout=10)
            return resp.status_code == 200
        except Exception:
            return False

    def _parse_match(self, m: dict) -> Optional[Fixture]:
        """解析单场比赛数据"""
        match_no = m.get("matchNo", "")
        if not match_no:
            return None

        # 解析spf赔率: "2.44,3.40,2.35"
        spf = m.get("spf", "")
        home_odds = draw_odds = away_odds = None
        if spf:
            parts = spf.split(",")
            if len(parts) >= 3:
                try:
                    home_odds = float(parts[0])
                    draw_odds = float(parts[1])
                    away_odds = float(parts[2])
                except ValueError:
                    pass

        # 解析rqspf让球: "+1,1.45,4.25,4.90" 或 "-1,2.10,3.20,2.80"
        rqspf = m.get("rqspf", "")
        handicap = None
        h_home = h_draw = h_away = None
        if rqspf:
            parts = rqspf.split(",")
            if len(parts) >= 4:
                try:
                    handicap = float(parts[0])
                    h_home = float(parts[1])
                    h_draw = float(parts[2])
                    h_away = float(parts[3])
                except ValueError:
                    pass

        # 解析开赛时间
        kickoff = m.get("matchTimeFormat", "")
        if kickoff:
            # "2026-07-20 23:00:00" → ISO format
            kickoff = kickoff.replace(" ", "T")

        fixture = Fixture(
            match_id=match_no,
            competition=m.get("league", ""),
            home_team=m.get("team1", ""),
            away_team=m.get("team2", ""),
            kickoff=kickoff,
            home_odds=home_odds,
            draw_odds=draw_odds,
            away_odds=away_odds,
            handicap=handicap,
            handicap_home_odds=h_home,
            handicap_draw_odds=h_draw,
            handicap_away_odds=h_away,
            source="sina",
        )

        # 附加原始数据（比分/半全场/进球数赔率）
        fixture._raw_bf = m.get("bf", "")       # 比分赔率(31项)
        fixture._raw_bqc = m.get("bqc", "")     # 半全场赔率(9项)
        fixture._raw_jq = m.get("jq", "")       # 总进球赔率(8项: 0-7+)
        fixture._tiCaiId = m.get("tiCaiId", "") # 体彩matchId
        fixture._matchNoValue = m.get("matchNoValue", "")  # 数字场次号
        fixture._sell_status = m.get("showSellStatus", "")  # 销售状态

        return fixture

    def get_available_dates(self) -> list[str]:
        """获取有赛事的日期列表"""
        params = {
            "format": "json",
            "__caller__": "web",
            "__version__": "1.0.0",
            "__verno__": "1",
            "cat1": "jczqMatches",
            "gameTypes": "spf",
            "date": "",
            "isPrized": "",
            "isAll": "1",
            "dpc": "1",
        }
        try:
            resp = _get_session().get(API_URL, params=params, timeout=10)
            data = resp.json()
            return data.get("result", {}).get("dates", [])
        except Exception:
            return []
