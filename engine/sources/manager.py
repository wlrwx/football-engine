from __future__ import annotations
"""数据源管理器 - fallback 链调度 + DJYY增强"""
import hashlib
import json
import time
from datetime import date, datetime
from pathlib import Path

from .base import DataSource, Fixture, MatchResult, ImportManifest
from .sporttery import SportterySource
from .sina import SinaSource
from .wancai500 import Wancai500Source
from .djyy import DJYYSource
from .espn import EspnSource
from .team_match import match_pair


# DJYY 增强是可选信号，不能因为第三方接口慢而拖垮整个预测流水线。
DJYY_ENRICH_TIME_BUDGET = 60.0  # 秒



def _extract_context(comparison: dict | None, info: dict | None,
                     lineups: dict | None) -> dict:
    """从 DJYY 已有/新增接口提取比赛情境特征（store-only，供日后验证）。

    - stakes: 动机/情境（comparison 已有，零成本）—— 平局的经典预测因子
    - referee/weather/coach: info 附带字段
    - lineup: 双方阵型 + 首发攻击手数（防缩阵型是平局信号）
    """
    ctx: dict = {}
    if comparison:
        stakes = comparison.get("stakes")
        if stakes:
            ctx["stakes"] = stakes
    if info:
        for k_in, k_out in (("referee", "referee"), ("weather", "weather"),
                            ("coach", "coach")):
            v = info.get(k_in)
            if v:
                ctx[k_out] = v
    if lineups and lineups.get("available"):
        for side in ("home", "away"):
            lu = lineups.get(side) or {}
            starting = lu.get("starting") or []
            ctx[f"{side}_formation"] = lu.get("formation") or lu.get("formation_zh")
            ctx[f"{side}_starters"] = len(starting)
            ctx[f"{side}_starting_attackers"] = sum(
                1 for p in starting
                if str(p.get("position", "")).upper().startswith(("F", "FW", "ST"))
                or "前锋" in str(p.get("position_zh", ""))
            )
    return {k: v for k, v in ctx.items() if v not in (None, "", 0) or k.endswith(("starters", "attackers"))}


class SourceManager:
    """管理多个数据源，按优先级 fallback

    优先级: 竞彩(1) > 新浪(2) > 500万(3) > DJYY(4) > ESPN(9)
    海外IP: 体彩被WAF拦→自动降级到新浪(数据一致)
    增强: 无论主源是谁，都尝试从DJYY获取模型概率+多庄家赔率+xG
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.sources: list[DataSource] = sorted(
            [SportterySource(), SinaSource(), Wancai500Source(), DJYYSource(), EspnSource()],
            key=lambda s: s.priority,
        )
        # DJYY 增强源（单独引用，用于enrichment）
        self._djyy = DJYYSource()

    def fetch_fixtures(self, target_date: date) -> tuple[list[Fixture], ImportManifest]:
        """按优先级尝试获取赛程，返回数据和导入清单"""
        for source in self.sources:
            try:
                fixtures = source.fetch_fixtures(target_date)
                if fixtures:
                    manifest = self._create_manifest(target_date, source, fixtures)
                    return fixtures, manifest
            except Exception as e:
                print(f"[WARN] 数据源 {source.name} 获取失败: {e}")
                continue

        raise RuntimeError(f"所有数据源均无法获取 {target_date} 的赛程数据")

    def fetch_merged_fixtures(self, target_date: date) -> tuple[list[Fixture], ImportManifest]:
        """三源融合获取（生产模式）

        数据流:
          live.500.com (GBK) → [num, fid, home, away, league]
          体彩 API → by_num 索引 (had/hhad/ttg/crs/hafu)
          odds.500.com → Bet365(cid=3) + 平博(cid=24)

        匹配键: 场次号 num（如"周日104"），不能用 fid（两套体系）
        融合优先级: 体彩 > Bet365 兜底
        """
        from .wancai500 import Wancai500Source, BOOKMAKER_CID, _safe_lt
        from .sporttery import SportterySource

        wancai = Wancai500Source()
        sporttery = SportterySource()

        # [1/3] live.500.com: 抓比赛列表
        print("  [融合1/3] live.500.com (GBK)...")
        live_list = wancai.fetch_live_list()
        if not live_list:
            # fallback 到普通模式
            return self.fetch_fixtures(target_date)

        # [2/3] 体彩官方: 一次性拉全部盘口，用场次号索引
        print("  [融合2/3] 体彩官方...")
        st_by_num: dict[str, dict] = {}
        st_fixtures = []
        try:
            st_fixtures = sporttery.fetch_fixtures(target_date)
            for sf in st_fixtures:
                # 从 match_id 提取场次号: "2026-07-20_周日104" → "周日104"
                num = sf.match_id.split("_", 1)[-1] if "_" in sf.match_id else ""
                if num:
                    st_by_num[num] = {
                        "had": {"h": sf.home_odds, "d": sf.draw_odds, "a": sf.away_odds},
                        "hhad": {
                            "goalLine": sf.handicap,
                            "h": sf.handicap_home_odds,
                            "d": sf.handicap_draw_odds,
                            "a": sf.handicap_away_odds,
                        },
                        "ttg": getattr(sf, "_raw_ttg", {}),
                        "crs": getattr(sf, "_raw_crs", {}),
                        "hafu": getattr(sf, "_raw_hafu", {}),
                        "kickoff": sf.kickoff,
                    }
            print(f"    体彩 {len(st_by_num)} 场已索引")
        except Exception as e:
            print(f"    体彩获取失败: {e}（尝试新浪降级）")
            # 降级到新浪（海外IP可用，数据与体彩一致）
            try:
                from .sina import SinaSource
                sina = SinaSource()
                sina_fixtures = sina.fetch_fixtures(target_date)
                for sf in sina_fixtures:
                    num = sf.match_id  # 新浪的match_id就是场次号如"周一201"
                    if num:
                        st_by_num[num] = {
                            "had": {"h": sf.home_odds, "d": sf.draw_odds, "a": sf.away_odds},
                            "hhad": {
                                "goalLine": sf.handicap,
                                "h": sf.handicap_home_odds,
                                "d": sf.handicap_draw_odds,
                                "a": sf.handicap_away_odds,
                            },
                            "ttg": {},  # 新浪无ttg，用jq代替
                            "crs": {},  # 新浪无crs，用bf代替
                            "hafu": {}, # 新浪无hafu，用bqc代替
                        }
                        # 附加新浪原始数据
                        st_by_num[num]["bf"] = getattr(sf, "_raw_bf", "")
                        st_by_num[num]["bqc"] = getattr(sf, "_raw_bqc", "")
                        st_by_num[num]["jq"] = getattr(sf, "_raw_jq", "")
                print(f"    新浪降级成功: {len(st_by_num)} 场已索引")
            except Exception as e2:
                print(f"    新浪也失败: {e2}（用Bet365兜底）")

        # [3/3] odds.500.com: 逐场抓机构赔率 + 融合
        print("  [融合3/3] odds.500.com (Bet365+平博)...")
        import time as _time
        from datetime import timedelta as _td
        fixtures = []

        # 以体彩官方在售为准（sporttery 是权威在售列表，500 页面可能滞后显示已结束场次）
        # 500.com 仅提供 Bet365/平博赔率补充
        if st_by_num and st_fixtures:
            _live_by_num = {m.get("num"): m for m in live_list}
            for sf in st_fixtures:
                num = sf.match_id.split("_", 1)[-1] if "_" in sf.match_id else ""
                m = _live_by_num.get(num)
                fid = m.get("fid", "") if m else ""
                # 体彩为主，Bet365/平博补充缺失赔率
                b_eu = wancai.fetch_500_odds(fid, "europe", BOOKMAKER_CID["bet365"]) if fid else []
                b_as = wancai.fetch_500_odds(fid, "asian", BOOKMAKER_CID["bet365"]) if fid else []
                p_eu = wancai.fetch_500_odds(fid, "europe", BOOKMAKER_CID["pinnacle"]) if fid else []
                p_as = wancai.fetch_500_odds(fid, "asian", BOOKMAKER_CID["pinnacle"]) if fid else []
                b365_had = {
                    "h": wancai._safe_float(_safe_lt(b_eu, 0)),
                    "d": wancai._safe_float(_safe_lt(b_eu, 1)),
                    "a": wancai._safe_float(_safe_lt(b_eu, 2)),
                }
                # 融合: 体彩优先，Bet365 兜底
                final_h = sf.home_odds or b365_had["h"]
                final_d = sf.draw_odds or b365_had["d"]
                final_a = sf.away_odds or b365_had["a"]
                st_goal = sf.handicap
                final_handicap = st_goal if st_goal else wancai._safe_float(_safe_lt(b_as, 1))

                fixture = Fixture(
                    match_id=sf.match_id,
                    competition=sf.competition,
                    home_team=sf.home_team,
                    away_team=sf.away_team,
                    kickoff=sf.kickoff,
                    home_odds=final_h,
                    draw_odds=final_d,
                    away_odds=final_a,
                    handicap=final_handicap,
                    handicap_home_odds=sf.handicap_home_odds or wancai._safe_float(_safe_lt(b_as, 0)),
                    # 2026-08-06 修复：合并层漏传让球平赔率（sporttery 采到的 hhad.d 被丢弃），
                    # 导致历史所有 predictions 的 handicap_draw_odds=None，让球平 EV 永远算不了。
                    handicap_draw_odds=sf.handicap_draw_odds or wancai._safe_float(_safe_lt(b_eu, 1)),
                    handicap_away_odds=sf.handicap_away_odds or wancai._safe_float(_safe_lt(b_as, 2)),
                    source="merged",
                )
                fixture._num = num
                fixture._fid = fid
                fixture._sporttery_had = {"h": sf.home_odds, "d": sf.draw_odds, "a": sf.away_odds}
                fixture._sporttery_hhad = {"goalLine": sf.handicap,
                                           "h": sf.handicap_home_odds, "d": sf.handicap_draw_odds,
                                           "a": sf.handicap_away_odds}
                fixture._sporttery_ttg = getattr(sf, "_raw_ttg", {})
                fixture._sporttery_crs = getattr(sf, "_raw_crs", {})
                fixture._sporttery_hafu = getattr(sf, "_raw_hafu", {})
                fixture._bet365 = {
                    "had": b365_had,
                    "hhad": {"goalLine": wancai._safe_float(_safe_lt(b_as, 1)),
                             "h": wancai._safe_float(_safe_lt(b_as, 0)),
                             "a": wancai._safe_float(_safe_lt(b_as, 2))},
                }
                fixture._pinnacle = {
                    "had": {"h": wancai._safe_float(_safe_lt(p_eu, 0)),
                            "d": wancai._safe_float(_safe_lt(p_eu, 1)),
                            "a": wancai._safe_float(_safe_lt(p_eu, 2))},
                    "hhad": {"goalLine": wancai._safe_float(_safe_lt(p_as, 1)),
                             "h": wancai._safe_float(_safe_lt(p_as, 0)),
                             "a": wancai._safe_float(_safe_lt(p_as, 2))},
                }
                fixtures.append(fixture)
                _time.sleep(0.2)  # 限速防封
            print(f"  ✓ 融合完成: {len(fixtures)} 场 (体彩在售为主)")
        else:
            # 降级: 体彩不可用时用 500 列表，但过滤已结束/异常场次
            print("  ⚠ 体彩列表为空，使用 500 列表（已过滤历史场次）")
            for m in live_list:
                fid = m["fid"]
                num = m["num"]

                # 从竞彩编号推断实际比赛日期（周X → 日期）
                _weekday_map = {'周一': 0, '周二': 1, '周三': 2, '周四': 3, '周五': 4, '周六': 5, '周日': 6}
                actual_date_str = target_date.isoformat()
                for wd_str, wd_num in _weekday_map.items():
                    if wd_str in num:
                        today_wd = target_date.weekday()
                        diff = (wd_num - today_wd) % 7
                        actual_date_str = (target_date + _td(days=diff)).isoformat()
                        break
                # 过滤: 推断日期已过（历史列表残留）或超过5天（编号推断错误）
                try:
                    _ad = datetime.strptime(actual_date_str, "%Y-%m-%d").date()
                except Exception:
                    _ad = target_date
                if _ad < target_date or _ad > target_date + _td(days=5):
                    print(f"    ⏭ 跳过历史/异常场次 {num} ({actual_date_str})")
                    continue

                # Bet365 欧赔 + 亚盘
                b_eu = wancai.fetch_500_odds(fid, "europe", BOOKMAKER_CID["bet365"])
                b_as = wancai.fetch_500_odds(fid, "asian", BOOKMAKER_CID["bet365"])
                # 平博 欧赔 + 亚盘
                p_eu = wancai.fetch_500_odds(fid, "europe", BOOKMAKER_CID["pinnacle"])
                p_as = wancai.fetch_500_odds(fid, "asian", BOOKMAKER_CID["pinnacle"])

                # 体彩数据（用 num 匹配，关键！）
                st_match = st_by_num.get(num, {})
                st_had = st_match.get("had", {})
                st_hhad = st_match.get("hhad", {})

                # Bet365 数据
                b365_had = {
                    "h": wancai._safe_float(_safe_lt(b_eu, 0)),
                    "d": wancai._safe_float(_safe_lt(b_eu, 1)),
                    "a": wancai._safe_float(_safe_lt(b_eu, 2)),
                }

                # 融合: 体彩优先，Bet365 兜底
                final_h = st_had.get("h") or b365_had["h"]
                final_d = st_had.get("d") or b365_had["d"]
                final_a = st_had.get("a") or b365_had["a"]

                # 让球盘同理
                st_goal = st_hhad.get("goalLine")
                final_handicap = st_goal if st_goal else wancai._safe_float(_safe_lt(b_as, 1))

                # 从竞彩编号推断实际比赛日期（周X → 日期）
                _weekday_map = {'周一': 0, '周二': 1, '周三': 2, '周四': 3, '周五': 4, '周六': 5, '周日': 6}
                actual_date_str = target_date.isoformat()
                for wd_str, wd_num in _weekday_map.items():
                    if wd_str in num:
                        today_wd = target_date.weekday()
                        diff = (wd_num - today_wd) % 7
                        from datetime import timedelta
                        actual_date_str = (target_date + timedelta(days=diff)).isoformat()
                        break

                fixture = Fixture(
                    match_id=f"{actual_date_str}_{num}",
                    competition=m.get("league", ""),
                    home_team=m.get("home", ""),
                    away_team=m.get("away", ""),
                    kickoff=m.get("kickoff", ""),
                    home_odds=final_h,
                    draw_odds=final_d,
                    away_odds=final_a,
                    handicap=final_handicap,
                    handicap_home_odds=st_hhad.get("h") or wancai._safe_float(_safe_lt(b_as, 0)),
                    handicap_away_odds=st_hhad.get("a") or wancai._safe_float(_safe_lt(b_as, 2)),
                    source="merged",
                )

                # 附加全部原始数据
                fixture._num = num
                fixture._fid = fid
                fixture._sporttery_had = st_had
                fixture._sporttery_hhad = st_hhad
                fixture._sporttery_ttg = st_match.get("ttg", {})
                fixture._sporttery_crs = st_match.get("crs", {})
                fixture._sporttery_hafu = st_match.get("hafu", {})
                fixture._bet365 = {
                    "had": b365_had,
                    "hhad": {"goalLine": wancai._safe_float(_safe_lt(b_as, 1)),
                             "h": wancai._safe_float(_safe_lt(b_as, 0)),
                             "a": wancai._safe_float(_safe_lt(b_as, 2))},
                }
                fixture._pinnacle = {
                    "had": {"h": wancai._safe_float(_safe_lt(p_eu, 0)),
                            "d": wancai._safe_float(_safe_lt(p_eu, 1)),
                            "a": wancai._safe_float(_safe_lt(p_eu, 2))},
                    "hhad": {"goalLine": wancai._safe_float(_safe_lt(p_as, 1)),
                             "h": wancai._safe_float(_safe_lt(p_as, 0)),
                             "a": wancai._safe_float(_safe_lt(p_as, 2))},
                }

                # 世界杯未开售标注
                if m.get("league") == "世界杯":
                    has_st = bool(st_had.get("h") or st_hhad.get("h"))
                    fixture._sporttery_status = "已开售" if has_st else "未开售(待售)"
                else:
                    fixture._sporttery_status = ""

                fixtures.append(fixture)
                _time.sleep(0.2)  # 限速防封

        manifest = self._create_manifest(target_date, wancai, fixtures)
        manifest.source = "merged(sporttery+500wan)"
        print(f"  ✓ 融合完成: {len(fixtures)} 场")
        return fixtures, manifest

    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """按优先级获取比赛结果"""
        for source in self.sources:
            try:
                results = source.fetch_results(target_date)
                if results:
                    return results
            except Exception:
                continue
        return []

    def health_check(self) -> dict[str, bool]:
        """检查所有数据源健康状态"""
        status = {}
        for source in self.sources:
            try:
                status[source.name] = source.health_check()
            except Exception:
                status[source.name] = False
        return status

    def enrich_from_djyy(self, fixtures: list[Fixture], target_date: date,
                         context_cache: dict | None = None) -> dict[str, dict]:
        """DJYY增强: 为每场比赛获取模型概率+多庄家赔率+xG

        无论主数据源是谁，都尝试从DJYY获取额外信号。
        返回: {match_id: {model_probs, pinnacle_odds, xg, opening_odds}}
        """
        enrichment: dict[str, dict] = {}
        deadline = time.monotonic() + DJYY_ENRICH_TIME_BUDGET

        try:
            djyy_fixtures = self._djyy.fetch_fixtures(target_date)
        except Exception:
            return enrichment

        if not djyy_fixtures:
            return enrichment

        # 建立队名→DJYY ID映射（多级匹配，解决跨源译名不一致）
        djyy_map: dict[str, int] = {}
        for df in djyy_fixtures:
            djyy_id = getattr(df, "_djyy_id", None)
            if djyy_id:
                # 用 "主队_vs_客队" 作为键
                key = f"{df.home_team}_vs_{df.away_team}"
                djyy_map[key] = djyy_id

        # 逐场匹配并获取 comparison
        for fixture in fixtures:
            if time.monotonic() > deadline:
                break

            key = f"{fixture.home_team}_vs_{fixture.away_team}"
            djyy_id = djyy_map.get(key)

            # 尝试多级匹配（队名可能有译名/缩写差异）
            if not djyy_id:
                for dk, dv in djyy_map.items():
                    dk_parts = dk.replace("_vs_", "|").split("|")
                    if len(dk_parts) == 2 and match_pair(
                        dk_parts[0], dk_parts[1],
                        fixture.home_team, fixture.away_team,
                    ):
                        djyy_id = dv
                        break

            if not djyy_id:
                continue

            try:
                if time.monotonic() > deadline:
                    break
                comparison = self._djyy.fetch_match_comparison(djyy_id)
                if not comparison:
                    continue

                model = comparison.get("model", {})
                bookmaker = comparison.get("bookmaker", {})

                # team_form: 提取近期真实xG均值 + 赛程密度
                form_xg = None
                rest_days = None
                if time.monotonic() <= deadline:
                    try:
                        form = self._djyy.fetch_team_form(djyy_id, limit=5)
                        if form and form.get("available"):
                            home_fixtures = form.get("home", {}).get("fixtures", [])
                            away_fixtures = form.get("away", {}).get("fixtures", [])
                            home_xgs = [fx.get("xg") for fx in home_fixtures if fx.get("xg")]
                            away_xgs = [fx.get("xg") for fx in away_fixtures if fx.get("xg")]
                            if home_xgs or away_xgs:
                                form_xg = {
                                    "home_avg": round(sum(home_xgs) / len(home_xgs), 3) if home_xgs else None,
                                    "away_avg": round(sum(away_xgs) / len(away_xgs), 3) if away_xgs else None,
                                    "home_n": len(home_xgs),
                                    "away_n": len(away_xgs),
                                }
                            # 赛程密度: 最近一场比赛距今天数
                            rest_days = {}
                            for side, fxs in [("home", home_fixtures), ("away", away_fixtures)]:
                                dates = [fx.get("date") or fx.get("played_at", "")[:10]
                                         for fx in fxs if fx.get("date") or fx.get("played_at")]
                                if dates:
                                    last = max(dates)
                                    try:
                                        last_dt = datetime.strptime(last, "%Y-%m-%d").date()
                                        rest_days[side] = (target_date - last_dt).days
                                    except (ValueError, TypeError):
                                        pass
                            if not rest_days:
                                rest_days = None
                    except Exception:
                        pass

                # 伤停: 提取缺阵球员 (影响攻击力评估)
                injuries = None
                if time.monotonic() <= deadline:
                    try:
                        info = self._djyy.fetch_match_info(djyy_id)
                        if info and info.get("available"):
                            inj_data = info.get("injuries") or {}
                            home_inj = inj_data.get("home", [])
                            away_inj = inj_data.get("away", [])
                            if home_inj or away_inj:
                                injuries = {
                                    "home_count": len(home_inj),
                                    "away_count": len(away_inj),
                                    # 前锋/中场缺阵影响更大
                                    "home_attackers": sum(
                                        1 for p in home_inj
                                        if p.get("position", "") in ("F", "M", "Forward", "Midfielder")
                                        or "前锋" in p.get("position_zh", "")
                                        or "中场" in p.get("position_zh", "")
                                    ),
                                    "away_attackers": sum(
                                        1 for p in away_inj
                                        if p.get("position", "") in ("F", "M", "Forward", "Midfielder")
                                        or "前锋" in p.get("position_zh", "")
                                        or "中场" in p.get("position_zh", "")
                                    ),
                                }
                    except Exception:
                        pass

                # 首发阵容（2026-08-30 新增, store-only）：阵型+首发攻击手数。
                # 每场每日只抓一次（context_cache 以 djyy_id 为键, 由 main 持久化）,
                # 失败也记 tried_at 防止每 30 分钟重试打爆接口。
                _lineups = None
                if time.monotonic() <= deadline:
                    _cc = context_cache if context_cache is not None else {}
                    _cached = _cc.get(str(djyy_id)) or {}
                    if "lineups" in _cached or "lineups_tried_at" in _cached:
                        _lineups = _cached.get("lineups")
                    else:
                        try:
                            _lu = self._djyy.fetch_match_lineups(djyy_id)
                            _lineups = _lu if _lu and _lu.get("available") else None
                        except Exception:
                            _lineups = None
                        from datetime import datetime as _dt
                        _cc[str(djyy_id)] = {
                            "lineups": _lineups,
                            "lineups_tried_at": _dt.now().isoformat(timespec="seconds"),
                        }

                enrichment[fixture.match_id] = {
                    "djyy_id": djyy_id,
                    "model_probs": {
                        "home": model.get("p_home"),
                        "draw": model.get("p_draw"),
                        "away": model.get("p_away"),
                    } if model else None,
                    "pinnacle_odds": bookmaker.get("1x2"),
                    "top_scores": model.get("top_scores", []) if model else [],
                    "btts": model.get("btts") if model else None,
                    "totals": model.get("totals") if model else None,
                    "form_xg": form_xg,
                    "rest_days": rest_days,
                    "injuries": injuries,
                    "context": _extract_context(comparison, info, _lineups),
                }
            except Exception:
                continue

        return enrichment

    def get_league_params(self) -> dict:
        """获取联赛场均数据（支持联赛独立参数）"""
        try:
            return self._djyy.fetch_league_matrix()
        except Exception:
            return {}

    def _create_manifest(
        self, target_date: date, source: DataSource, fixtures: list[Fixture]
    ) -> ImportManifest:
        """创建不可变导入清单"""
        content = json.dumps(
            [{"match_id": f.match_id, "home": f.home_team, "away": f.away_team}
             for f in fixtures],
            ensure_ascii=False,
            sort_keys=True,
        )
        sha = hashlib.sha256(content.encode()).hexdigest()

        manifest = ImportManifest(
            import_date=target_date.isoformat(),
            source=source.name,
            fixture_count=len(fixtures),
            sha256=sha,
            timestamp=datetime.now().isoformat(),
            fallback_used=source.priority > 1,
        )

        # 持久化清单
        manifest_dir = self.data_dir / "daily" / target_date.isoformat()
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "import_manifest.json"
        if not manifest_path.exists():
            manifest_path.write_text(
                json.dumps(manifest.__dict__, ensure_ascii=False, indent=2)
            )

        return manifest
