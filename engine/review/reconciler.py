"""比赛对账器 - 对比预测与实际结果，计算准确率

核心流程:
  1. 读取当日预测数据 (predictions.json)
  2. 读取/录入实际赛果 (results.json)
  3. 逐条对比，计算方向/比分命中率
  4. 生成对账报告
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional


@dataclass
class ReconciliationItem:
    """单场对账结果"""
    match_id: str
    home_team: str
    away_team: str
    # 预测
    pred_home_win_prob: float
    pred_draw_prob: float
    pred_away_win_prob: float
    pred_result: str  # "home" / "draw" / "away"
    pred_top_score: str  # 最可能比分
    # 实际
    actual_home_score: Optional[int]
    actual_away_score: Optional[int]
    actual_result: Optional[str]  # "home" / "draw" / "away"
    # 对账结果
    direction_correct: bool
    score_correct: bool
    error_reason: str = ""  # 偏差原因（手动标注或自动分析）


class MatchReconciler:
    """比赛对账器"""

    def __init__(self, data_dir: str = "data/daily"):
        self.data_dir = Path(data_dir)

    def load_predictions(self, target_date: date) -> list[dict]:
        """加载当日预测数据"""
        pred_file = self.data_dir / target_date.strftime("%Y-%m-%d") / "predictions.json"
        if not pred_file.exists():
            return []
        with open(pred_file, encoding="utf-8") as f:
            return json.load(f)

    def load_results(self, target_date: date) -> dict:
        """加载当日赛果（若有）"""
        result_file = self.data_dir / target_date.strftime("%Y-%m-%d") / "results.json"
        if not result_file.exists():
            return {}
        with open(result_file, encoding="utf-8") as f:
            return {r["match_id"]: r for r in json.load(f)}

    def save_results(self, target_date: date, results: list[dict]) -> None:
        """保存赛果数据"""
        result_dir = self.data_dir / target_date.strftime("%Y-%m-%d")
        result_dir.mkdir(parents=True, exist_ok=True)
        with open(result_dir / "results.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    def reconcile(self, target_date: date) -> tuple[list[ReconciliationItem], dict]:
        """执行对账

        返回: (对账明细列表, 汇总统计)
        """
        predictions = self.load_predictions(target_date)
        results_map = self.load_results(target_date)

        items = []
        for p in predictions:
            match_id = p.get("match_id", "")
            # 确定预测结果
            hwp = p.get("home_win_prob") or 0
            dwp = p.get("draw_prob") or 0
            awp = p.get("away_win_prob") or 0
            if hwp >= dwp and hwp >= awp:
                pred_result = "home"
            elif dwp >= hwp and dwp >= awp:
                pred_result = "draw"
            else:
                pred_result = "away"
            
            # 最可能比分
            top_scores = p.get("top_scores") or []
            pred_top_score = f"{top_scores[0][0]}-{top_scores[0][1]}" if top_scores else ""

            # 实际结果
            actual = results_map.get(match_id) or {}
            hs = actual.get("home_score")
            as_ = actual.get("away_score")
            
            actual_result = None
            if hs is not None and as_ is not None:
                if hs > as_:
                    actual_result = "home"
                elif hs == as_:
                    actual_result = "draw"
                else:
                    actual_result = "away"

            # 对账
            direction_correct = pred_result == actual_result if actual_result else False
            actual_score_str = f"{hs}-{as_}" if hs is not None and as_ is not None else ""
            score_correct = (pred_top_score == actual_score_str) if pred_top_score and actual_score_str else False

            items.append(ReconciliationItem(
                match_id=match_id,
                home_team=p.get("home_team", ""),
                away_team=p.get("away_team", ""),
                pred_home_win_prob=hwp,
                pred_draw_prob=dwp,
                pred_away_win_prob=awp,
                pred_result=pred_result,
                pred_top_score=pred_top_score,
                actual_home_score=hs,
                actual_away_score=as_,
                actual_result=actual_result,
                direction_correct=direction_correct,
                score_correct=score_correct,
            ))

        # 统计
        total_with_result = sum(1 for i in items if i.actual_result is not None)
        direction_correct_count = sum(1 for i in items if i.direction_correct)
        score_correct_count = sum(1 for i in items if i.score_correct)

        stats = {
            "date": target_date.strftime("%Y-%m-%d"),
            "total_predictions": len(items),
            "total_with_result": total_with_result,
            "direction_accuracy": direction_correct_count / total_with_result if total_with_result > 0 else 0,
            "score_accuracy": score_correct_count / total_with_result if total_with_result > 0 else 0,
            "direction_correct": direction_correct_count,
            "score_correct": score_correct_count,
        }

        return items, stats
