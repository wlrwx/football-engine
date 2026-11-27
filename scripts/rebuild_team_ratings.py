#!/usr/bin/env python3
"""从 match_history.db 按时间顺序重算球队 Elo 与攻防评级。

用于修复 EloUpdater 攻防均值回归漏加 `(1 - decay) * 1.0` 后，
对比修复前后的攻防分布，并在显式传入 `--apply` 时安全重写评级文件。
默认 dry-run，不修改磁盘文件。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.learning.elo_updater import EloConfig, EloUpdater  # noqa: E402


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


def rebuild_ratings(
    db_path: Path,
    output_path: Path,
    apply: bool = False,
    xg_blend_weight: float = 0.0,
) -> dict:
    """从 SQLite 赛果表重放重建评级，返回前后摘要。"""
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

        rows = conn.execute(
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

    with tempfile.TemporaryDirectory(prefix="rebuild_elo_") as tmpdir:
        tmp_ratings = Path(tmpdir) / "team_ratings.json"
        updater = EloUpdater(tmp_ratings, EloConfig())
        for r in rows:
            lg = r["league"] or ""
            updater.update(
                home_team=r["home_team"],
                away_team=r["away_team"],
                home_score=int(r["score_home"]),
                away_score=int(r["score_away"]),
                match_date=r["date"],
                league_avg_goals=baselines.get(lg),
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
        "matches_replayed": len(rows),
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
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
