from __future__ import annotations
"""500万数据源 - 机构赔率（Bet365/平博）+ 实时列表

关键坑（已踩）：
- live.500.com 页面是 GBK 编码，不设 r.encoding='gbk' 会乱码
- 500万主站有 Tencent EdgeOne WAF，改用 odds.500.com 子域接口
- odds.php 需要 fid（从 live.500.com 抓取）
- 500万的 fid 与体彩的 matchId 是【两套不同体系】，跨源匹配用场次号 num
- 赔率返回格式: europe → [[h, d, a]], asian → [[home, goalLine, away]]
- cid=3 是 Bet365, cid=24 是平博(Pinnacle)
"""
import json
import re
import time
from datetime import date, datetime
from typing import Optional

import requests

from .base import DataSource, Fixture, MatchResult, OddsSnapshot


LIVE_URL = "https://live.500.com/"
ODDS_URL = "https://odds.500.com/json/odds.php"

# 三套请求头（不同域名不同 Referer）
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")

# live.500.com 用
H_LIVE = {
    "User-Agent": _UA,
    "Referer": "https://live.500.com/",
}

# odds.500.com 机构赔率用（多一个 XMLHttpRequest 头，伪装 AJAX）
H_500 = {
    "User-Agent": _UA,
    "Referer": "https://odds.500.com/",
    "X-Requested-With": "XMLHttpRequest",
}

# 使用 Session 并关闭 trust_env（解决 macOS 代理坑）
_SESSION = requests.Session()
_SESSION.trust_env = False  # 忽略系统代理，否则内网/代理环境会超时

# 从 live.500.com 提取场次信息的正则
# tr 属性: id="a{行id}" order="{星期}{场次}" gy="{联赛,主队,客队}" lid="{联赛id}" fid="{场次id}"
LIVE_ROW_RE = re.compile(
    r'<tr\s+id="a(\d+)"[^>]*order="(\d+)"[^>]*gy="([^"]*)"[^>]*'
    r'(?:yy="([^"]*)"[^>]*)?lid="(\d+)"[^>]*fid="(\d+)"',
    re.DOTALL,
)

# 备用正则（属性顺序可能变化）
LIVE_ROW_RE_ALT = re.compile(
    r'<tr[^>]*\bfid="(\d+)"[^>]*\border="(\d+)"[^>]*\bgy="([^"]*)"',
    re.DOTALL,
)

# 机构 cid 对照表
BOOKMAKER_CID = {
    "bet365": 3,
    "pinnacle": 24,   # 平博
    "william_hill": 4,
    "ladbrokes": 5,
    "interwetten": 12,
}

# 星期映射: order首位 → 中文（用于构造场次号）
WEEKDAY_MAP = {
    "1": "周一", "2": "周二", "3": "周三", "4": "周四",
    "5": "周五", "6": "周六", "7": "周日",
}


def _safe_lt(data, idx: int) -> str:
    """安全取值: 赔率返回格式 [[v0, v1, v2, ...]]"""
    if data and len(data) > 0 and len(data[0]) > idx:
        return str(data[0][idx])
    return ""


class Wancai500Source(DataSource):
    """500万机构赔率数据源

    数据流:
      live.500.com (GBK) → 抓 [num, fid, home, away, league]
      odds.500.com (fid+cid) → Bet365(cid=3) / 平博(cid=24) 欧赔+亚盘

    跨源匹配: 用场次号 num（如"周日104"）对齐体彩，不能用 fid（两套体系）
    融合优先级: 体彩 > Bet365 兜底
    """

    def __init__(self):
        self._match_cache: list[dict] = []

    @property
    def name(self) -> str:
        return "wancai500"

    @property
    def priority(self) -> int:
        return 3  # 体彩(1) > 新浪(2) > 500万(3)

    def fetch_live_list(self) -> list[dict]:
        """从 live.500.com 获取实时赛事列表

        返回: [{num, fid, order, league, home, away, lid}, ...]
        关键: 页面是 GBK 编码！
        """
        try:
            resp = _SESSION.get(LIVE_URL, headers=H_LIVE, timeout=15)
            resp.encoding = "gbk"  # 关键！不设会乱码
            html = resp.text
        except requests.RequestException:
            return []

        matches = []
        rows = LIVE_ROW_RE.findall(html)
        if not rows:
            rows_alt = LIVE_ROW_RE_ALT.findall(html)
            for row in rows_alt:
                fid, order, gy = row[0], row[1], row[2]
                parts = gy.split(",") if gy else []
                num = self._order_to_num(order)
                matches.append({
                    "num": num,
                    "fid": fid,
                    "order": order,
                    "league": parts[0] if len(parts) > 0 else "",
                    "home": parts[1] if len(parts) > 1 else "",
                    "away": parts[2] if len(parts) > 2 else "",
                })
        else:
            for row in rows:
                row_id, order, gy, yy, lid, fid = row
                parts = gy.split(",") if gy else []
                num = self._order_to_num(order)
                matches.append({
                    "num": num,
                    "fid": fid,
                    "order": order,
                    "league": parts[0] if len(parts) > 0 else "",
                    "home": parts[1] if len(parts) > 1 else "",
                    "away": parts[2] if len(parts) > 2 else "",
                    "lid": lid,
                })

        self._match_cache = matches
        return matches

    def fetch_500_odds(self, fid: str, odds_type: str = "europe",
                       cid: int = 3) -> Optional[list]:
        """获取指定场次的机构赔率

        Args:
            fid: 场次ID（从 live.500.com 获取）
            odds_type: "europe"=欧赔(胜平负), "asian"=亚盘(让球)
            cid: 机构ID（3=Bet365, 24=平博Pinnacle）

        Returns:
            赔率数据 [[h, d, a]] 或 [[home, goalLine, away]]，或 None
        """
        params = {
            "fid": fid,
            "cid": cid,
            "type": odds_type,
            "r": 1,
            "_": int(time.time() * 1000),  # 时间戳防缓存
        }

        try:
            resp = _SESSION.get(ODDS_URL, params=params, headers=H_500, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            # 返回格式通常是 [[v0, v1, v2, ...]] 或嵌套结构
            if isinstance(data, list) and len(data) > 0:
                return data
            elif isinstance(data, dict):
                # 某些情况返回 {"data": [[...]]}
                inner = data.get("data", data)
                if isinstance(inner, list):
                    return inner
            return data
        except (requests.RequestException, json.JSONDecodeError):
            return None

    def fetch_fixtures(self, target_date: date) -> list[Fixture]:
        """获取赛程 + 多机构赔率

        流程:
          1. live.500.com 获取列表（num + fid）
          2. 逐场拉取 Bet365(cid=3) + 平博(cid=24) 欧赔+亚盘
          3. 融合: Bet365优先（竞彩对齐在 manager 层做）
        """
        live_list = self.fetch_live_list()
        if not live_list:
            return []

        fixtures = []
        for m in live_list:
            fid = m["fid"]

            # Bet365 欧赔 + 亚盘
            b_eu = self.fetch_500_odds(fid, "europe", BOOKMAKER_CID["bet365"])
            b_as = self.fetch_500_odds(fid, "asian", BOOKMAKER_CID["bet365"])

            # 平博 欧赔 + 亚盘
            p_eu = self.fetch_500_odds(fid, "europe", BOOKMAKER_CID["pinnacle"])
            p_as = self.fetch_500_odds(fid, "asian", BOOKMAKER_CID["pinnacle"])

            # 解析 Bet365（主赔率源）
            home_odds = self._safe_float(_safe_lt(b_eu, 0))
            draw_odds = self._safe_float(_safe_lt(b_eu, 1))
            away_odds = self._safe_float(_safe_lt(b_eu, 2))

            # 亚盘: [[home, goalLine, away]]
            handicap = self._safe_float(_safe_lt(b_as, 1))
            hcap_home = self._safe_float(_safe_lt(b_as, 0))
            hcap_away = self._safe_float(_safe_lt(b_as, 2))

            fixture = Fixture(
                match_id=f"{target_date.isoformat()}_{m['num']}",
                competition=m.get("league", ""),
                home_team=m.get("home", ""),
                away_team=m.get("away", ""),
                kickoff="",
                home_odds=home_odds,
                draw_odds=draw_odds,
                away_odds=away_odds,
                handicap=handicap,
                handicap_home_odds=hcap_home,
                handicap_away_odds=hcap_away,
                source=self.name,
            )

            # 附加多机构数据
            fixture._num = m["num"]  # 场次号（跨源匹配键！）
            fixture._fid = fid
            fixture._bet365 = {
                "had": {"h": _safe_lt(b_eu, 0), "d": _safe_lt(b_eu, 1), "a": _safe_lt(b_eu, 2)},
                "hhad": {"goalLine": _safe_lt(b_as, 1), "h": _safe_lt(b_as, 0), "a": _safe_lt(b_as, 2)},
            }
            fixture._pinnacle = {
                "had": {"h": _safe_lt(p_eu, 0), "d": _safe_lt(p_eu, 1), "a": _safe_lt(p_eu, 2)},
                "hhad": {"goalLine": _safe_lt(p_as, 1), "h": _safe_lt(p_as, 0), "a": _safe_lt(p_as, 2)},
            }

            fixtures.append(fixture)
            time.sleep(0.2)  # 限速防封

        return fixtures

    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """500万不提供独立赛果接口，返回空（依赖竞彩或DJYY）"""
        return []

    def fetch_odds_snapshot(self, target_date: date) -> list[OddsSnapshot]:
        """获取当前机构赔率快照（Bet365 + Pinnacle 双源）"""
        fixtures = self.fetch_fixtures(target_date)
        now = datetime.now().isoformat()
        snapshots = []
        for f in fixtures:
            # Bet365 快照
            if f.home_odds and f.draw_odds and f.away_odds:
                snapshots.append(OddsSnapshot(
                    match_id=f.match_id,
                    timestamp=now,
                    home_odds=f.home_odds,
                    draw_odds=f.draw_odds,
                    away_odds=f.away_odds,
                    source=f"{self.name}_bet365",
                ))
            # Pinnacle 快照
            pin = getattr(f, "_pinnacle", {})
            pin_had = pin.get("had", {})
            ph = self._safe_float(pin_had.get("h"))
            pd = self._safe_float(pin_had.get("d"))
            pa = self._safe_float(pin_had.get("a"))
            if ph and pd and pa:
                snapshots.append(OddsSnapshot(
                    match_id=f.match_id,
                    timestamp=now,
                    home_odds=ph,
                    draw_odds=pd,
                    away_odds=pa,
                    source=f"{self.name}_pinnacle",
                ))
        return snapshots

    def health_check(self) -> bool:
        """检查 500万是否可达"""
        try:
            resp = _SESSION.get(LIVE_URL, headers=H_LIVE, timeout=10)
            resp.encoding = "gbk"
            return resp.status_code == 200 and "fid" in resp.text
        except Exception:
            return False

    @staticmethod
    def _order_to_num(order: str) -> str:
        """将 order 字段转为场次号（如 "7104" → "周日104"）

        order 首位是星期（1=周一...7=周日），后面是场次编号。
        """
        if not order or len(order) < 2:
            return order
        weekday_digit = order[0]
        match_num = order[1:]
        weekday_str = WEEKDAY_MAP.get(weekday_digit, f"周{weekday_digit}")
        return f"{weekday_str}{match_num}"

    @staticmethod
    def _safe_float(val) -> Optional[float]:
        try:
            v = float(val) if val is not None and val != "" else None
            return v if v is not None and v >= 0 else None
        except (ValueError, TypeError):
            return None
