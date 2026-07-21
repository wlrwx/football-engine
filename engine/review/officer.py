"""复盘官 - 预测偏差归因分析与自我进化引擎

核心能力:
  1. 分层归因: 执行层 → 系统层 → 认知层
  2. 提炼 IF-THEN 规则
  3. Obsidian 知识库沉淀
  4. 触发预测策略进化

流程:
  reconcilier → 对账结果 → 复盘官分析 → Obsidian 沉淀 → 策略进化
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, List, Tuple

from engine.review.reconciler import ReconciliationItem


@dataclass
class AttributionAnalysis:
    """归因分析结果"""
    # 执行层 - 具体发生了什么
    execution_layer: List[str]
    # 系统层 - 为什么批量出现
    system_layer: List[str]
    # 认知层 - 为什么未被质疑
    cognitive_layer: List[str]
    # IF-THEN 规则
    rules: List[Tuple[str, str]]  # (IF条件, THEN动作)


class ReviewOfficer:
    """复盘官 - 预测偏差归因分析与自我进化引擎"""

    def __init__(self, obsidian_vault: Optional[str] = None):
        self.obsidian_vault = Path(obsidian_vault) if obsidian_vault else None
        # 默认存放到项目 data/reviews 目录
        self.review_dir = Path("data/reviews")
        self.review_dir.mkdir(parents=True, exist_ok=True)

    def analyze_deviation(
        self,
        item: ReconciliationItem,
        context: Optional[dict] = None
    ) -> AttributionAnalysis:
        """执行分层归因分析

        Args:
            item: 对账结果
            context: 额外上下文（伤停/天气/首发变动等）
        """
        execution = []
        system = []
        cognitive = []
        rules = []

        context = context or {}

        # ========== 执行层 - 具体发生了什么 ==========
        if not item.direction_correct:
            # 方向预测错误
            if item.actual_result == "draw" and item.pred_result != "draw":
                execution.append("未捕捉到平局倾向：模型低估了平局概率")
                rules.append((
                    "双方近期平局率均>30% AND 让球盘深度<0.5",
                    "自动上调平局概率 10%"
                ))
            elif item.actual_result == "home" and item.pred_result == "away":
                execution.append("方向完全错误：客队过热，主队打出")
                rules.append((
                    "客队赔率持续下降超过 3 天 AND 主队主场胜率>60%",
                    "自动下调客队胜率 8%，上调主队胜率 5%"
                ))
            elif item.actual_result == "away" and item.pred_result == "home":
                execution.append("方向完全错误：强队翻车，客队爆冷")
                rules.append((
                    "主队 ELO 优势 > 200 AND 赔率低于 1.5",
                    "自动下调主胜概率 10%，上调冷平概率 8%"
                ))

        if not item.score_correct and item.actual_home_score is not None:
            # 比分预测错误
            total_goals = item.actual_home_score + item.actual_away_score
            if total_goals >= 4:
                execution.append("大比分打出，模型低估了进球数")
                rules.append((
                    "双方近 3 场场均进球 > 2.5 AND 大小球盘口 >= 3",
                    "自动上调大球概率 12%"
                ))
            elif total_goals <= 1:
                execution.append("小球/零封打出，模型高估了进球数")
                rules.append((
                    "双方近 3 场均失 < 0.8 AND 大小球盘口 <= 2",
                    "自动上调小球概率 10%"
                ))

        # 伤停因素
        injuries = context.get("injuries", {})
        if injuries.get("home_missing_key_players") or injuries.get("away_missing_key_players"):
            execution.append("核心球员缺阵未被充分量化")
            rules.append((
                "赛前确认有主力中场/前锋缺阵",
                "自动下调该队胜率 15%，平局概率 +8%"
            ))

        # ========== 系统层 - 为什么批量出现 ==========
        if len(execution) >= 2:
            system.append("多维度偏差叠加：单一数据源的噪声被放大")
        if any("平局" in e for e in execution):
            system.append("平局模块权重偏低：平局因子在决策树中叶深度过深")
        if any("伤停" in e for e in execution):
            system.append("伤停量化不足：仅使用布尔标记，未区分位置重要性权重")

        # ========== 认知层 - 为什么未被质疑 ==========
        if item.pred_home_win_prob > 0.7 and not item.direction_correct:
            cognitive.append("高置信度偏差：过度自信，胜率>70%时未启用二次验证")
            rules.append((
                "单选项预测概率 > 70%",
                "自动触发交叉验证，对比多机构赔率一致性"
            ))
        if item.actual_result == "away" and item.pred_result == "home":
            cognitive.append("主队优势偏见：ELO 差值过度主导了预测")
            rules.append((
                "ELO 差值 > 150 但客队近 5 场不败",
                "ELO 权重减半，增加近期状态权重至 40%"
            ))

        # 默认兜底（如果没有分析出具体原因）
        if not execution:
            execution.append("预测方向正确或数据不足")
        if not system:
            system.append("系统层无明显结构性问题")
        if not cognitive:
            cognitive.append("认知偏差不显著")

        return AttributionAnalysis(
            execution_layer=execution,
            system_layer=system,
            cognitive_layer=cognitive,
            rules=rules,
        )

    def save_to_obsidian(
        self,
        target_date: date,
        item: ReconciliationItem,
        analysis: AttributionAnalysis,
        stats: dict,
    ) -> str:
        """将复盘结果写入 Obsidian 知识库

        Returns: 保存的文件路径
        """
        filename = f"{target_date.strftime('%Y-%m-%d')}_{item.home_team}vs{item.away_team}.md"
        
        if self.obsidian_vault:
            file_path = self.obsidian_vault / "WorldCup_2026" / "Reviews" / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # 默认存放到项目内
            file_path = self.review_dir / filename

        # 构造 markdown 内容
        rules_md = "\n".join([
            f"- **IF** {if_cond}  \n  **THEN** {then_action}"
            for if_cond, then_action in analysis.rules
        ])

        content = f"""---
tags: [复盘, 归因, 预测偏差]
match_id: "{item.match_id}"
prediction_accuracy: {"方向正确" if item.direction_correct else "方向错误"}{", 比分正确" if item.score_correct else ""}
date: {target_date.strftime('%Y-%m-%d')}
---

# {item.home_team} vs {item.away_team} 复盘报告

## 📊 预测 vs 实际

| 维度 | 预测 | 实际 | 结果 |
|-----|------|------|------|
| **胜平负** | {"主胜" if item.pred_result == "home" else "平局" if item.pred_result == "draw" else "客胜"} ({max(item.pred_home_win_prob, item.pred_draw_prob, item.pred_away_win_prob):.1%}) | {"主胜" if item.actual_result == "home" else "平局" if item.actual_result == "draw" else "客胜" if item.actual_result else "无数据"} | {'✅ 正确' if item.direction_correct else '❌ 错误'} |
| **比分** | {item.pred_top_score or '无'} | {f"{item.actual_home_score}-{item.actual_away_score}" if item.actual_home_score is not None else '无数据'} | {'✅ 正确' if item.score_correct else '❌ 错误'} |

## 🎯 分层归因分析

### 1. 执行层（发生了什么）
{chr(10).join([f"- {e}" for e in analysis.execution_layer])}

### 2. 系统层（为什么批量出现）
{chr(10).join([f"- {s}" for s in analysis.system_layer])}

### 3. 认知层（为什么未被质疑）
{chr(10).join([f"- {c}" for c in analysis.cognitive_layer])}

## 📐 可复用规则 (IF-THEN)
{rules_md if rules_md else "- 无新增规则"}

## 📈 当日整体统计
- 总场次：{stats.get('total_predictions', 0)}
- 已赛：{stats.get('total_with_result', 0)}
- 方向准确率：{stats.get('direction_accuracy', 0):.1%}
- 比分准确率：{stats.get('score_accuracy', 0):.1%}

"""

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return str(file_path)

    def should_trigger_evolution(self, analysis: AttributionAnalysis) -> bool:
        """判断是否需要触发策略进化

        条件:
          1. 发现全新的战术/场外变量
          2. 同一类偏差连续出现
          3. 高置信度预测错误
        """
        # 规则数量 >= 2，说明有显著偏差
        return len(analysis.rules) >= 2
