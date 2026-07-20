from __future__ import annotations
"""回测引擎 - 用历史数据验证系统是否有正期望

数据源:
  - data/daily/*/predictions.json (每日预测)
  - MatchDB match_history (实际结果)
  - review_ledger.jsonl (复盘记录, 含各源Brier)

输出:
  - 整体: 命中率, ROI, Brier, 校准误差
  - 分联赛: 各联赛表现
  - 滚动: 7日滚动命中率/ROI趋势
  - 策略对比: 不同置信阈值下的表现
"""
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BacktestMatch:
    """单场回测记录"""
    date: str
    match_id: str
    league: str
    home_team: str
    away_team: str
    pred_probs: tuple  # (home, draw, away)
    confidence: float
    actual_outcome: str  # "home" / "draw" / "away"
    score: tuple  # (home, away)
    best_pick: str
    best_prob: float
    odds: float | None
    hit: bool
    pnl: float = 0.0


@dataclass
class BacktestReport:
    """回测报告"""
    n_matches: int = 0
    n_days: int = 0
    hit_rate: float = 0.0
    avg_brier: float = 0.0
    roi: float = 0.0
    total_staked: float = 0.0
    total_pnl: float = 0.0
    by_league: dict = field(default_factory=dict)
    by_confidence: dict = field(default_factory=dict)
    rolling_7d: list = field(default_factory=list)
    calibration: dict = field(default_factory=dict)
    source_comparison: dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"{'='*50}",
            f"  回测报告: {self.n_days}天 / {self.n_matches}场",
            f"{'='*50}",
            f"  命中率: {self.hit_rate:.1%} ({int(self.hit_rate * self.n_matches)}/{self.n_matches})",
            f"  Brier:  {self.avg_brier:.4f} (越低越好, <0.22=优秀)",
            f"  ROI:    {self.roi:+.2%}",
            f"  总投入: {self.total_staked:.0f}元, 盈亏: {self.total_pnl:+.1f}元",
            f"",
            f"  分联赛:",
        ]
        for lg, stats in sorted(self.by_league.items(), key=lambda x: -x[1]["n"]):
            lines.append(
                f"    {lg}: {stats['n']}场, 命中{stats['hit_rate']:.0%}, "
                f"ROI={stats['roi']:+.1%}, Brier={stats['brier']:.3f}"
            )
        lines.append("")
        lines.append("  置信度分层:")
        for band, stats in sorted(self.by_confidence.items()):
            lines.append(
                f"    {band}: {stats['n']}场, 命中{stats['hit_rate']:.0%}, "
                f"ROI={stats['roi']:+.1%}"
            )
        if self.source_comparison:
            lines.append("")
            lines.append("  各源Brier对比:")
            for src, b in self.source_comparison.items():
                lines.append(f"    {src}: {b:.4f}")
        lines.append(f"{'='*50}")
        return "\n".join(lines)


class BacktestRunner:
    """回测引擎"""

    def __init__(self, data_dir: Path, db_path: Path = None):
        self.data_dir = data_dir
        self.daily_dir = data_dir / "daily"
        self.db_path = db_path or (data_dir / "state" / "match_history.db")

    def run(self, min_confidence: float = 0.0, kelly_fraction: float = 0.25) -> BacktestReport:
        """执行完整回测"""
        matches = self._load_matches()
        if not matches:
            return BacktestReport()

        if min_confidence > 0:
            matches = [m for m in matches if m.confidence >= min_confidence]

        report = BacktestReport()
        report.n_matches = len(matches)
        report.n_days = len(set(m.date for m in matches))

        # 命中率
        hits = sum(1 for m in matches if m.hit)
        report.hit_rate = hits / len(matches)

        # Brier
        briers = [self._brier(m) for m in matches]
        report.avg_brier = sum(briers) / len(briers)

        # ROI (Quarter-Kelly模拟)
        total_staked = 0.0
        total_pnl = 0.0
        for m in matches:
            if m.odds and m.best_prob * m.odds > 1.0:
                kelly_f = (m.best_prob * m.odds - 1) / (m.odds - 1) * kelly_fraction
                stake = max(1.0, min(100.0, kelly_f * 10000))
                total_staked += stake
                if m.hit:
                    total_pnl += stake * (m.odds - 1)
                    m.pnl = stake * (m.odds - 1)
                else:
                    total_pnl -= stake
                    m.pnl = -stake

        report.total_staked = total_staked
        report.total_pnl = total_pnl
        report.roi = total_pnl / total_staked if total_staked > 0 else 0

        # 分联赛
        league_groups = defaultdict(list)
        for m in matches:
            league_groups[m.league or "unknown"].append(m)
        for lg, ms in league_groups.items():
            lg_hits = sum(1 for m in ms if m.hit)
            lg_briers = [self._brier(m) for m in ms]
            lg_pnl = sum(m.pnl for m in ms)
            lg_staked = sum(
                max(1, min(100, ((m.best_prob * m.odds - 1) / (m.odds - 1) * kelly_fraction * 10000)))
                for m in ms if m.odds and m.best_prob * m.odds > 1.0
            )
            report.by_league[lg] = {
                "n": len(ms),
                "hit_rate": lg_hits / len(ms),
                "brier": sum(lg_briers) / len(lg_briers),
                "roi": lg_pnl / lg_staked if lg_staked > 0 else 0,
            }

        # 分置信度
        conf_bands = {"high(>0.6)": [], "mid(0.4-0.6)": [], "low(<0.4)": []}
        for m in matches:
            if m.best_prob >= 0.6:
                conf_bands["high(>0.6)"].append(m)
            elif m.best_prob >= 0.4:
                conf_bands["mid(0.4-0.6)"].append(m)
            else:
                conf_bands["low(<0.4)"].append(m)
        for band, ms in conf_bands.items():
            if not ms:
                continue
            band_hits = sum(1 for m in ms if m.hit)
            band_pnl = sum(m.pnl for m in ms)
            band_staked = sum(
                max(1, min(100, ((m.best_prob * m.odds - 1) / (m.odds - 1) * kelly_fraction * 10000)))
                for m in ms if m.odds and m.best_prob * m.odds > 1.0
            )
            report.by_confidence[band] = {
                "n": len(ms),
                "hit_rate": band_hits / len(ms),
                "roi": band_pnl / band_staked if band_staked > 0 else 0,
            }

        # 滚动7日
        dates = sorted(set(m.date for m in matches))
        for i in range(6, len(dates)):
            window = set(dates[i-6:i+1])
            window_matches = [m for m in matches if m.date in window]
            w_hits = sum(1 for m in window_matches if m.hit)
            report.rolling_7d.append({
                "end_date": dates[i],
                "n": len(window_matches),
                "hit_rate": w_hits / len(window_matches) if window_matches else 0,
            })

        # 校准 (分10档)
        cal_bins = defaultdict(lambda: {"pred_sum": 0.0, "hits": 0, "n": 0})
        for m in matches:
            bin_key = round(m.best_prob * 10) / 10
            cal_bins[bin_key]["pred_sum"] += m.best_prob
            cal_bins[bin_key]["hits"] += 1 if m.hit else 0
            cal_bins[bin_key]["n"] += 1
        report.calibration = {
            f"{k:.0%}": {
                "n": v["n"],
                "avg_pred": round(v["pred_sum"] / v["n"], 3),
                "actual_freq": round(v["hits"] / v["n"], 3),
                "gap": round(v["pred_sum"] / v["n"] - v["hits"] / v["n"], 3),
            }
            for k, v in sorted(cal_bins.items())
        }

        # 各源Brier对比
        report.source_comparison = self._source_brier_comparison()

        return report

    @staticmethod
    def _brier(m: BacktestMatch) -> float:
        actual_vec = [0.0, 0.0, 0.0]
        idx = {"home": 0, "draw": 1, "away": 2}[m.actual_outcome]
        actual_vec[idx] = 1.0
        return sum((p - a) ** 2 for p, a in zip(m.pred_probs, actual_vec))

    def _load_matches(self) -> list[BacktestMatch]:
        """从 data/daily/ 加载所有有结果的预测"""
        matches = []
        if not self.daily_dir.exists():
            return matches

        db_results = self._load_db_results()

        for day_dir in sorted(self.daily_dir.iterdir()):
            if not day_dir.is_dir():
                continue
            pred_file = day_dir / "predictions.json"
            if not pred_file.exists():
                continue

            date_str = day_dir.name
            try:
                predictions = json.loads(pred_file.read_text())
            except Exception:
                continue

            for p in predictions:
                result = db_results.get(p.get("match_id"))
                if not result:
                    key = f"{p['home_team']}_vs_{p['away_team']}"
                    result = db_results.get(key)
                if not result:
                    continue

                score_h, score_a = result["score_home"], result["score_away"]
                if score_h is None:
                    continue
                if score_h > score_a:
                    actual = "home"
                elif score_h == score_a:
                    actual = "draw"
                else:
                    actual = "away"

                probs = (p["home_win_prob"], p["draw_prob"], p["away_win_prob"])
                best_idx = probs.index(max(probs))
                best_pick = ["home", "draw", "away"][best_idx]
                best_prob = probs[best_idx]
                odds = p.get(f"{best_pick}_odds")

                matches.append(BacktestMatch(
                    date=date_str,
                    match_id=p.get("match_id", ""),
                    league=p.get("competition", "unknown"),
                    home_team=p["home_team"],
                    away_team=p["away_team"],
                    pred_probs=probs,
                    confidence=p.get("confidence", 0),
                    actual_outcome=actual,
                    score=(score_h, score_a),
                    best_pick=best_pick,
                    best_prob=best_prob,
                    odds=odds,
                    hit=best_pick == actual,
                ))

        return matches

    def _load_db_results(self) -> dict:
        """从MatchDB加载所有比赛结果"""
        results = {}
        if not self.db_path.exists():
            return results

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT match_id, home_team, away_team, score_home, score_away "
                "FROM match_history WHERE score_home IS NOT NULL"
            ).fetchall()
            for r in rows:
                entry = {"score_home": r["score_home"], "score_away": r["score_away"]}
                results[r["match_id"]] = entry
                results[f"{r['home_team']}_vs_{r['away_team']}"] = entry
        except Exception:
            pass
        finally:
            conn.close()
        return results

    def _source_brier_comparison(self) -> dict:
        """从review_ledger读取各源Brier"""
        ledger_path = self.data_dir / "state" / "review_ledger.jsonl"
        if not ledger_path.exists():
            return {}

        source_briers = defaultdict(list)
        try:
            for line in ledger_path.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = json.loads(line)
                for src in ["model", "market", "djyy", "final"]:
                    b = entry.get(f"brier_{src}")
                    if b is not None:
                        source_briers[src].append(b)
        except Exception:
            pass

        return {
            src: round(sum(bs) / len(bs), 4)
            for src, bs in source_briers.items() if bs
        }
