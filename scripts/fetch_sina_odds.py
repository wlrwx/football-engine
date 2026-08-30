"""从新浪彩票 API 抓取竞彩赔率数据（初始+即时+变化历史）

数据来源: alpha.lottery.sina.com.cn/gateway/index/entry
无需登录，GET 请求即可

输出: data/daily/{date}/odds_sina.json
  - 欧赔: 18 家公司，每家有初始赔率和即时赔率
  - 亚盘: 7 家公司
  - 大小球: 12 家公司
  - 欧赔变化历史: 时间序列
"""
import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent

GATEWAY = "https://alpha.lottery.sina.com.cn/gateway/index/entry"
SX = {
    "format": "json",
    "__caller__": "wap",
    "__version__": "1.0.0",
    "__verno__": "10000",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://lotto.sina.cn/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
}


def _fetch_json(cat1: str, **extra) -> dict | list | None:
    """调用新浪网关 API"""
    params = {**SX, "cat1": cat1, "dpc": "1", **extra}
    url = GATEWAY + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            result = data.get("result", {})
            if result.get("status", {}).get("code") == 0:
                return result.get("data")
            return None
    except Exception as e:
        print(f"    ⚠ {cat1} 请求失败: {e}")
        return None


def fetch_match_list(date_str: str) -> list[dict]:
    """获取某日竞彩比赛列表（带matchNo编号，用于和竞彩匹配）"""
    data = _fetch_json("footballMatchListJczq", date=date_str)
    if isinstance(data, list):
        return data
    # fallback: 如果竞足列表为空，用全部比赛
    data = _fetch_json("footballMatchListAll", date=date_str)
    if isinstance(data, list):
        return data
    return []


def fetch_euro_odds(match_id: str) -> list[dict]:
    """获取欧赔（18家公司，初始+即时）"""
    data = _fetch_json("footballMatchOddsEuro", matchId=match_id)
    if isinstance(data, list):
        return data
    return []


def fetch_euro_odds_change(match_id: str, company_id: str, offer_id: str) -> list[dict]:
    """获取欧赔变化历史"""
    data = _fetch_json("footballMatchOddsEuroChange", matchId=match_id, companyId=company_id, offerId=offer_id)
    if isinstance(data, list):
        return data
    return []


def fetch_asia_odds(match_id: str) -> list[dict]:
    """获取亚盘"""
    data = _fetch_json("footballMatchOddsAsia", matchId=match_id)
    if isinstance(data, list):
        return data
    return []


def fetch_totals_odds(match_id: str) -> list[dict]:
    """获取大小球"""
    data = _fetch_json("footballMatchOddsTotals", matchId=match_id)
    if isinstance(data, list):
        return data
    return []


def extract_odds_snapshot(match: dict, euro: list, asia: list, totals: list, changes: dict) -> dict:
    """提取赔率快照"""
    # 找 Bet365 (id=7) 或 Pinnacle (id=9) 或第一家公司
    bet365 = next((c for c in euro if c.get("companyId") == "7"), None)
    pinnacle = next((c for c in euro if c.get("companyId") == "9"), None)
    first = euro[0] if euro else None
    primary = bet365 or pinnacle or first

    result = {
        "match_id": match.get("matchId", ""),
        "match_no": match.get("matchNo", ""),  # 竞彩编号如"周五001"
        "home_team": match.get("team1", ""),
        "away_team": match.get("team2", ""),
        "league": match.get("league", ""),
        "match_time": match.get("matchTimeFormat", ""),
        "score": {"home": match.get("score1", ""), "away": match.get("score2", "")},
        "status": match.get("statusCn", ""),
    }

    if primary:
        result["euro"] = {
            "company": primary.get("companyName", ""),
            "initial": {
                "home": float(primary.get("o1Ini") or 0),
                "draw": float(primary.get("o2Ini") or 0),
                "away": float(primary.get("o3Ini") or 0),
            },
            "current": {
                "home": float(primary.get("o1New") or 0),
                "draw": float(primary.get("o2New") or 0),
                "away": float(primary.get("o3New") or 0),
            },
        }

        # 计算赔率变化方向
        ini = result["euro"]["initial"]
        cur = result["euro"]["current"]
        if ini["home"] > 0 and cur["home"] > 0:
            result["euro"]["movement"] = {
                "home": "up" if cur["home"] > ini["home"] else "down" if cur["home"] < ini["home"] else "flat",
                "draw": "up" if cur["draw"] > ini["draw"] else "down" if cur["draw"] < ini["draw"] else "flat",
                "away": "up" if cur["away"] > ini["away"] else "down" if cur["away"] < ini["away"] else "flat",
            }
            # 压缩比: 初始/即时, >1=赔率被压缩（资金涌入）, <1=赔率被抬高
            result["euro"]["compression"] = {
                "home": round(ini["home"] / cur["home"], 3) if cur["home"] > 0 else 1.0,
                "draw": round(ini["draw"] / cur["draw"], 3) if cur["draw"] > 0 else 1.0,
                "away": round(ini["away"] / cur["away"], 3) if cur["away"] > 0 else 1.0,
            }

    # 所有公司的赔率摘要
    result["all_euro"] = []
    for c in euro:
        result["all_euro"].append({
            "company": c.get("companyName", ""),
            "initial": [float(c.get("o1Ini") or 0), float(c.get("o2Ini") or 0), float(c.get("o3Ini") or 0)],
            "current": [float(c.get("o1New") or 0), float(c.get("o2New") or 0), float(c.get("o3New") or 0)],
        })

    # 亚盘摘要
    if asia:
        a = asia[0]
        result["asia"] = {
            "company": a.get("companyName", ""),
            "initial": {
                "home": float(a.get("o1Ini") or 0),
                "handicap": a.get("o3IniStr", ""),
                "away": float(a.get("o2Ini") or 0),
            },
            "current": {
                "home": float(a.get("o1New") or 0),
                "handicap": a.get("o3NewStr", ""),
                "away": float(a.get("o2New") or 0),
            },
        }

    # 大小球摘要
    if totals:
        t = totals[0]
        result["totals"] = {
            "company": t.get("companyName", ""),
            "initial": {
                "over": float(t.get("o1Ini") or 0),
                "line": t.get("o3IniStr", ""),
                "under": float(t.get("o2Ini") or 0),
            },
            "current": {
                "over": float(t.get("o1New") or 0),
                "line": t.get("o3NewStr", ""),
                "under": float(t.get("o2New") or 0),
            },
        }

    # 赔率变化历史（主要公司）
    if changes:
        result["odds_history"] = changes

    return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD")
    parser.add_argument("--output-dir", default=None, help="输出目录")
    args = parser.parse_args()

    if args.date:
        dates = [args.date]
    else:
        # 竞彩使用北京时间；GitHub Actions runner 是 UTC，直接用 datetime.now()
        # 会在北京时间 00:00-08:00 抓错日期（8/14 sina_odds 全缺的根因）。
        # 统一走 engine.beijing_time（导入失败时回退旧公式，不中断抓取）。
        try:
            sys.path.insert(0, str(ROOT))
            from engine.beijing_time import beijing_today
            today = beijing_today()
        except Exception:
            today = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")
        dates = [today]

    output_dir = Path(args.output_dir) if args.output_dir else None

    print(f"[fetch_sina_odds] Fetching odds for {len(dates)} date(s)...")

    for date_str in dates:
        print(f"  → fetching {date_str}...")
        matches = fetch_match_list(date_str)
        if not matches:
            print("    ⚠ 无比赛")
            continue

        print(f"    {len(matches)} matches found, filtering to competitive leagues...")

        # 只抓竞彩相关联赛的比赛（跳过小联赛减少 API 调用）
        competitive_leagues = {
            "英超", "英冠", "德甲", "德乙", "意甲", "意乙", "西甲", "西乙",
            "法甲", "法乙", "荷甲", "葡超", "比甲", "苏超", "土超",
            "日职", "日乙", "K联赛", "K2联赛", "澳超",
            "美职联", "巴甲", "阿甲", "智利甲", "哥伦甲", "墨甲", "墨西甲",
            "瑞典超", "挪超", "芬超", "丹超", "冰岛超",
            "欧冠", "欧联", "欧协联", "欧罗巴", "世预赛", "欧预赛",
            "中超", "韩K", "瑞典", "挪威", "芬兰", "丹麦",
        }
        filtered = []
        for m in matches:
            league = m.get("league", "")
            # 竞彩联赛或有大赔率的比赛
            if any(lg in league for lg in competitive_leagues):
                filtered.append(m)
            elif m.get("euroO1") and float(m.get("euroO1") or 0) > 1:
                # 有欧赔的也抓
                filtered.append(m)

        print(f"    {len(filtered)} matches after filtering")
        matches = filtered

        import concurrent.futures

        all_odds = []
        def process_match(m):
            mid = m.get("matchId", "")
            if not mid:
                return None
            euro = fetch_euro_odds(mid)
            changes = []
            if euro:
                c = euro[0]
                changes_raw = fetch_euro_odds_change(mid, c.get("companyId", ""), c.get("offerId", "1"))
                if changes_raw:
                    changes = [
                        {"time": datetime.fromtimestamp(int(r.get("oddsTime", 0))).strftime("%Y-%m-%d %H:%M"),
                         "home": float(r.get("o1") or 0), "draw": float(r.get("o2") or 0), "away": float(r.get("o3") or 0)}
                        for r in changes_raw
                    ]
            asia = fetch_asia_odds(mid)
            totals = fetch_totals_odds(mid)
            return extract_odds_snapshot(m, euro, asia, totals, changes)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(process_match, m): m for m in matches}
            for i, future in enumerate(concurrent.futures.as_completed(futures)):
                result = future.result()
                if result:
                    all_odds.append(result)
                if (i + 1) % 20 == 0:
                    print(f"    ... {i+1}/{len(matches)}")

        # 保存
        if output_dir is None:
            output_dir = ROOT / "data" / "daily" / date_str
        output_dir.mkdir(parents=True, exist_ok=True)

        output_file = output_dir / "odds_sina.json"
        output_file.write_text(json.dumps(all_odds, ensure_ascii=False, indent=2))
        print(f"  ✓ {date_str}: {len(all_odds)} matches with odds → {output_file}")

        # 时间序列累积（2026-08-05 盘口系统修复）：每次抓取把当前快照追加到
        # data/state/odds_series/<match_id>.jsonl，形成系统自己的水位时间序列。
        # 此前 odds_history 只靠接口自带快照，系统没有累积 → realtime 监控是死代码。
        # 30 分钟粒度 × 多次 run 累积后，可算"赛前资金流斜率"而非单次 initial→current。
        _series_dir = ROOT / "data" / "state" / "odds_series"
        _series_dir.mkdir(parents=True, exist_ok=True)
        _now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for snap in all_odds:
            _mid = snap.get("match_id", "")
            _cur = snap.get("euro", {}).get("current", {})
            if not _mid or not _cur.get("home"):
                continue
            _rec = {
                "ts": _now_ts,
                "match_no": snap.get("match_no", ""),
                "home_team": snap.get("home_team", ""),
                "away_team": snap.get("away_team", ""),
                "league": snap.get("league", ""),
                "match_time": snap.get("match_time", ""),
                "euro": {"home": _cur["home"], "draw": _cur.get("draw"), "away": _cur.get("away")},
                "asia": snap.get("asia", {}).get("current"),
                "totals": snap.get("totals", {}).get("current"),
            }
            # 2026-08-30: 文件名带销售日期前缀, 结构性杜绝跨周同编号场次复用同一文件
            # （此前 <sina_id>.jsonl 虽然唯一, 但读取端按"周日002"兜底时会命中上周文件）
            _path = _series_dir / f"{date_str}_{_mid}.jsonl"
            _lines = _path.read_text(encoding="utf-8").splitlines() if _path.exists() else []
            if _lines:
                try:
                    _last = json.loads(_lines[-1])
                    if (_last.get("euro", {}).get("home") == _rec["euro"]["home"]
                            and _last.get("euro", {}).get("draw") == _rec["euro"]["draw"]
                            and _last.get("euro", {}).get("away") == _rec["euro"]["away"]):
                        # 水位未变：更新时间戳表示仍在监控，不重复追加
                        _lines[-1] = json.dumps(_rec, ensure_ascii=False)
                        _path.write_text("\n".join(_lines) + "\n", encoding="utf-8")
                        continue
                except Exception:
                    pass
            with open(_path, "a", encoding="utf-8") as _f:
                _f.write(json.dumps(_rec, ensure_ascii=False) + "\n")
        print(f"  ✓ 水位时间序列累积: {len(all_odds)} 场已追加 → {_series_dir}")

    print(f"\n  ✓ Total: {len(all_odds)} matches saved")


if __name__ == "__main__":
    main()
