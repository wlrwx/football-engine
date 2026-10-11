"""从 djyylive.com API 抓取比赛数据（v2 - 2026-07-31 改版后）

DJYY 网站改版，不再用 SSR RSC Flight Data，改为公开 JSON API。
本脚本通过 /api/leagues/fixtures + /api/match/{id}/comparison 抓取：
  - 比赛列表（含队名、联赛、比分）
  - Pinnacle 赔率（胜平负 + BTTS + 大小球）
  - DJYY 模型概率（p_home/p_draw/p_away + 比分矩阵 + 大小球）
  - 角球、半全场等

用法: python scripts/fetch_djyy_ssr.py
输出: data/djyy_matches.json
"""
import os
import json
import sys
import urllib.parse
import urllib.request
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
BASE = os.environ.get("DJYY_PROXY", "https://djyylive.com").rstrip("/")
OUTPUT = ROOT / "data" / "djyy_matches.json"

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _fetch_json(url: str, timeout: int = 15) -> dict | list | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; football-engine/1.0)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  ✗ Failed: {e}", file=sys.stderr)
        return None


def fetch_fixtures(
    date_from: str,
    date_to: str,
    category: str = "tier1+euro+tier2+other+world",
) -> list[dict]:
    """抓取赛程（默认覆盖全部联赛分类，与 engine/sources/djyy.py 的 DJYYSource 一致）

    注意: category 值含 '+'，需用 quote 编码（'+' 在 query string 中表示空格）
    """
    url = (
        f"{BASE}/api/leagues/fixtures"
        f"?date_from={date_from}&date_to={date_to}"
        f"&category={urllib.parse.quote(category)}"
    )
    data = _fetch_json(url)
    if isinstance(data, list):
        return data
    return []


def fetch_comparison(match_id: int) -> dict | None:
    url = f"{BASE}/api/match/{match_id}/comparison"
    return _fetch_json(url, timeout=10)


def _safe(d: dict | None, key: str, default=None):
    """安全取值: d 或 d[key] 为 None 时返回 default"""
    if not isinstance(d, dict):
        return default
    v = d.get(key)
    return v if v is not None else default


def extract_match(fixture: dict, comp: dict | None) -> dict:
    m = {
        "fs_match_id": fixture.get("id"),
        "home_name": _safe(_safe(fixture, "home"), "name_en", ""),
        "away_name": _safe(_safe(fixture, "away"), "name_en", ""),
        "home_name_cn": _safe(_safe(fixture, "home"), "name_zh", ""),
        "away_name_cn": _safe(_safe(fixture, "away"), "name_zh", ""),
        "league": _safe(_safe(fixture, "league"), "name_en", ""),
        "league_zh": _safe(_safe(fixture, "league"), "name_zh", ""),
        "starting_at": fixture.get("starting_at", ""),
        "status": _safe(_safe(fixture, "score"), "status", ""),
        "home_goals": str(_safe(_safe(fixture, "score"), "home", "")),
        "away_goals": str(_safe(_safe(fixture, "score"), "away", "")),
        "has_odds": fixture.get("has_odds", False),
        # Defaults
        "home_odds_djyy": None,
        "draw_odds_djyy": None,
        "away_odds_djyy": None,
        "djyy_model_prob": None,
        "odds_source": None,
        "home_prematch_xg": None,
        "away_prematch_xg": None,
        "btts_yes_odds": None,
        "btts_no_odds": None,
        "over_25_odds": None,
        "under_25_odds": None,
        "top_scores": None,
        "totals": None,
    }

    if not comp:
        return m

    # Model probabilities
    model = comp.get("model") or {}
    if model.get("p_home") is not None:
        m["djyy_model_prob"] = {
            "home": model.get("p_home"),
            "draw": model.get("p_draw"),
            "away": model.get("p_away"),
        }
        m["top_scores"] = model.get("top_scores")
        m["totals"] = model.get("totals")

    # Markets → Pinnacle odds
    markets = comp.get("markets") or []
    for mk in markets:
        key = mk.get("key", "")
        bm = mk.get("bookmaker")
        if not bm:
            continue

        raw = bm.get("raw_odds") or {}

        if key == "1x2_fulltime":
            m["home_odds_djyy"] = raw.get("home")
            m["draw_odds_djyy"] = raw.get("draw")
            m["away_odds_djyy"] = raw.get("away")
            m["odds_source"] = f"DJYY/{bm.get('name', 'Pinnacle')}"

        elif key == "btts":
            m["btts_yes_odds"] = raw.get("yes")
            m["btts_no_odds"] = raw.get("no")

        elif key in ("total_over_under_2_5", "totals_2_5"):
            m["over_25_odds"] = raw.get("over")
            m["under_25_odds"] = raw.get("under")

    return m


def main():
    print("[fetch_djyy_v2] Fetching from djyylive.com API...")

    # 抓今明两天（UTC 日期，多抓一天覆盖时区差异）
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_to = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")

    fixtures = fetch_fixtures(today, date_to)
    print(f"  ✓ Fixtures: {len(fixtures)} matches")

    if not fixtures:
        print("  ⚠ No fixtures found, keeping existing file")
        return

    matches = []
    enriched = 0
    for i, fx in enumerate(fixtures):
        mid = fx.get("id")
        if not mid:
            continue

        comp = None
        try:
            comp = fetch_comparison(mid)
        except Exception:
            pass

        m = extract_match(fx, comp)
        matches.append(m)

        if m.get("home_odds_djyy"):
            enriched += 1

        if (i + 1) % 10 == 0:
            print(f"    ... {i+1}/{len(fixtures)} ({enriched} with odds)")

    output = {
        "source": "djyylive.com API v2",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "total": len(matches),
        "with_odds": enriched,
        "matches": matches,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"  ✓ {len(matches)} matches ({enriched} with Pinnacle odds) → {OUTPUT}")


if __name__ == "__main__":
    main()
