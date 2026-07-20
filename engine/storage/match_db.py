"""比赛历史数据库 - SQLite持久化层

存储策略:
- match_history: 每场已结算比赛的完整记录(预测vs真实)
- team_season_stats: 每队赛季累计xG/胜率(从DJYY live逐步积累)
- league_baselines: 联赛场均参数(从DJYY league-matrix同步)

设计原则:
- 只存竞彩相关比赛(每天7-15场), 不全量拉
- 每次结算自动积累, 越用越准
- 单文件SQLite, 适合GitHub Actions无服务器环境
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path


class MatchDB:
    """比赛历史SQLite数据库"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS match_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT UNIQUE,
                date TEXT NOT NULL,
                league TEXT,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                -- 预测
                pred_home_prob REAL,
                pred_draw_prob REAL,
                pred_away_prob REAL,
                pred_home_xg REAL,
                pred_away_xg REAL,
                pred_top_score TEXT,
                -- 真实结果
                score_home INTEGER,
                score_away INTEGER,
                actual_home_xg REAL,
                actual_away_xg REAL,
                ht_home INTEGER,
                ht_away INTEGER,
                -- 各源Brier
                brier_model REAL,
                brier_market REAL,
                brier_djyy REAL,
                brier_final REAL,
                -- 元数据
                djyy_id INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS team_season_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL,
                league TEXT,
                season TEXT DEFAULT '2026',
                -- 累计统计
                matches_played INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                goals_for INTEGER DEFAULT 0,
                goals_against INTEGER DEFAULT 0,
                -- xG累计
                xg_for_sum REAL DEFAULT 0,
                xg_against_sum REAL DEFAULT 0,
                xg_matches INTEGER DEFAULT 0,
                -- 最近5场xG (JSON array)
                recent_xg TEXT DEFAULT '[]',
                -- 更新时间
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(team_name, league, season)
            );

            CREATE TABLE IF NOT EXISTS league_baselines (
                league_name TEXT PRIMARY KEY,
                league_id INTEGER,
                avg_goals REAL,
                home_win_rate REAL,
                draw_rate REAL,
                away_win_rate REAL,
                btts_rate REAL,
                over25_rate REAL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_match_date ON match_history(date);
            CREATE INDEX IF NOT EXISTS idx_match_league ON match_history(league);
            CREATE INDEX IF NOT EXISTS idx_team_name ON team_season_stats(team_name);

            CREATE TABLE IF NOT EXISTS player_xg_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name TEXT NOT NULL,
                team_name TEXT NOT NULL,
                league TEXT,
                match_date TEXT,
                -- 单场数据
                xg REAL,
                xgot REAL,
                rating REAL,
                position TEXT,
                minutes INTEGER,
                -- 累计
                season_xg_sum REAL DEFAULT 0,
                season_matches INTEGER DEFAULT 0,
                UNIQUE(player_name, team_name, match_date)
            );

            CREATE INDEX IF NOT EXISTS idx_player_team ON player_xg_history(team_name);
            CREATE INDEX IF NOT EXISTS idx_player_name ON player_xg_history(player_name);
        """)
        self.conn.commit()

    def record_match(self, data: dict):
        """记录一场已结算比赛"""
        self.conn.execute("""
            INSERT OR REPLACE INTO match_history
            (match_id, date, league, home_team, away_team,
             pred_home_prob, pred_draw_prob, pred_away_prob,
             pred_home_xg, pred_away_xg, pred_top_score,
             score_home, score_away, actual_home_xg, actual_away_xg,
             ht_home, ht_away,
             brier_model, brier_market, brier_djyy, brier_final,
             djyy_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("match_id"),
            data.get("date"),
            data.get("league"),
            data.get("home_team"),
            data.get("away_team"),
            data.get("pred_home_prob"),
            data.get("pred_draw_prob"),
            data.get("pred_away_prob"),
            data.get("pred_home_xg"),
            data.get("pred_away_xg"),
            json.dumps(data.get("pred_top_score", []), ensure_ascii=False),
            data.get("score_home"),
            data.get("score_away"),
            data.get("actual_home_xg"),
            data.get("actual_away_xg"),
            data.get("ht_home"),
            data.get("ht_away"),
            data.get("brier_model"),
            data.get("brier_market"),
            data.get("brier_djyy"),
            data.get("brier_final"),
            data.get("djyy_id"),
        ))
        self.conn.commit()

    def update_team_stats(self, team_name: str, league: str,
                          goals_for: int, goals_against: int,
                          xg_for: float | None = None,
                          xg_against: float | None = None):
        """更新球队赛季统计 (每场结算后调用)"""
        result = "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L"

        # 获取或创建记录
        row = self.conn.execute(
            "SELECT * FROM team_season_stats WHERE team_name=? AND league=?",
            (team_name, league)
        ).fetchone()

        if row:
            # 更新累计
            new_xg_list = json.loads(row["recent_xg"] or "[]")
            if xg_for is not None:
                new_xg_list.append(round(xg_for, 3))
                new_xg_list = new_xg_list[-5:]  # 只保留最近5场

            self.conn.execute("""
                UPDATE team_season_stats SET
                    matches_played = matches_played + 1,
                    wins = wins + ?,
                    draws = draws + ?,
                    losses = losses + ?,
                    goals_for = goals_for + ?,
                    goals_against = goals_against + ?,
                    xg_for_sum = xg_for_sum + ?,
                    xg_against_sum = xg_against_sum + ?,
                    xg_matches = xg_matches + ?,
                    recent_xg = ?,
                    updated_at = datetime('now')
                WHERE team_name=? AND league=?
            """, (
                1 if result == "W" else 0,
                1 if result == "D" else 0,
                1 if result == "L" else 0,
                goals_for, goals_against,
                xg_for or 0, xg_against or 0,
                1 if xg_for is not None else 0,
                json.dumps(new_xg_list),
                team_name, league,
            ))
        else:
            self.conn.execute("""
                INSERT INTO team_season_stats
                (team_name, league, matches_played, wins, draws, losses,
                 goals_for, goals_against, xg_for_sum, xg_against_sum,
                 xg_matches, recent_xg)
                VALUES (?,?,1,?,?,?,?,?,?,?,?,?)
            """, (
                team_name, league,
                1 if result == "W" else 0,
                1 if result == "D" else 0,
                1 if result == "L" else 0,
                goals_for, goals_against,
                xg_for or 0, xg_against or 0,
                1 if xg_for is not None else 0,
                json.dumps([round(xg_for, 3)] if xg_for else []),
            ))
        self.conn.commit()

    def get_team_xg(self, team_name: str, league: str = None) -> dict | None:
        """获取球队xG统计 (预测时调用)"""
        if league:
            row = self.conn.execute(
                "SELECT * FROM team_season_stats WHERE team_name=? AND league=?",
                (team_name, league)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM team_season_stats WHERE team_name=? ORDER BY xg_matches DESC LIMIT 1",
                (team_name,)
            ).fetchone()

        if not row or row["xg_matches"] == 0:
            return None

        return {
            "avg_xg_for": row["xg_for_sum"] / row["xg_matches"],
            "avg_xg_against": row["xg_against_sum"] / row["xg_matches"],
            "recent_xg": json.loads(row["recent_xg"] or "[]"),
            "matches": row["matches_played"],
            "win_rate": row["wins"] / max(1, row["matches_played"]),
        }

    def sync_league_baselines(self, leagues_data: list[dict]):
        """从DJYY league-matrix同步联赛基线"""
        for lg in leagues_data:
            self.conn.execute("""
                INSERT OR REPLACE INTO league_baselines
                (league_name, league_id, avg_goals, home_win_rate,
                 draw_rate, away_win_rate, btts_rate, over25_rate, updated_at)
                VALUES (?,?,?,?,?,?,?,?,datetime('now'))
            """, (
                lg.get("name_zh") or lg.get("name_en", ""),
                lg.get("id"),
                lg.get("avg_goals"),
                lg.get("home_win_rate"),
                lg.get("draw_rate"),
                lg.get("away_win_rate"),
                lg.get("btts_rate"),
                lg.get("over25_rate"),
            ))
        self.conn.commit()

    def get_league_baseline(self, league_name: str) -> dict | None:
        """获取联赛基线参数"""
        row = self.conn.execute(
            "SELECT * FROM league_baselines WHERE league_name=?",
            (league_name,)
        ).fetchone()
        return dict(row) if row else None

    def get_xg_calibration(self, league: str = None, limit: int = 100) -> dict:
        """xG校准: 预测xG vs 真实xG的偏差 (供优化器用)"""
        query = """
            SELECT pred_home_xg, pred_away_xg, actual_home_xg, actual_away_xg,
                   score_home, score_away, league
            FROM match_history
            WHERE actual_home_xg IS NOT NULL AND pred_home_xg IS NOT NULL
        """
        params = []
        if league:
            query += " AND league=?"
            params.append(league)
        query += " ORDER BY date DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        if not rows:
            return {"n": 0}

        pred_xg_sum = sum(r["pred_home_xg"] + r["pred_away_xg"] for r in rows)
        actual_xg_sum = sum(r["actual_home_xg"] + r["actual_away_xg"] for r in rows)
        actual_goals_sum = sum(r["score_home"] + r["score_away"] for r in rows)
        n = len(rows)

        return {
            "n": n,
            "avg_pred_total_xg": round(pred_xg_sum / n, 3),
            "avg_actual_total_xg": round(actual_xg_sum / n, 3),
            "avg_actual_goals": round(actual_goals_sum / n, 3),
            "xg_bias": round((pred_xg_sum - actual_xg_sum) / n, 3),  # >0=高估
            "xg_to_goals_ratio": round(actual_xg_sum / max(1, actual_goals_sum), 3),
        }

    def record_lineup_xg(self, team_name: str, league: str, match_date: str,
                         players: list[dict]):
        """存储赛后阵容球员xG数据 (结算时调用)

        players: [{name, position, xg, xgot, rating, minutes}]
        """
        for p in players:
            name = p.get("name") or p.get("name_zh")
            if not name:
                continue
            self.conn.execute("""
                INSERT OR REPLACE INTO player_xg_history
                (player_name, team_name, league, match_date, xg, xgot, rating, position, minutes)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                name, team_name, league, match_date,
                p.get("xg"), p.get("xgot"), p.get("rating"),
                p.get("position"), p.get("minutes"),
            ))
        self.conn.commit()

    def get_team_key_players(self, team_name: str, top_n: int = 5) -> list[dict]:
        """获取球队xG贡献最高的球员 (预测时参考)

        返回按场均xG排序的球员列表
        """
        rows = self.conn.execute("""
            SELECT player_name, position,
                   SUM(xg) as total_xg, COUNT(*) as matches,
                   SUM(xg) / COUNT(*) as avg_xg,
                   AVG(rating) as avg_rating
            FROM player_xg_history
            WHERE team_name = ? AND xg IS NOT NULL
            GROUP BY player_name
            ORDER BY avg_xg DESC
            LIMIT ?
        """, (team_name, top_n)).fetchall()

        return [
            {
                "name": r["player_name"],
                "position": r["position"],
                "avg_xg": round(r["avg_xg"], 3),
                "total_xg": round(r["total_xg"], 3),
                "matches": r["matches"],
                "avg_rating": round(r["avg_rating"], 2) if r["avg_rating"] else None,
            }
            for r in rows
        ]

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
