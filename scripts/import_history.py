"""导入 lottery-football 历史数据作为冷启动"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def import_from_lottery_football(csv_path: str):
    """
    从 lottery-football 的 historical_matches.csv 导入。
    原始格式: date,competition,home_team,away_team,home_score,away_score,...
    """
    src = Path(csv_path)
    if not src.exists():
        print(f"文件不存在: {csv_path}")
        sys.exit(1)

    out_dir = ROOT / "data" / "historical"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "matches.csv"

    count = 0
    with open(src, "r", encoding="utf-8") as fin, \
         open(out_path, "w", encoding="utf-8", newline="") as fout:

        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=[
            "date", "competition", "home_team", "away_team",
            "home_score", "away_score",
            "home_elo", "away_elo",
            "home_attack", "away_attack",
            "home_defense", "away_defense",
            "home_odds", "draw_odds", "away_odds", "handicap",
        ])
        writer.writeheader()

        for row in reader:
            # 跳过无结果的
            if not row.get("home_score") or not row.get("away_score"):
                continue

            writer.writerow({
                "date": row.get("date", ""),
                "competition": row.get("competition", row.get("league", "")),
                "home_team": row.get("home_team", ""),
                "away_team": row.get("away_team", ""),
                "home_score": row.get("home_score", ""),
                "away_score": row.get("away_score", ""),
                "home_elo": row.get("home_elo", "1500"),
                "away_elo": row.get("away_elo", "1500"),
                "home_attack": row.get("home_attack", "1.0"),
                "away_attack": row.get("away_attack", "1.0"),
                "home_defense": row.get("home_defense", "1.0"),
                "away_defense": row.get("away_defense", "1.0"),
                "home_odds": row.get("home_odds", ""),
                "draw_odds": row.get("draw_odds", ""),
                "away_odds": row.get("away_odds", ""),
                "handicap": row.get("handicap", ""),
            })
            count += 1

    print(f"✓ 导入 {count} 场历史比赛 → {out_path}")


def bootstrap_elo():
    """从历史数据初始化 Elo 评级"""
    from engine.learning.elo_updater import EloUpdater

    matches_path = ROOT / "data" / "historical" / "matches.csv"
    ratings_path = ROOT / "data" / "models" / "team_ratings.json"

    if not matches_path.exists():
        print("先运行导入: python scripts/import_history.py <csv_path>")
        sys.exit(1)

    elo = EloUpdater(ratings_path)

    with open(matches_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                elo.update(
                    row["home_team"], row["away_team"],
                    int(row["home_score"]), int(row["away_score"]),
                )
            except (ValueError, KeyError):
                continue

    elo.save()
    print(f"✓ Elo 初始化完成: {len(elo.ratings)} 支球队")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  导入: python scripts/import_history.py /path/to/historical_matches.csv")
        print("  Elo:  python scripts/import_history.py --elo")
        sys.exit(1)

    if sys.argv[1] == "--elo":
        bootstrap_elo()
    else:
        import_from_lottery_football(sys.argv[1])
