"""从 djyydata.com SSR 抓取比赛数据，写入 data/djyy_matches.json

GitHub Actions 定时运行，每天 2 次（UTC 02:00 / 08:00）
用法: python scripts/fetch_djyy_ssr.py
"""
import json
import re
import sys
import urllib.request
import ssl
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DJYY_URL = "https://djyydata.com/en/data/league-matrix.json"
OUTPUT = ROOT / "data" / "djyy_matches.json"

# Cloudflare SSL 兼容
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def fetch_rsc() -> str:
    """获取 djyydata.com SSR HTML，返回原始文本"""
    req = urllib.request.Request(
        DJYY_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; football-engine/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_matches(html: str) -> list[dict]:
    """从 RSC Flight Data 提取比赛对象（正则提取，避免嵌套JSON解析问题）"""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\\"(.*?)\\"\]\)', html)
    if not chunks:
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html)

    for chunk in chunks:
        if 'home_name' not in chunk:
            continue
        decoded = chunk.replace('\\"', '"').replace('\\n', '\n')
        matches = []

        for hm in re.finditer(r'"home_name":"([^"]+)"', decoded):
            home = hm.group(1)
            pos = hm.start()
            snippet = decoded[pos:pos + 6000]  # 扩大到 6000 以包含 odds_comparison

            away = re.search(r'"away_name":"([^"]+)"', snippet)
            home_cn = re.search(r'"home_name_cn":"([^"]+)"', snippet)
            away_cn = re.search(r'"away_name_cn":"([^"]+)"', snippet)
            status = re.search(r'"status":"([^"]+)"', snippet)
            league = re.search(r'"league":\{[^}]*"name":"([^"]+)"', snippet)
            fs_match_id = re.search(r'"fs_match_id":(\d+)', snippet)
            home_goals = re.search(r'"home_goals":"(\d+)"', snippet)
            away_goals = re.search(r'"away_goals":"(\d+)"', snippet)
            home_xg = re.search(r'"home_xg":"([\d.]+)"', snippet)
            away_xg = re.search(r'"away_xg":"([\d.]+)"', snippet)
            home_pre_xg = re.search(r'"home_prematch_xg":"([\d.]+)"', snippet)
            away_pre_xg = re.search(r'"away_prematch_xg":"([\d.]+)"', snippet)
            home_corners = re.search(r'"home_corners":"(-?\d+)"', snippet)
            away_corners = re.search(r'"away_corners":"(-?\d+)"', snippet)
            home_red = re.search(r'"home_red_cards":"(\d+)"', snippet)
            away_red = re.search(r'"away_red_cards":"(\d+)"', snippet)
            in_today = re.search(r'"in_today":(\d)', snippet)
            pt_value = re.search(r'"pt_value_bet":(\d+)', snippet)
            pt_home = re.search(r'"pt_home_advantage":(\d+)', snippet)
            # 提取 odds_comparison（双重转义的 JSON）
            odds_raw = re.search(r'"odds_comparison":"(\{.*?\})"', snippet)

            match = {
                "fs_match_id": int(fs_match_id.group(1)) if fs_match_id else None,
                "home_name": home,
                "away_name": away.group(1) if away else "",
                "home_name_cn": home_cn.group(1) if home_cn else "",
                "away_name_cn": away_cn.group(1) if away_cn else "",
                "status": status.group(1) if status else "",
                "league": league.group(1) if league else "",
                "home_goals": home_goals.group(1) if home_goals else "",
                "away_goals": away_goals.group(1) if away_goals else "",
                "home_xg": home_xg.group(1) if home_xg else "",
                "away_xg": away_xg.group(1) if away_xg else "",
                "home_prematch_xg": home_pre_xg.group(1) if home_pre_xg else "",
                "away_prematch_xg": away_pre_xg.group(1) if away_pre_xg else "",
                "home_corners": int(home_corners.group(1)) if home_corners else None,
                "away_corners": int(away_corners.group(1)) if away_corners else None,
                "home_red_cards": int(home_red.group(1)) if home_red else 0,
                "away_red_cards": int(away_red.group(1)) if away_red else 0,
                "in_today": bool(int(in_today.group(1))) if in_today else False,
                "pt_value_bet": int(pt_value.group(1)) if pt_value else 0,
                "pt_home_advantage": int(pt_home.group(1)) if pt_home else 0,
                "odds_comparison": odds_raw.group(1).replace('\\\\"', '"').replace('\\"', '"') if odds_raw else "",
            }
            matches.append(match)

        return matches

    return []


def main():
    print(f"[{datetime.now().isoformat()}] Fetching DJYY SSR data...")
    try:
        html = fetch_rsc()
        matches = extract_matches(html)
        if not matches:
            print("  ⚠ No matches extracted, keeping existing file")
            sys.exit(0)

        data = {
            "source": "djyydata.com SSR",
            "extracted_at": datetime.now().isoformat(),
            "total": len(matches),
            "matches": matches,
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"  ✓ {len(matches)} matches → {OUTPUT}")
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()