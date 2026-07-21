#!/usr/bin/env python3
"""
复盘入口 - 一键执行对账 + 归因分析 + Obsidian 沉淀

用法:
  python -m engine.review.review --date 2026-07-20          # 复盘指定日期
  python -m engine.review.review --date today --auto-save    # 自动保存到 Obsidian

流程:
  对账器 → 复盘官分析 → Obsidian 沉淀 → 策略进化建议
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from engine.review.reconciler import MatchReconciler
from engine.review.officer import ReviewOfficer


def run_review(target_date: date, auto_save: bool = True):
    """执行完整复盘流程"""
    print("=" * 60)
    print(f"📊 世界杯预测系统 · 复盘官 - {target_date.strftime('%Y-%m-%d')}")
    print("=" * 60)

    # Step 1: 对账
    print("\n[1/4] 执行对账...")
    reconciler = MatchReconciler()
    items, stats = reconciler.reconcile(target_date)

    print(f"  总预测: {stats['total_predictions']} 场")
    print(f"  已出结果: {stats['total_with_result']} 场")
    print(f"  方向准确率: {stats['direction_accuracy']:.1%} ({stats['direction_correct']}/{stats['total_with_result']})")
    print(f"  比分准确率: {stats['score_accuracy']:.1%} ({stats['score_correct']}/{stats['total_with_result']})")

    if stats['total_with_result'] == 0:
        print("\n⚠️  当日暂无已出赛果，无法执行复盘")
        return

    # Step 2: 归因分析
    print("\n[2/4] 分层归因分析...")
    officer = ReviewOfficer()
    
    saved_files = []
    evolution_suggestions = []

    for item in items:
        if item.actual_result is None:
            continue  # 未开赛的跳过

        print(f"\n  🎯 {item.home_team} vs {item.away_team}")
        
        # 读取额外上下文（伤停/天气等，如有）
        context = {}
        context_file = Path(f"data/daily/{target_date.strftime('%Y-%m-%d')}/context.json")
        if context_file.exists():
            with open(context_file, encoding="utf-8") as f:
                all_context = json.load(f)
                context = all_context.get(item.match_id, {})

        analysis = officer.analyze_deviation(item, context)
        
        print(f"    执行层: {len(analysis.execution_layer)} 项")
        print(f"    系统层: {len(analysis.system_layer)} 项")
        print(f"    认知层: {len(analysis.cognitive_layer)} 项")
        print(f"    新增规则: {len(analysis.rules)} 条")

        if officer.should_trigger_evolution(analysis):
            evolution_suggestions.append((item.match_id, analysis.rules))

        # Step 3: 保存到 Obsidian
        if auto_save:
            saved_path = officer.save_to_obsidian(target_date, item, analysis, stats)
            saved_files.append(saved_path)

    # Step 4: 策略进化建议
    print("\n[3/4] 策略进化建议...")
    if evolution_suggestions:
        print(f"  ⚠️  发现 {len(evolution_suggestions)} 场需优化预测策略")
        for match_id, rules in evolution_suggestions:
            print(f"    - {match_id}: 建议 {len(rules)} 条规则")
    else:
        print("  ✅ 当前策略表现稳定，暂无进化建议")

    # 保存汇总报告
    print("\n[4/4] 生成汇总报告...")
    summary_file = _save_summary(target_date, items, stats, evolution_suggestions, saved_files)

    print("\n" + "=" * 60)
    print("✅ 复盘完成")
    print(f"  - 复盘报告: {len(saved_files)} 份已保存")
    print(f"  - 汇总报告: {summary_file}")
    if evolution_suggestions:
        print(f"  ⚠️  建议优化: {len(evolution_suggestions)} 场预测策略")
    print("=" * 60)


def _save_summary(target_date, items, stats, evolution_suggestions, saved_files) -> str:
    """保存当日复盘汇总"""
    summary_dir = Path("data/reviews/summaries")
    summary_dir.mkdir(parents=True, exist_ok=True)
    
    summary = {
        "date": target_date.strftime("%Y-%m-%d"),
        "stats": stats,
        "evolution_suggestions": evolution_suggestions,
        "review_files": saved_files,
        "items": [{
            "match_id": i.match_id,
            "home_team": i.home_team,
            "away_team": i.away_team,
            "pred_result": i.pred_result,
            "actual_result": i.actual_result,
            "direction_correct": i.direction_correct,
            "score_correct": i.score_correct,
        } for i in items if i.actual_result is not None]
    }

    summary_file = summary_dir / f"{target_date.strftime('%Y-%m-%d')}_summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    
    return str(summary_file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="复盘官 - 预测偏差归因分析")
    parser.add_argument("--date", type=str, default="today", help="复盘日期 (YYYY-MM-DD 或 today)")
    parser.add_argument("--no-save", action="store_true", help="不保存到 Obsidian")
    args = parser.parse_args()

    if args.date == "today":
        target = date.today()
    else:
        target = date.fromisoformat(args.date)

    run_review(target, auto_save=not args.no_save)
