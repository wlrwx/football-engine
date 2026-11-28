from __future__ import annotations
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

import os
import requests

from .base import DataSource, Fixture, MatchResult, OddsSnapshot


# CF Worker代理: 解决GH Actions海外IP被WAF拦截
# Worker路由: /api/sporttery/gateway/... → webapi.sporttery.cn/gateway/...
_PROXY = os.environ.get("SPORTTERY_PROXY", "")
if _PROXY:
    SPORTTERY_API = f"{_PROXY.rstrip('/')}/api/sporttery/gateway/uniform/football/getMatchCalculatorV1.qry"
else:
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

# 最后一次失败的可区分记录（2026-10-13，仿 engine/sources/djyy.py 的
# LAST_ERROR 模式）。背景：fetch_results 原来 `except Exception: return []`
# 连日志都没有——被 WAF 拦（403）、网络断、返回结构变更三种情况在下游
# 全部表现为"今天没有赛果"，结算静默跳过。与 DJYY 静默 3.5 周同病。
LAST_ERROR: dict = {}


def _record_error(kind: str, detail: str) -> None:
    LAST_ERROR.clear()
    LAST_ERROR.update({
        "kind": kind,
        "detail": detail,
        "at": datetime.now().isoformat(timespec="seconds"),
        "proxy_configured": bool(_PROXY),
    })


def _parse_single(item: dict, pool_dict: dict, pool_code: str) -> Optional[bool]:
    """从体彩官方盘口字典或 poolList 解析单关标记（1/true -> True, 0/false -> False）。"""
    raw = (pool_dict or {}).get("single")
    if raw is None:
        for entry in (item or {}).get("poolList") or []:
            if isinstance(entry, dict) and str(entry.get("poolCode") or "").upper() == pool_code:
                raw = entry.get("bettingSingle")
                if raw is None:
                    raw = entry.get("single")
                break
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no"):
        return False
    return None


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
                # 成功必须清掉旧错误（与 djyy._get_json 同口径）——
                # 否则一次历史故障会永久挂红，掩盖"现在已恢复"的事实
                LAST_ERROR.clear()
                return resp.json()
            except (requests.RequestException, json.JSONDecodeError) as e:
                print(f"    [sporttery] attempt {attempt+1}/{retries} failed: {type(e).__name__}: {e}")
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
        print(f"    [sporttery] URL: {SPORTTERY_API}")

        try:
            data = self._fetch_json(SPORTTERY_API, params)
        except Exception as e:
            # 2026-10-13: 与 fetch_results 同修——失败原因要可区分（WAF/代理/结构变更），
            # "今天确实没在售"是另一种情况（返回 200 + 空列表，不会走到这里）。
            kind = "blocked_by_waf" if isinstance(e, requests.HTTPError) else "fetch_failed"
            _record_error(kind, f"{type(e).__name__}: {e}")
            print(f"    [sporttery] ⚠ fetch_fixtures {kind}: {e}")
            return []

        fixtures = []
        match_info_list = data.get("value", {}).get("matchInfoList", [])

        _weekday_map = {'周一': 0, '周二': 1, '周三': 2, '周四': 3, '周五': 4, '周六': 5, '周日': 6}

        for day_group in match_info_list:
            # matchInfoList 按 businessDate（销售日）分组。
            # 竞彩编号"周X001"里的周X = 销售日星期，页面分组按它来（用户视角的比赛日）。
            # 以本组 businessDate 为基准推断编号日期，兼容多组响应，不依赖外部传入的 target_date。
            base_str = day_group.get("businessDate", "")
            base_date = None
            try:
                base_date = date.fromisoformat(base_str) if base_str else None
            except ValueError:
                base_date = None
            if base_date is None:
                base_date = target_date

            sub_matches = day_group.get("subMatchList", [])

            for item in sub_matches:
                match_num = item.get("matchNumStr", "") or str(item.get("matchNum", ""))
                home = item.get("homeTeamAbbName", "") or item.get("homeTeamName", "")
                away = item.get("awayTeamAbbName", "") or item.get("awayTeamName", "")
                league = item.get("leagueAbbName", "") or item.get("leagueName", "")
                match_time = item.get("matchTime", "")

                # 分组日期：从编号推断（周X → 基准周内的日期），作为 match_id 前缀
                match_date_str = base_date.isoformat()
                for wd_str, wd_num in _weekday_map.items():
                    if wd_str in match_num:
                        diff = (wd_num - base_date.weekday()) % 7
                        match_date_str = (base_date + __import__('datetime').timedelta(days=diff)).isoformat()
                        break

                # 开球时间：用接口返回的真实比赛日期 matchDate + matchTime
                # （matchDate 是真实开球日，可能与编号日相差1天：如"周一001"在周二 00:00 开球）
                real_date_str = item.get("matchDate", "") or match_date_str
                kickoff = f"{real_date_str} {match_time}" if match_time else ""

                # 胜平负 (had): {h, d, a}
                had = item.get("had", {})
                # 让球盘 (hhad): {goalLine, h, d, a}
                hhad = item.get("hhad", {})

                handicap = self._safe_float(hhad.get("goalLine"))

                # match_id 用从编号推断的实际比赛日期
                fixture = Fixture(
                    match_id=f"{match_date_str}_{match_num}",
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
                    had_single=_parse_single(item, had, "HAD"),
                    hhad_single=_parse_single(item, hhad, "HHAD"),
                )

                # 附加原始盘口（供下游模型使用）
                fixture._raw_ttg = item.get("ttg", {})    # 总进球 {s0..s7}
                fixture._raw_crs = item.get("crs", {})    # 波胆 {s00s00=0:0...}
                fixture._raw_hafu = item.get("hafu", {})  # 半全场 {aa, ah...}

                fixtures.append(fixture)

        return fixtures

    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """获取比赛结果"""
        if _PROXY:
            url = f"{_PROXY.rstrip('/')}/api/sporttery/gateway/uniform/football/getMatchResultV1.qry"
        else:
            url = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchResultV1.qry"
        params = {"channel": "c"}

        try:
            data = self._fetch_json(url, params)
        except Exception as e:
            # 2026-10-13: 原来这里 `except Exception: return []` 完全静默。
            # 被 WAF 拦（raise_for_status 的 HTTPError）、代理没配好、返回结构
            # 变更，三种病因在下游长得一模一样："今天没有赛果" → 结算静默跳过、
            # 账本不涨、也没人知道。记录病因 + 打日志，让"拿不到"和"没有赛果"可区分。
            kind = "blocked_by_waf" if isinstance(e, requests.HTTPError) else "fetch_failed"
            _record_error(kind, f"{type(e).__name__}: {e}")
            print(f"    [sporttery] ⚠ fetch_results {kind}: {e}"
                  + ("（403=被拦，不是没赛果）" if kind == "blocked_by_waf" else ""))
            return []

        results = []
        _weekday_map = {'周一': 0, '周二': 1, '周三': 2, '周四': 3, '周五': 4, '周六': 5, '周日': 6}
        for item in data.get("value", {}).get("matchResultList", []):
            match_num = item.get("matchNumStr", "") or str(item.get("matchNum", ""))
            
            # 从竞彩编号推断实际比赛日期
            match_date_str = item.get("matchDate", "")
            for wd_str, wd_num in _weekday_map.items():
                if wd_str in match_num:
                    today_wd = target_date.weekday()
                    diff = (wd_num - today_wd) % 7
                    actual_date = target_date + __import__('datetime').timedelta(days=diff)
                    match_date_str = actual_date.isoformat()
                    break
            if not match_date_str:
                match_date_str = target_date.isoformat()
            results.append(MatchResult(
                match_id=f"{match_date_str}_{match_num}",
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
