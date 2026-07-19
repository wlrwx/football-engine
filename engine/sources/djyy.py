"""DJYY 数据源 - 第三方模型概率 + 多庄家赔率 + xG

来源: https://djyylive.com
托管: Cloudflare Pages（海外IP可访问，解决竞彩WAF问题）
数据: SportMonks + 自有 djyy-elo-model

核心API（无鉴权）:
- /api/leagues/fixtures?date_from=&date_to=&category=  赛程+xG+比分
- /api/match/{id}/comparison  模型概率+多庄家赔率+全市场
- /api/match/{id}/info  教练/伤停/裁判/天气/场地
- /api/match/{id}/team_form?limit=5  近期战绩
- /data/league-matrix.json  33联赛场均数据
"""
import json
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from .base import DataSource, Fixture, MatchResult, OddsSnapshot


DJYY_BASE = "https://djyylive.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class DJYYSource(DataSource):
    """DJYY 数据源

    优势:
    - 海外IP可访问（Cloudflare托管），GitHub Actions可用
    - 多庄家赔率（Pinnacle + bet365 + Unibet）
    - 第三方模型概率（djyy-elo-model）
    - xG 数据
    - 初盘/即时盘对比
    - 联赛场均统计（支持联赛独立参数）
    """

    def __init__(self):
        self._league_matrix: Optional[dict] = None

    @property
    def name(self) -> str:
        return "djyy"

    @property
    def priority(self) -> int:
        return 4  # 体彩(1) > 新浪(2) > 500万(3) > DJYY(4)

    def _get_json(self, path: str, params: dict = None, retries: int = 2) -> Optional[dict]:
        """通用 GET 请求"""
        url = f"{DJYY_BASE}{path}"
        for attempt in range(retries):
            try:
                resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, json.JSONDecodeError):
                if attempt == retries - 1:
                    return None
                time.sleep(1)
        return None

    def fetch_fixtures(self, target_date: date) -> list[Fixture]:
        """获取赛程 + xG + 赔率标记

        API: /api/leagues/fixtures?date_from=&date_to=&category=
        """
        date_str = target_date.isoformat()
        # 取前后一天确保覆盖时区差异
        date_from = (target_date - timedelta(days=1)).isoformat()
        date_to = (target_date + timedelta(days=1)).isoformat()

        data = self._get_json("/api/leagues/fixtures", params={
            "date_from": date_from,
            "date_to": date_to,
            "category": "tier1+euro+tier2+other+world",
        })

        if not data or not isinstance(data, list):
            return []

        fixtures = []
        for item in data:
            # 只取目标日期的比赛
            starting_at = item.get("starting_at", "")
            if not starting_at.startswith(date_str):
                # 跨日比赛：北京时间日期可能偏移
                pass  # 保留所有，下游按 kickoff 过滤

            fixture_id = item.get("id", "")
            league_info = item.get("league", {})
            home_info = item.get("home", {})
            away_info = item.get("away", {})
            score = item.get("score")
            xg = item.get("xg", {})
            rank = item.get("rank", {})

            fixture = Fixture(
                match_id=f"{date_str}_{fixture_id}",
                competition=league_info.get("name_zh", "") or league_info.get("name_en", ""),
                home_team=home_info.get("name_zh", "") or home_info.get("name_en", ""),
                away_team=away_info.get("name_zh", "") or away_info.get("name_en", ""),
                kickoff=starting_at,
                home_odds=None,  # 赔率在 comparison API 里
                draw_odds=None,
                away_odds=None,
                source=self.name,
            )

            # 附加数据
            fixture._djyy_id = fixture_id
            fixture._xg_home = xg.get("home") if xg else None
            fixture._xg_away = xg.get("away") if xg else None
            fixture._rank_home = rank.get("home") if rank else None
            fixture._rank_away = rank.get("away") if rank else None
            fixture._has_odds = item.get("has_odds", False)
            fixture._league_id = league_info.get("id")
            fixture._league_category = league_info.get("category", "")

            # 已完场的比赛直接有比分
            if score and score.get("status") == "完场":
                fixture._final_home = score.get("home")
                fixture._final_away = score.get("away")

            fixtures.append(fixture)

        return fixtures

    def fetch_match_comparison(self, djyy_id: int) -> Optional[dict]:
        """获取单场比赛的完整对比数据（模型+赔率+全市场）

        返回:
        - model: {p_home, p_draw, p_away, btts, totals, top_scores}
        - markets: [{key, model, bookmaker{Pinnacle/bet365/Unibet}}]
        - bookmaker: 汇总赔率
        - stakes: 动机/情境
        """
        return self._get_json(f"/api/match/{djyy_id}/comparison")

    def fetch_match_info(self, djyy_id: int) -> Optional[dict]:
        """获取比赛详情（教练、伤停、裁判、天气、场地）"""
        return self._get_json(f"/api/match/{djyy_id}/info")

    def fetch_team_form(self, djyy_id: int, limit: int = 5) -> Optional[dict]:
        """获取双方近期战绩"""
        return self._get_json(f"/api/match/{djyy_id}/team_form", params={
            "limit": limit, "v": 2,
        })

    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """从赛程API提取已完场比赛的结果"""
        fixtures = self.fetch_fixtures(target_date)
        results = []
        for f in fixtures:
            home_score = getattr(f, "_final_home", None)
            away_score = getattr(f, "_final_away", None)
            if home_score is not None and away_score is not None:
                results.append(MatchResult(
                    match_id=f.match_id,
                    home_score=int(home_score),
                    away_score=int(away_score),
                    home_team=f.home_team,
                    away_team=f.away_team,
                    competition=f.competition,
                    match_date=target_date.isoformat(),
                ))
        return results

    def fetch_odds_snapshot(self, target_date: date) -> list[OddsSnapshot]:
        """获取赔率快照（从 comparison API 提取 Pinnacle 赔率）"""
        fixtures = self.fetch_fixtures(target_date)
        now = datetime.now().isoformat()
        snapshots = []

        for f in fixtures:
            djyy_id = getattr(f, "_djyy_id", None)
            if not djyy_id or not getattr(f, "_has_odds", False):
                continue

            comparison = self.fetch_match_comparison(djyy_id)
            if not comparison:
                continue

            # 提取 Pinnacle 1X2 赔率
            bookmaker = comparison.get("bookmaker", {})
            odds_1x2 = bookmaker.get("1x2", {})
            home_p = odds_1x2.get("home")
            draw_p = odds_1x2.get("draw")
            away_p = odds_1x2.get("away")

            if home_p and draw_p and away_p:
                # 概率转赔率
                snapshots.append(OddsSnapshot(
                    match_id=f.match_id,
                    timestamp=now,
                    home_odds=round(1.0 / home_p, 3) if home_p > 0 else 0,
                    draw_odds=round(1.0 / draw_p, 3) if draw_p > 0 else 0,
                    away_odds=round(1.0 / away_p, 3) if away_p > 0 else 0,
                    source=f"{self.name}_pinnacle",
                ))

            time.sleep(0.2)  # 礼貌延迟

        return snapshots

    def fetch_league_matrix(self) -> dict:
        """获取33联赛场均数据（支持联赛独立参数）

        返回: {league_id: {avg_goals, avg_corners, ...}}
        """
        if self._league_matrix is not None:
            return self._league_matrix

        data = self._get_json("/data/league-matrix.json")
        self._league_matrix = data if data else {}
        return self._league_matrix

    def get_model_probabilities(self, djyy_id: int) -> Optional[dict]:
        """获取 DJYY 模型概率（作为 ensemble 第四路信号）

        Returns:
            {"p_home": float, "p_draw": float, "p_away": float,
             "btts_yes": float, "over25": float, "top_scores": [...]}
        """
        comparison = self.fetch_match_comparison(djyy_id)
        if not comparison:
            return None

        model = comparison.get("model", {})
        if not model:
            return None

        totals = model.get("totals", {})
        over25_pair = totals.get("2.5", [None, None])

        return {
            "p_home": model.get("p_home"),
            "p_draw": model.get("p_draw"),
            "p_away": model.get("p_away"),
            "btts_yes": model.get("btts", {}).get("yes"),
            "over25": over25_pair[1] if len(over25_pair) > 1 else None,
            "top_scores": model.get("top_scores", []),
            "source": model.get("source", "djyy-elo-model"),
            "as_of": model.get("as_of", ""),
        }

    def get_opening_vs_current(self, djyy_id: int) -> Optional[dict]:
        """获取初盘 vs 即时盘对比（赔率变动方向分析）

        Returns:
            {"opening": {h, d, a}, "current": {h, d, a}, "movement": {h, d, a}}
        """
        comparison = self.fetch_match_comparison(djyy_id)
        if not comparison:
            return None

        markets = comparison.get("markets", [])
        for market in markets:
            if market.get("key") == "1x2_fulltime":
                bm = market.get("bookmaker", {})
                if not bm:
                    return None
                current = bm.get("raw_odds", {})
                opening = bm.get("opening_raw_odds", {})
                if current and opening:
                    return {
                        "opening": opening,
                        "current": current,
                        "movement": {
                            "home": self._safe_float(current.get("home", 0)) - self._safe_float(opening.get("home", 0) or 0),
                            "draw": self._safe_float(current.get("draw", 0)) - self._safe_float(opening.get("draw", 0) or 0),
                            "away": self._safe_float(current.get("away", 0)) - self._safe_float(opening.get("away", 0) or 0),
                        },
                        "bookmaker": bm.get("name", "Pinnacle"),
                    }
        return None

    def health_check(self) -> bool:
        """检查 DJYY 是否可达"""
        try:
            resp = requests.get(f"{DJYY_BASE}/api/league/directory",
                                headers=HEADERS, timeout=10)
            return resp.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _safe_float(val) -> Optional[float]:
        try:
            return float(val) if val else None
        except (ValueError, TypeError):
            return None
