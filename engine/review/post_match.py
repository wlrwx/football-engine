from __future__ import annotations
"""赛后复盘 + 偏差检测 + 滚动账本

核心: 对每场已结算比赛, 计算各信号源(model/market/DJYY)的Brier score,
按联赛/置信档/赔率档聚合命中率, 识别系统偏差。
优化器基于此数据做反事实重放验证。
"""
import hashlib
import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path


@dataclass
class MatchReview:
    """单场复盘记录 - 优化器反事实重放的原子单元"""
    match_id: str
    date: str
    league: str
    actual_idx: int  # 0=主胜 1=平 2=客胜
    # 三路原始概率 (供反事实重放)
    model_raw: list  # [h, d, a]
    market_fair: list | None
    djyy_prob: list | None
    final_prob: list  # [h, d, a]
    # 分档维度
    confidence_tier: str  # "low" / "mid" / "high"
    odds_band: str  # "1.2-1.5" / "1.5-2.0" / "2.0-3.0" / "3.0+"
    best_selection: int  # argmax(final)
    hit: bool
    pnl: float
    # per-source Brier
    brier_model: float | None
    brier_market: float | None
    brier_djyy: float | None
    brier_final: float
    # 上下文
    home_xg: float = 0.0
    away_xg: float = 0.0
    total_goals_actual: int = 0


@dataclass
class BiasFlag:
    """系统偏差标记"""
    dimension: str  # "league" / "confidence_tier" / "odds_band" / "outcome"
    key: str
    outcome: str  # "home" / "draw" / "away"
    predicted_avg: float
    actual_rate: float
    gap: float  # predicted - actual (>0 = 高估)
    n: int
    severity: str  # "info" / "warn" / "critical"
    suggested_action: str


def brier_score(probs: list, actual_idx: int) -> float:
    """三分类 Brier score"""
    if not probs or len(probs) < 3:
        return 1.0
    return sum((p - (1.0 if i == actual_idx else 0.0)) ** 2
               for i, p in enumerate(probs[:3]))


def wilson_lower(hits: int, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound (小样本保护)"""
    if n == 0:
        return 0.0
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, center - spread)


class ReviewLedger:
    """append-only 滚动账本 data/state/review_ledger.jsonl"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, reviews: list[MatchReview]):
        with open(self.path, "a", encoding="utf-8") as f:
            for r in reviews:
                f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    def load_window(self, n_matches: int | None = None,
                    days: int | None = None) -> list[MatchReview]:
        """加载最近 n_matches 场或最近 days 天的记录"""
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                records.append(MatchReview(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        if days:
            cutoff = datetime.now().strftime("%Y-%m-%d")
            # 简单按日期字符串过滤
            records = [r for r in records if r.date >= cutoff[:8] + str(int(cutoff[8:10]) - days).zfill(2)]
        if n_matches:
            records = records[-n_matches:]
        return records

    def split_train_val(self, val_matches: int = 30
                        ) -> tuple[list[MatchReview], list[MatchReview]]:
        """时间序列切分: 旧=train, 最近val_matches=val (无未来泄漏)"""
        all_records = self.load_window()
        if len(all_records) <= val_matches:
            return all_records, all_records
        return all_records[:-val_matches], all_records[-val_matches:]

    @property
    def count(self) -> int:
        if not self.path.exists():
            return 0
        return sum(1 for line in self.path.read_text().strip().split("\n") if line.strip())


class PostMatchReviewer:
    """赛后复盘: 逐场计算各源Brier, 聚合分维度命中率"""

    def __init__(self, data_dir: Path, config: dict | None = None):
        self.data_dir = data_dir
        self.cfg = config or {}
        self.ledger = ReviewLedger(data_dir / "state" / "review_ledger.jsonl")

    def review_day(self, date_str: str) -> dict:
        """对指定日期做完整复盘, 返回报告dict"""
        daily_dir = self.data_dir / "daily" / date_str
        predictions = self._load_json(daily_dir / "predictions.json", [])
        results = self._load_json(daily_dir / "results.json", [])

        if not predictions or not results:
            return {"date": date_str, "n_matches": 0, "status": "no_data"}

        # 建立 match_id → prediction 索引 (含 fixture fallback)
        pred_map = {}
        for p in predictions:
            mid = p.get("match_id", "")
            pred_map[mid] = p
            fixture = mid.split("_", 1)[-1] if "_" in mid else mid
            pred_map[fixture] = p

        reviews = []
        for r in results:
            mid = r.get("match_id", "")
            hs, as_ = r.get("home_score"), r.get("away_score")
            if hs is None or as_ is None:
                continue

            pred = pred_map.get(mid)
            if not pred:
                fixture = mid.split("_", 1)[-1] if "_" in mid else mid
                pred = pred_map.get(fixture)
            if not pred:
                continue

            # 实际结果索引
            if hs > as_:
                actual_idx = 0
            elif hs == as_:
                actual_idx = 1
            else:
                actual_idx = 2

            # 三路原始概率
            model_raw_dict = pred.get("model_raw") or {}
            model_raw = [
                model_raw_dict.get("home", 0),
                model_raw_dict.get("draw", 0),
                model_raw_dict.get("away", 0),
            ] if model_raw_dict else None

            market_fair = pred.get("market_fair")  # [h, d, a] or None

            djyy_dict = pred.get("djyy_model_prob")
            djyy_prob = [
                djyy_dict.get("home", 0),
                djyy_dict.get("draw", 0),
                djyy_dict.get("away", 0),
            ] if djyy_dict and djyy_dict.get("home") else None

            final_prob = [
                pred.get("home_win_prob", 0),
                pred.get("draw_prob", 0),
                pred.get("away_win_prob", 0),
            ]

            # 置信档
            conf = max(final_prob)
            tier = "high" if conf > 0.55 else "mid" if conf > 0.40 else "low"

            # 赔率档
            odds_h = pred.get("home_odds") or 2.0
            band = self._odds_band(min(odds_h, pred.get("away_odds") or 2.0))

            # 命中
            best_sel = final_prob.index(max(final_prob))
            hit = best_sel == actual_idx

            review = MatchReview(
                match_id=mid,
                date=date_str,
                league=pred.get("competition", ""),
                actual_idx=actual_idx,
                model_raw=model_raw,
                market_fair=market_fair,
                djyy_prob=djyy_prob,
                final_prob=final_prob,
                confidence_tier=tier,
                odds_band=band,
                best_selection=best_sel,
                hit=hit,
                pnl=r.get("pnl", 0),
                brier_model=brier_score(model_raw, actual_idx) if model_raw else None,
                brier_market=brier_score(market_fair, actual_idx) if market_fair else None,
                brier_djyy=brier_score(djyy_prob, actual_idx) if djyy_prob else None,
                brier_final=brier_score(final_prob, actual_idx),
                home_xg=pred.get("home_xg", 0),
                away_xg=pred.get("away_xg", 0),
                total_goals_actual=hs + as_,
            )
            reviews.append(review)

        if not reviews:
            return {"date": date_str, "n_matches": 0, "status": "no_matched"}

        # 追加到账本
        self.ledger.append(reviews)

        # 聚合报告
        report = self._aggregate(date_str, reviews)

        # 写 review.json
        review_path = daily_dir / "review.json"
        review_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

        return report

    def _aggregate(self, date_str: str, reviews: list[MatchReview]) -> dict:
        """聚合统计"""
        n = len(reviews)
        hits = sum(1 for r in reviews if r.hit)

        # per-source 平均 Brier
        def _avg_brier(attr):
            vals = [getattr(r, attr) for r in reviews if getattr(r, attr) is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        source_brier = {
            "model": _avg_brier("brier_model"),
            "market": _avg_brier("brier_market"),
            "djyy": _avg_brier("brier_djyy"),
            "final": _avg_brier("brier_final"),
        }

        # 分维度命中率
        by_league = self._group_stats(reviews, "league")
        by_tier = self._group_stats(reviews, "confidence_tier")
        by_band = self._group_stats(reviews, "odds_band")

        # 偏差检测
        biases = self._detect_biases(reviews)

        report = {
            "date": date_str,
            "n_matches": n,
            "hit_rate": round(hits / n, 4),
            "hits": hits,
            "source_brier": source_brier,
            "by_league": by_league,
            "by_confidence_tier": by_tier,
            "by_odds_band": by_band,
            "biases": [asdict(b) for b in biases],
            "total_pnl": round(sum(r.pnl for r in reviews), 2),
            "generated_at": datetime.now().isoformat(),
        }

        # SHA-256
        report["sha256"] = hashlib.sha256(
            json.dumps(report, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:32]

        return report

    def _group_stats(self, reviews: list[MatchReview], attr: str) -> dict:
        """按某维度分组统计命中率+Brier+Wilson"""
        groups: dict[str, list] = {}
        for r in reviews:
            key = getattr(r, attr)
            groups.setdefault(key, []).append(r)

        result = {}
        for key, items in groups.items():
            n = len(items)
            hits = sum(1 for r in items if r.hit)
            briers = [r.brier_final for r in items]
            result[key] = {
                "n": n,
                "hit_rate": round(hits / n, 4),
                "wilson_lower": round(wilson_lower(hits, n), 4),
                "avg_brier": round(sum(briers) / n, 4),
            }
        return result

    def _detect_biases(self, reviews: list[MatchReview]) -> list[BiasFlag]:
        """检测系统偏差: 预测概率 vs 实际频率"""
        flags = []
        min_n = self.cfg.get("bias_min_samples", 5)
        gap_threshold = self.cfg.get("bias_gap_threshold", 0.10)

        # 按联赛检测主胜高估/低估
        leagues: dict[str, list] = {}
        for r in reviews:
            leagues.setdefault(r.league, []).append(r)

        for league, items in leagues.items():
            if len(items) < min_n:
                continue
            # 主胜: 预测平均 vs 实际频率
            pred_home_avg = sum(r.final_prob[0] for r in items) / len(items)
            actual_home_rate = sum(1 for r in items if r.actual_idx == 0) / len(items)
            gap = pred_home_avg - actual_home_rate
            if abs(gap) >= gap_threshold:
                flags.append(BiasFlag(
                    dimension="league",
                    key=league,
                    outcome="home",
                    predicted_avg=round(pred_home_avg, 4),
                    actual_rate=round(actual_home_rate, 4),
                    gap=round(gap, 4),
                    n=len(items),
                    severity="warn" if abs(gap) < 0.20 else "critical",
                    suggested_action="reduce home_adv_weight" if gap > 0 else "increase home_adv_weight",
                ))

        # 全局平局偏差
        pred_draw_avg = sum(r.final_prob[1] for r in reviews) / len(reviews)
        actual_draw_rate = sum(1 for r in reviews if r.actual_idx == 1) / len(reviews)
        draw_gap = pred_draw_avg - actual_draw_rate
        if len(reviews) >= min_n and abs(draw_gap) >= gap_threshold:
            flags.append(BiasFlag(
                dimension="outcome",
                key="all",
                outcome="draw",
                predicted_avg=round(pred_draw_avg, 4),
                actual_rate=round(actual_draw_rate, 4),
                gap=round(draw_gap, 4),
                n=len(reviews),
                severity="warn",
                suggested_action="increase market_weight (trust market on draws)" if draw_gap > 0 else "reduce draw_bias",
            ))

        return flags

    @staticmethod
    def _odds_band(odds: float) -> str:
        if odds < 1.5:
            return "1.0-1.5"
        elif odds < 2.0:
            return "1.5-2.0"
        elif odds < 3.0:
            return "2.0-3.0"
        else:
            return "3.0+"

    @staticmethod
    def _load_json(path: Path, default):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return default


class BiasDetector:
    """从滚动账本检测系统偏差 (用于联赛优化器先验提示)"""

    def __init__(self, ledger: ReviewLedger, config: dict | None = None):
        self.ledger = ledger
        self.cfg = config or {}

    def scan(self, window_matches: int = 200) -> list[BiasFlag]:
        """扫描最近 window_matches 场的系统偏差"""
        reviews = self.ledger.load_window(n_matches=window_matches)
        if len(reviews) < self.cfg.get("bias_min_samples", 10):
            return []

        reviewer = PostMatchReviewer(Path("."), self.cfg)
        return reviewer._detect_biases(reviews)
