#!/usr/bin/env python3
"""NJStats prob 归档 → 模型可用特征（2026-10-13 新增）

## 为什么需要这个

`scripts/njstats_archive.py` 把 NJStats 的 `/prob/*` 端点逐日归档到
`data/state/njstats_archive/*_prob_*.json`。那些 JSON 里每条记录含：

- 主客队 **overall / home / away 三套完整统计**（ppg、进失球、半场、胜负平、
  领先落后分布——各 20+ 字段）
- **openingHome / openingDraw / openingAway 开盘赔率**
- drawRate、matchTime、联赛/赛季 ID

但归档只是"存下来"，不加工就用不上。而项目里正好缺两样东西：

1. **开盘赔率**：STATUS.md 记着「开盘列（PSH/PSD/PSA）样本为 0，
   带开盘列的旧格式文件又没有比分，要做开收盘对比必须先合并两套 schema」。
   NJStats 的 prob 端点直接给开盘价，且与赛果同源（同一批 matchId），
   **不需要合并 schema**。
2. **客队客场 / 主队主场的针对性统计**：我们的 Dixon-Coles 用全局
   attack/defense，没有主客分离。这里 home.home.* 和 away.away.* 正好是
   主场/客场口径。

## 输出

`data/state/njstats_features/{date}.json`，每条一行：

    {
      "match_id": "...", "date": "...", "league": "...",
      "home": "...", "away": "...",
      "kickoff": "...",
      "opening": {"home": 3.2, "draw": 3.6, "away": 2.05},
      "opening_fair": {"home": ..., "draw": ..., "away": ...},   # 去水
      "home_stats": {"ppg_all":..., "scored_avg_all":..., ...},
      "away_stats": {...},
      "draw_rate": 36
    }

## 用法

    python -m engine.sources.njstats_features              # 全量重建
    python -m engine.sources.njstats_features --date 2026-10-12

## 数据纪律

- **只读归档文件，不发网络请求**（归档由 njstats_archive.py 负责，额度有限）
- 开盘赔率缺失的比赛照样输出（opening=null），不丢记录
- 去水用等赔法（1/sum(1/odds)），与 engine/backtest/historical_replay.py 的
  `devig()` 口径一致——两处不一致会让"开盘 vs 收盘"的对比失真
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ARCHIVE = ROOT / "data" / "state" / "njstats_archive"
OUT = ROOT / "data" / "state" / "njstats_features"

# 我们关心的统计字段（从 prob 记录里按固定路径提取）
# 命名统一成 <指标>_<口径>：口径 all/home/away 三套
STAT_FIELDS = [
    ("ppg", "ppg"),
    ("scored_avg", "scoredAVG"),
    ("conceded_avg", "concededAVG"),
    ("scored_avg_ht", "scoredAVGHT"),
    ("conceded_avg_ht", "concededAVGHT"),
    ("win_pct", "winPct"),
    ("draw_pct", "drawPct"),
    ("lose_pct", "losePct"),
    ("matches_played", "matchesPlayed"),
]
# prob 记录里 stats 的三个口径键
SCOPES = ("all", "home", "away")


def devig(odds: dict) -> dict | None:
    """等赔法去水。口径与 engine/backtest/historical_replay.py 的 devig() 一致。"""
    try:
        h, d, a = float(odds["home"]), float(odds["draw"]), float(odds["away"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(h, d, a) <= 1.0:
        return None
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    if inv <= 0:
        return None
    return {"home": (1.0 / h) / inv, "draw": (1.0 / d) / inv, "away": (1.0 / a) / inv}


def extract_team(team: dict, scope: str) -> dict:
    """从 prob 记录的 home/away 块里按口径提取统计。

    prob 结构（实测）：`team['overall']` 是全局、`team['home']` 是主场、
    `team['away']` 是客场——**注意 all 口径的键名是 `overall` 而不是 `all`**。
    字段名带 `_all` / `_home` / `_away` 后缀，与 scope 对应。
    （踩过：按 scope 名直接索引 team['all'] 会静默取到空字典。）
    """
    block_key = {"all": "overall", "home": "home", "away": "away"}[scope]
    block = team.get(block_key) or {}
    suffix = {"all": "_all", "home": "_home", "away": "_away"}[scope]
    out = {}
    for out_name, base in STAT_FIELDS:
        v = block.get(f"{base}{suffix}")
        if v is not None:
            try:
                out[out_name] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def extract_record(r: dict, date_str: str) -> dict:
    """一条 prob 记录 → 特征行。"""
    opening = None
    try:
        oh, od, oa = r.get("openingHome"), r.get("openingDraw"), r.get("openingAway")
        if oh and od and oa:
            opening = {"home": float(oh), "draw": float(od), "away": float(oa)}
    except (TypeError, ValueError):
        opening = None

    return {
        "match_id": str(r.get("matchId", "")),
        "date": date_str,
        "league": r.get("leagueName", ""),
        "season": r.get("season", ""),
        "home": r.get("homeName", ""),
        "away": r.get("awayName", ""),
        "status": r.get("status", ""),
        "kickoff": r.get("matchTimeStr", ""),
        "opening": opening,
        "opening_fair": devig(opening) if opening else None,
        # 主队取主场口径 + 全局，客队取客场口径 + 全局
        "home_stats": {
            "home": extract_team(r.get("home") or {}, "home"),
            "all": extract_team(r.get("home") or {}, "all"),
        },
        "away_stats": {
            "away": extract_team(r.get("away") or {}, "away"),
            "all": extract_team(r.get("away") or {}, "all"),
        },
        "draw_rate": r.get("drawRate"),
        # 赛果（未完赛为 null）——用于事后校准
        "score": (
            [r.get("homeCount"), r.get("awayCount")]
            if r.get("homeCount") is not None else None
        ),
        "ht_score": (
            [r.get("homeHalfCount"), r.get("awayHalfCount")]
            if r.get("homeHalfCount") is not None else None
        ),
    }


def load_day(date_str: str) -> list[dict]:
    """读某一天所有 prob_* 归档，按 matchId 去重（8 个端点内容重复）。

    8 个端点（draw/over/under/btts/home/away/ht-goal/avg-goal）返回的
    记录集是同一批比赛（统计字段相同），差别只在端点语义。所以只取
    `prob_draw` 一个端点做主数据源，其余端点忽略——除非将来要存它们的
    专属字段（目前看没有，prob 记录不含端点相关值）。
    """
    rows: dict[str, dict] = {}
    # 主数据源：prob_draw（字段最全，实测 571 条/快照）
    files = sorted(ARCHIVE.glob(f"{date_str}_prob_draw*.json"))
    if not files:
        # 回退：任一 prob 端点
        files = sorted(ARCHIVE.glob(f"{date_str}_prob_*.json"))
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8")).get("data") or []
        except (json.JSONDecodeError, OSError):
            continue
        for r in data:
            mid = str(r.get("matchId", ""))
            if mid and mid not in rows:
                rows[mid] = extract_record(r, date_str)
    return list(rows.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NJStats prob 归档 → 模型特征")
    ap.add_argument("--date", default="", help="只处理某一天（YYYY-MM-DD）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.date:
        dates = [args.date]
    else:
        dates = sorted({
            f.name[:10] for f in ARCHIVE.glob("*_prob_*.json")
            if len(f.name) >= 10 and f.name[:4].isdigit()
        })

    if not dates:
        print("无 prob 归档可处理（先跑 scripts/njstats_archive.py）")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    total = 0
    for d in dates:
        rows = load_day(d)
        if not rows:
            continue
        with_opening = sum(1 for r in rows if r["opening"])
        with_stats = sum(
            1 for r in rows
            if r["home_stats"]["home"].get("ppg") and r["away_stats"]["away"].get("ppg")
        )
        dest = OUT / f"{d}.json"
        dest.write_text(json.dumps(
            {"date": d, "n": len(rows), "rows": rows}, ensure_ascii=False), encoding="utf-8")
        total += len(rows)
        if not args.quiet:
            print(f"{d}: {len(rows)} 场 | 开盘赔率 {with_opening} | 主客分离统计 {with_stats}")

    if not args.quiet:
        print(f"合计 {total} 条 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
