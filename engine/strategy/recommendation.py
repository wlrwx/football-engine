from __future__ import annotations
"""推荐引擎 - 移植自 lottery-football 的核心推荐逻辑"""
from dataclasses import dataclass, field


@dataclass
class RecommendationConfig:
    """推荐参数（lottery-football stable 预设）"""
    # 让球推荐阈值：让球概率 >= 此值时切换到让球推荐
    pair_switch_threshold: float = 0.6816
    # 让球反转阈值：让球概率 < 此值时反转推荐
    handicap_inversion_threshold: float = 0.4678
    # 单选阈值：最强概率 >= 此值时从双选缩为单选
    single_recommendation_threshold: float = 0.7172
    # 最低赔率过滤
    min_odds: float = 1.03


@dataclass
class Recommendation:
    """单场推荐结果"""
    match_id: str
    home_team: str
    away_team: str
    competition: str
    # 推荐选项: ["home", "draw"] 或 ["away"] 等
    selections: list[str] = field(default_factory=list)
    # 是否使用让球盘
    use_handicap: bool = False
    handicap_value: float = 0.0
    # 推荐概率
    confidence: float = 0.0
    # 对应赔率
    odds: list[float] = field(default_factory=list)
    # 推荐原因
    reason: str = ""


class RecommendationEngine:
    """
    推荐引擎。
    移植 lottery-football 的 pair-switch / handicap-inversion / single-collapse 逻辑。

    核心思路：
    1. 找到所有盘口（正常 + 让球）中概率最高的格子
    2. 如果最高在正常盘且是胜/负 → 检查让球盘同方向是否够高 → 切换到让球双选
    3. 如果最高在让球盘但概率不够高 → 反转推荐另外两个
    4. 如果双选中有一个概率特别高 → 缩为单选
    5. 赔率过滤：所有推荐选项赔率必须 >= min_odds
    """

    def __init__(self, config: RecommendationConfig | None = None):
        self.cfg = config or RecommendationConfig()

    def recommend(
        self,
        match_id: str,
        home_team: str,
        away_team: str,
        competition: str,
        # 正常盘概率
        normal_probs: tuple[float, float, float],  # (win, draw, lose)
        normal_odds: tuple[float, float, float] | None = None,
        # 让球盘概率
        handicap_probs: tuple[float, float, float] | None = None,
        handicap_odds: tuple[float, float, float] | None = None,
        handicap_value: float = 0.0,
    ) -> Recommendation | None:
        """生成推荐，不满足条件返回 None"""
        cfg = self.cfg
        labels = ["home", "draw", "away"]

        # 构建所有盘口的概率行
        rows = []
        rows.append(("normal", normal_probs, normal_odds, 0.0))
        if handicap_probs and handicap_value != 0:
            rows.append(("handicap", handicap_probs, handicap_odds, handicap_value))

        # 找全局最高概率格子
        best_row_idx = 0
        best_cell_idx = 0
        best_prob = 0.0
        for ri, (_, probs, _, _) in enumerate(rows):
            for ci, p in enumerate(probs):
                if p > best_prob:
                    best_prob = p
                    best_row_idx = ri
                    best_cell_idx = ci

        row_type, row_probs, row_odds, hdp_val = rows[best_row_idx]
        best_label = labels[best_cell_idx]

        selections = []
        use_handicap = False
        reason = ""

        # === Pair-switch 逻辑 ===
        if row_type == "normal" and best_cell_idx != 1:  # 最高在正常盘的胜/负
            # 检查让球盘同方向
            if handicap_probs and handicap_value != 0:
                hdp_same = handicap_probs[best_cell_idx]
                if hdp_same >= cfg.pair_switch_threshold and hdp_same < best_prob:
                    # 切换到让球盘：推荐 {同方向, 平}
                    use_handicap = True
                    hdp_val = handicap_value
                    if best_cell_idx == 0:  # 主胜方向
                        selections = ["home", "draw"]
                    else:  # 客胜方向
                        selections = ["away", "draw"]
                    row_odds = handicap_odds
                    reason = f"让球盘同方向 {hdp_same:.1%} >= {cfg.pair_switch_threshold:.1%}，切换让球推荐"
                else:
                    # 正常盘相邻双选
                    selections = self._adjacent_mask(best_cell_idx)
                    reason = f"正常盘最高 {best_label} {best_prob:.1%}"
            else:
                selections = self._adjacent_mask(best_cell_idx)
                reason = f"正常盘最高 {best_label} {best_prob:.1%}"

        # === Handicap-inversion 逻辑 ===
        elif row_type == "handicap" and best_cell_idx != 1:
            if best_prob < cfg.handicap_inversion_threshold:
                # 概率太低，反转推荐另外两个
                use_handicap = True
                hdp_val = handicap_value
                all_labels = {"home", "draw", "away"}
                selections = sorted(all_labels - {best_label})
                reason = f"让球盘最高 {best_prob:.1%} < {cfg.handicap_inversion_threshold:.1%}，反转推荐"
            else:
                use_handicap = True
                hdp_val = handicap_value
                selections = self._adjacent_mask(best_cell_idx)
                reason = f"让球盘最高 {best_label} {best_prob:.1%}"

        # === 平局最高 ===
        else:
            selections = ["draw"]
            reason = f"平局概率最高 {best_prob:.1%}"

        # === Single-collapse 逻辑 ===
        if len(selections) == 2:
            probs_for_check = row_probs if row_probs else normal_probs
            sel_indices = [labels.index(s) for s in selections]
            max_sel_prob = max(probs_for_check[i] for i in sel_indices)
            if max_sel_prob >= cfg.single_recommendation_threshold:
                best_sel = selections[sel_indices.index(
                    sel_indices[0] if probs_for_check[sel_indices[0]] >= probs_for_check[sel_indices[1]]
                    else sel_indices[1]
                )]
                # 找概率最高的那个
                if probs_for_check[sel_indices[0]] >= probs_for_check[sel_indices[1]]:
                    selections = [selections[0]]
                else:
                    selections = [selections[1]]
                reason += f" → 单选收缩 ({max_sel_prob:.1%} >= {cfg.single_recommendation_threshold:.1%})"

        # === 赔率过滤 ===
        if row_odds:
            for sel in selections:
                idx = labels.index(sel)
                if row_odds[idx] < cfg.min_odds:
                    return None  # 赔率太低，不推荐

        # 计算推荐置信度
        if row_probs:
            confidence = sum(row_probs[labels.index(s)] for s in selections)
        else:
            confidence = best_prob

        # 收集赔率
        rec_odds = []
        if row_odds:
            rec_odds = [row_odds[labels.index(s)] for s in selections]

        return Recommendation(
            match_id=match_id,
            home_team=home_team,
            away_team=away_team,
            competition=competition,
            selections=selections,
            use_handicap=use_handicap,
            handicap_value=hdp_val if use_handicap else 0.0,
            confidence=round(confidence, 4),
            odds=rec_odds,
            reason=reason,
        )

    def recommend_batch(self, matches: list[dict]) -> list[Recommendation]:
        """批量推荐"""
        results = []
        for m in matches:
            rec = self.recommend(
                match_id=m.get("match_id", ""),
                home_team=m.get("home_team", ""),
                away_team=m.get("away_team", ""),
                competition=m.get("competition", ""),
                normal_probs=(
                    m.get("home_win_prob", 0),
                    m.get("draw_prob", 0),
                    m.get("away_win_prob", 0),
                ),
                normal_odds=(
                    m.get("home_odds"),
                    m.get("draw_odds"),
                    m.get("away_odds"),
                ) if m.get("home_odds") else None,
                handicap_probs=(
                    m.get("handicap_home_prob"),
                    m.get("handicap_draw_prob"),
                    m.get("handicap_away_prob"),
                ) if m.get("handicap_home_prob") else None,
                handicap_odds=(
                    m.get("hdp_home_odds"),
                    m.get("hdp_draw_odds"),
                    m.get("hdp_away_odds"),
                ) if m.get("hdp_home_odds") else None,
                handicap_value=m.get("handicap", 0.0) or 0.0,
            )
            if rec:
                results.append(rec)
        return results

    @staticmethod
    def _adjacent_mask(best_idx: int) -> list[str]:
        """
        相邻双选逻辑：
        主胜 → {主胜, 平}
        平 → {平}
        客胜 → {平, 客胜}
        """
        if best_idx == 0:
            return ["home", "draw"]
        elif best_idx == 1:
            return ["draw"]
        else:
            return ["draw", "away"]
