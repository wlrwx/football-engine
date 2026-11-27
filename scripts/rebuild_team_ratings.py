#!/usr/bin/env python3
"""从 match_history.db 按时间顺序重算球队 Elo 与攻防评级。

用于修复 EloUpdater 攻防均值回归漏加 `(1 - decay) * 1.0` 后，
对比修复前后的攻防分布，并在显式传入 `--apply` 时安全重写评级文件。
默认 dry-run，不修改磁盘文件。
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.learning.elo_updater import EloConfig, EloUpdater  # noqa: E402
from engine.prediction.league_stats import DEFAULT_AVG_GOALS, league_stats  # noqa: E402
from engine.team_aliases import normalize_team  # noqa: E402


def _summarize(ratings: dict[str, dict]) -> dict[str, float | int]:
    if not ratings:
        return {"count": 0}
    attacks = [float(v.get("attack", 1.0)) for v in ratings.values()]
    defenses = [float(v.get("defense", 1.0)) for v in ratings.values()]
    return {
        "count": len(ratings),
        "mean_attack": round(sum(attacks) / len(attacks), 4),
        "min_attack": round(min(attacks), 4),
        "max_attack": round(max(attacks), 4),
        "mean_defense": round(sum(defenses) / len(defenses), 4),
        "min_defense": round(min(defenses), 4),
        "max_defense": round(max(defenses), 4),
    }


def _collect_daily_results(daily_root: Path) -> list[dict]:
    """收集 data/daily/*/results.json 赛果，并从同目录 predictions.json 回查联赛名。"""
    rows: list[dict] = []
    if not daily_root.exists():
        return rows
    for rf in sorted(daily_root.glob("*/results.json")):
        day = rf.parent.name
        comp_by_mid: dict[str, str] = {}
        comp_by_pair: dict[tuple[str, str], str] = {}
        pf = rf.parent / "predictions.json"
        if pf.exists():
            try:
                praw = json.loads(pf.read_text(encoding="utf-8"))
                plist = praw.get("predictions", praw) if isinstance(praw, dict) else praw
                for p in plist or []:
                    comp = str(p.get("competition") or "").strip()
                    if not comp:
                        continue
                    if p.get("match_id"):
                        comp_by_mid[str(p["match_id"])] = comp
                    ht, at = p.get("home_team"), p.get("away_team")
                    if ht and at:
                        comp_by_pair[(normalize_team(ht), normalize_team(at))] = comp
            except (OSError, json.JSONDecodeError):
                pass
        try:
            rraw = json.loads(rf.read_text(encoding="utf-8"))
            rlist = rraw.get("results", rraw) if isinstance(rraw, dict) else rraw
        except (OSError, json.JSONDecodeError):
            continue
        for r in rlist or []:
            hs, as_ = r.get("home_score"), r.get("away_score")
            ht, at = r.get("home_team"), r.get("away_team")
            if hs is None or as_ is None or not ht or not at:
                continue
            mid = str(r.get("match_id") or "")
            comp = str(r.get("competition") or "").strip()
            if not comp:
                comp = comp_by_mid.get(mid) or comp_by_pair.get((normalize_team(ht), normalize_team(at)), "")
            try:
                rows.append({
                    "date": str(r.get("match_date") or day)[:10],
                    "league": comp,
                    "home_team": str(ht),
                    "away_team": str(at),
                    "score_home": int(hs),
                    "score_away": int(as_),
                    "actual_home_xg": None,
                    "actual_away_xg": None,
                    "prio": 2,
                })
            except (TypeError, ValueError):
                continue
    return rows


def _collect_csv_matches(historical_csv: Path) -> list[dict]:
    """收集 data/historical/matches.csv 冷启动历史赛果。"""
    rows: list[dict] = []
    if not historical_csv.exists():
        return rows
    with open(historical_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ht = str(r["home_team"]).strip()
                at = str(r["away_team"]).strip()
                dt = str(r["date"]).strip()[:10]
                hs = int(r["home_score"])
                as_ = int(r["away_score"])
            except (KeyError, TypeError, ValueError):
                continue
            if not ht or not at or not dt:
                continue
            rows.append({
                "date": dt,
                "league": str(r.get("competition") or "").strip(),
                "home_team": ht,
                "away_team": at,
                "score_home": hs,
                "score_away": as_,
                "actual_home_xg": None,
                "actual_away_xg": None,
                "prio": 1,
            })
    return rows


def rebuild_ratings(
    db_path: Path,
    output_path: Path,
    apply: bool = False,
    xg_blend_weight: float = 0.0,
    *,
    historical_csv: Path | None = None,
    daily_root: Path | None = None,
) -> dict:
    """按时间顺序合并历史 CSV / SQLite / 每日赛果并重放重建评级，返回前后摘要。"""
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    before_data: dict = {}
    if output_path.exists():
        before_data = json.loads(output_path.read_text(encoding="utf-8"))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        baselines: dict[str, float] = {}
        try:
            for row in conn.execute("SELECT league_name, avg_goals FROM league_baselines"):
                if row["league_name"] and row["avg_goals"]:
                    baselines[str(row["league_name"])] = float(row["avg_goals"])
        except sqlite3.Error:
            baselines = {}

        db_rows = conn.execute(
            """
            SELECT date, league, home_team, away_team,
                   score_home, score_away, actual_home_xg, actual_away_xg
            FROM match_history
            WHERE score_home IS NOT NULL AND score_away IS NOT NULL
            ORDER BY date ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    ordered: list[dict] = []
    db_keys: set[tuple[str, str, str]] = set()
    seq = 0
    for r in db_rows:
        dt = str(r["date"])[:10]
        k = (dt, normalize_team(r["home_team"]), normalize_team(r["away_team"]))
        db_keys.add(k)
        ordered.append({
            "date": dt,
            "league": str(r["league"] or "").strip(),
            "home_team": str(r["home_team"]),
            "away_team": str(r["away_team"]),
            "score_home": int(r["score_home"]),
            "score_away": int(r["score_away"]),
            "actual_home_xg": r["actual_home_xg"],
            "actual_away_xg": r["actual_away_xg"],
            "seq": seq,
        })
        seq += 1

    extra: dict[tuple[str, str, str], dict] = {}
    if historical_csv is not None:
        for item in _collect_csv_matches(historical_csv):
            k = (item["date"], normalize_team(item["home_team"]), normalize_team(item["away_team"]))
            if k in db_keys:
                continue
            item["seq"] = seq
            seq += 1
            extra[k] = item

    if daily_root is not None:
        for item in _collect_daily_results(daily_root):
            k = (item["date"], normalize_team(item["home_team"]), normalize_team(item["away_team"]))
            if k in db_keys:
                continue
            prev = extra.get(k)
            if prev is None:
                item["seq"] = seq
                seq += 1
                extra[k] = item
            else:
                if not item.get("league") and prev.get("league"):
                    item["league"] = prev["league"]
                item["seq"] = prev["seq"]
                extra[k] = item

    ordered.extend(extra.values())
    ordered = sorted(ordered, key=lambda x: (x["date"], x["seq"]))

    with tempfile.TemporaryDirectory(prefix="rebuild_elo_") as tmpdir:
        tmp_ratings = Path(tmpdir) / "team_ratings.json"
        updater = EloUpdater(tmp_ratings, EloConfig())
        for r in ordered:
            lg = r["league"]
            lg_avg = baselines.get(lg)
            if lg_avg is None and lg:
                cand = league_stats(lg)[0]
                if abs(cand - DEFAULT_AVG_GOALS) > 1e-6:
                    lg_avg = cand
            updater.update(
                home_team=r["home_team"],
                away_team=r["away_team"],
                home_score=r["score_home"],
                away_score=r["score_away"],
                match_date=r["date"],
                league_avg_goals=lg_avg,
                home_xg=r["actual_home_xg"],
                away_xg=r["actual_away_xg"],
                xg_blend_weight=xg_blend_weight,
            )

        rebuilt = {
            name: {
                "elo": round(rt.elo, 1),
                "attack": round(rt.attack, 4),
                "defense": round(rt.defense, 4),
                "form": round(rt.form, 4),
                "injury": rt.injury,
                "rest_days": rt.rest_days,
                "last_match_date": rt.last_match_date,
            }
            for name, rt in updater.ratings.items()
        }

        if apply:
            updater.ratings_path = output_path
            updater.save()

    return {
        "matches_replayed": len(ordered),
        "applied": apply,
        "before": _summarize(before_data),
        "after": _summarize(rebuilt),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重算 team_ratings.json")
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data" / "state" / "match_history.db",
        help="match_history.db 路径",
    )
    parser.add_argument(
        "--historical-csv",
        type=Path,
        default=ROOT / "data" / "historical" / "matches.csv",
        help="历史 CSV 路径",
    )
    parser.add_argument(
        "--daily-root",
        type=Path,
        default=ROOT / "data" / "daily",
        help="每日 results.json 根目录",
    )
    parser.add_argument(
        "--db-only",
        action="store_true",
        help="仅重放 match_history.db（不合并历史 CSV 与每日 results.json）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "models" / "team_ratings.json",
        help="输出 team_ratings.json 路径",
    )
    parser.add_argument(
        "--xg-blend",
        type=float,
        default=0.0,
        help="实测 xG 混合权重 [0, 1]",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="显式写入目标文件（默认仅 dry-run 打印对比）",
    )
    args = parser.parse_args(argv)
    summary = rebuild_ratings(
        db_path=args.db,
        output_path=args.output,
        apply=args.apply,
        xg_blend_weight=args.xg_blend,
        historical_csv=None if args.db_only else args.historical_csv,
        daily_root=None if args.db_only else args.daily_root,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
