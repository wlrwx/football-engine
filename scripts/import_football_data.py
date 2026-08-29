#!/usr/bin/env python3
"""football-data.co.uk 历史数据导入（2026-08-29）

下载各联赛近 N 个赛季的比分+赔率 CSV，计算联赛级平局率/胜平负分布，
用于：
  1. 重估 fusion.LEAGUE_DRAW_ANCHOR（现表是 2026-08-12 账本 n>=5 的临时值）
  2. 为 DC 模型 / league_params 提供真实先验
  3. RPS 等指标的长期基准

原始 CSV 可随时重抓 → 落 data/historical/football_data/（gitignore），
只提交汇总 JSON data/state/football_data_baselines.json。

用法: python3 scripts/import_football_data.py [--seasons 10] [--force]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "historical" / "football_data"
OUT_JSON = ROOT / "data" / "state" / "football_data_baselines.json"
BASE = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"

# 账本实际出现的联赛 → football-data 联赛代码（欧冠/欧罗巴/K1/沙职/杯赛不在覆盖内）
LEAGUE_MAP = {
    "英超": "E0", "英冠": "E1",
    "德甲": "D1", "德乙": "D2",
    "意甲": "I1",
    "法甲": "F1", "法乙": "F2",
    "西甲": "SP1",
    "荷甲": "N1", "荷乙": "N2",
    "葡超": "P1",
    "比甲": "B1",
    "土超": "T1",
    "希超": "G1",
    "苏超": "SC0",
    "挪超": "NOR",
    "瑞典超": "SWE", "瑞超": "SWE",
    "芬超": "FIN",
    "巴甲": "BRA",
    "美职联": "USA",
    "日职": "JPN", "日乙": "J2",
    "俄超": "RUS",
}

SEASON_CODES = ["1718", "1819", "1920", "2021", "2122", "2223", "2324", "2425", "2526", "2627"]

# /new/ 端点（单 CSV 全赛季，日历年赛程：北欧/美洲/亚洲/俄罗斯等）
NEW_BASE = "https://www.football-data.co.uk/new/{code}.csv"
NEW_FORMAT = {"NOR", "SWE", "FIN", "BRA", "USA", "JPN", "J2", "RUS",
              "ARG", "AUT", "DNK", "MEX", "SWZ", "POL", "ROU", "IRL", "CHN"}
RECENT_CUTOFF = "2023-07-01"  # recent3 统一按日期切

# 分隔符编码：football-data 部分赛季 CSV 是 latin-1 / GBK 混杂
ENCODINGS = ("utf-8-sig", "latin-1")


def _download(url: str, out: Path, text_check: str) -> list[dict] | None:
    """带重试的下载+缓存，返回 CSV 行。"""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                raw = resp.read()
            break
        except Exception as e:
            if attempt == 2:
                print(f"  ⚠ {out.stem} 下载失败: {e}")
                return None
            time.sleep(2)
    text = None
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None or text_check not in text:
        return None
    out.write_text(text, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def fetch_new_csv(code: str, force: bool) -> list[dict] | None:
    """/new/ 单文件全赛季格式（HG/AG/Res 列）→ 归一为 FTHG/FTAG/FTR。"""
    out = RAW_DIR / f"{code}_new.csv"
    if out.exists() and not force:
        rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig", errors="replace"))))
        if rows:
            return [_norm_new(r) for r in rows]
    rows = _download(NEW_BASE.format(code=code), out, "HG")
    return [_norm_new(r) for r in rows] if rows else None


def _norm_new(r: dict) -> dict:
    return {"Date": r.get("Date"), "FTHG": r.get("HG"), "FTAG": r.get("AG"),
            "FTR": r.get("Res"), "Season": r.get("Season")}


def fetch_csv(code: str, season: str, force: bool) -> list[dict] | None:
    """下载（或读缓存）一个联赛赛季 CSV，返回行列表。"""
    out = RAW_DIR / f"{code}_{season}.csv"
    if out.exists() and not force:
        text = out.read_text(encoding="utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        if rows:
            return rows
    return _download(BASE.format(season=season, code=code), out, "Date")


def parse_date(s: str) -> str | None:
    """football-data 日期格式 dd/mm/yy 或 dd/mm/yyyy → ISO。"""
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def summarize(rows: list[dict]) -> dict:
    """单联赛汇总：胜平负分布 + 平均进球（按场次，无水分）。"""
    n = h = d = a = goals = 0
    dates = []
    for r in rows:
        try:
            fthg, ftag = int(r.get("FTHG") or ""), int(r.get("FTAG") or "")
        except ValueError:
            continue
        if fthg is None or ftag is None or r.get("FTR") not in ("H", "D", "A"):
            continue
        n += 1
        goals += fthg + ftag
        h += r["FTR"] == "H"
        d += r["FTR"] == "D"
        a += r["FTR"] == "A"
        dt = parse_date(r.get("Date") or "")
        if dt:
            dates.append(dt)
    if not n:
        return {}
    return {
        "n": n,
        "home_rate": round(h / n, 4),
        "draw_rate": round(d / n, 4),
        "away_rate": round(a / n, 4),
        "avg_goals": round(goals / n, 3),
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, default=10, help="回溯赛季数（默认 10）")
    ap.add_argument("--force", action="store_true", help="忽略缓存强制重抓")
    args = ap.parse_args()
    seasons = SEASON_CODES[-args.seasons:]

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    league_rows: dict[str, list[dict]] = defaultdict(list)
    for code in sorted(set(LEAGUE_MAP.values())):
        if code in NEW_FORMAT:
            rows = fetch_new_csv(code, args.force) or []
            rows = [r for r in rows if str(r.get("Season") or "").isdigit()
                    and int(r["Season"]) >= 2017]
            league_rows[code].extend(rows)
            print(f"  {code}: {len(rows)} 场")
            continue
        for season in seasons:
            rows = fetch_csv(code, season, args.force)
            if rows:
                league_rows[code].extend(rows)
        n = len(league_rows.get(code, []))
        print(f"  {code}: {n} 场")

    # 联赛级汇总 + 近三赛季平局率（14475=2024-25 起算近三码）
    baselines: dict[str, dict] = {}
    for cn, code in LEAGUE_MAP.items():
        rows = league_rows.get(code, [])
        if not rows:
            continue
        s = summarize(rows)
        s_recent = summarize([r for r in rows
                              if (parse_date(r.get("Date") or "") or "") >= RECENT_CUTOFF])
        s["recent3"] = {k: s_recent[k] for k in ("n", "draw_rate") if k in s_recent}
        baselines[cn] = s

    # 锚定建议：锚 = 该联赛长期真实平局率（修正现表 0.30-0.55 的错误量级）
    proposal = {cn: v["draw_rate"] for cn, v in baselines.items() if v.get("draw_rate")}
    payload = {
        "generated": __import__("datetime").date.today().isoformat(),
        "source": "football-data.co.uk",
        "seasons": seasons,
        "leagues": baselines,
        "draw_anchor_proposal": dict(sorted(proposal.items(), key=lambda kv: -kv[1])),
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✅ {len(baselines)} 个联赛 → {OUT_JSON.relative_to(ROOT)}")
    for cn, dr in sorted(proposal.items(), key=lambda kv: -kv[1]):
        print(f"  {cn}: 平局率 {dr:.3f} (n={baselines[cn]['n']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
