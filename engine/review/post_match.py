from __future__ import annotations
"""赛后复盘 + 偏差检测 + 滚动账本

核心: 对每场已结算比赛, 计算各信号源(model/market/DJYY)的Brier score,
按联赛/置信档/赔率档聚合命中率, 识别系统偏差。
优化器基于此数据做反事实重放验证。
"""
import hashlib
import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import re

from engine.team_aliases import normalize_team, loose_normalize


def _extract_fixture(match_id: str) -> str:
    """从 match_id 提取场次号，如 '2026-07-20_周日201' → '201'"""
    if not match_id:
        return ""
    parts = re.split(r'[_\-]', match_id)
    for part in reversed(parts):
        m = re.search(r'(\d+)$', part)
        if m:
            return m.group(1)
    return ""


def _extract_pno(match_id: str) -> str:
    """从 match_id 提取完整竞彩编号，如 '2026-07-20_周日201' → '周日201'

    与 _extract_fixture 的区别: 保留"周X"前缀，避免不同星期的 001 互相误配。
    """
    if not match_id:
        return ""
    suffix = match_id.split("_", 1)[-1] if "_" in match_id else match_id
    if re.match(r'^(周一|周二|周三|周四|周五|周六|周日)\d+$', suffix):
        return suffix
    return ""


def _norm_match_no(match_id: str) -> str:
    """归一化 match_id 为竞彩编号段（账本幂等键用）：'2026-07-20_周一201' → '周一201'"""
    if not match_id:
        return ""
    return match_id.split("_", 1)[-1] if "_" in match_id else match_id


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
    # 比分命中闭环（2026-08-05 新增）：实际比分在 top_scores 中的命中位置
    #   score_rank=1 → top1 命中, 2 → top2, ..., 0 → 未进候选列表
    #   旧账本(8/5前)无此字段 → 默认 0，回填脚本负责补历史
    score_rank: int = 0
    score_hit: bool = False          # 实际比分在 top_scores 任意位置
    score_top3_hit: bool = False     # 命中主推前3（38% 基准）
    score_top5_hit: bool = False     # 命中主推前5（52% 基准）
    score_top8_hit: bool = False     # 命中候选前8（66% 基准）
    # 双源比分命中（2026-08-05 结构升级）：DJYY vs MC 各自命中位置，累积比较调权重
    #   -1 = 该源无候选数据, 0 = 有候选但未命中, N = 命中第N位
    score_djyy_rank: int = -1
    score_mc_rank: int = -1
    # 盘口信号（2026-08-05 结构化验证）：market_signal 最强方向是否命中
    market_signal_hit: bool | None = None
    # 分层评价（2026-08-06 借鉴 MBS 概率系统 8/3 批次自检方法论）
    # 结果层/进球框架层/比分层分开统计，禁止合并单一命中率掩盖结构性失真
    log_loss_final: float | None = None      # 概率质量（越低越好，不随命中次数波动）
    goal_framework_hit: bool | None = None   # 进球框架：预测大小球方向(≥3/≤2) vs 实际总进球
    prob_band: str = ""                      # 概率分段：<40% / 40-50% / 50-60% / 60%+
    freshness_risk: str = ""                 # 新鲜度风险：ok / watch / alert
    # 预测时点（2026-08-16 起记录，供时点分桶评估）
    as_of: str = ""                          # 预测生成时间（ISO，北京时间）
    kickoff: str = ""                        # 开赛时间（ISO，北京时间）
    # 融合链版本（2026-08-29）：v2 = 概率层重构后的洁净样本。
    # 校准层拟合/启用决策只认 v2，老账本（污染链产物）不再参与学习。
    chain: str = ""
    # RPS（2026-08-29 引入）：胜平负是有序结果（主<平<客），RPS 比 Brier 更贴口径。
    # 老账本无此字段 → None；历史评估由重放工具即时计算（scripts/rps_report.py）。
    rps_final: float | None = None
    rps_market: float | None = None
    rps_model: float | None = None


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


def log_loss_score(probs: list, actual_idx: int) -> float:
    """三分类 LogLoss（MBS 概率质量主指标，2026-08-06 引入）"""
    import math as _m
    if not probs or len(probs) < 3:
        return _m.log(3.0)  # 无数据=均匀分布熵
    p = max(min(probs[actual_idx], 0.999), 0.001)
    return -_m.log(p)


def _log_loss(probs: list, actual_idx: int) -> float:
    """账本字段计算入口（兼容 None）"""
    if not probs:
        return None
    return round(log_loss_score(probs, actual_idx), 4)


def rps_score(probs: list, actual_idx: int) -> float:
    """RPS（Ranked Probability Score，1X2 有序口径：主<平<客）。

    与三分类 Brier 的区别：把 主胜<平局<客胜 当作有序刻度，累计分布对比，
    「主胜预测成客胜」比「主胜预测成平局」罚得更重。0=完美，越小越好。
    """
    if not probs or len(probs) < 3:
        return 1.0
    p_h = probs[0]
    p_hd = probs[0] + probs[1]
    o_h = 1.0 if actual_idx == 0 else 0.0
    o_hd = 1.0 if actual_idx in (0, 1) else 0.0
    return ((p_h - o_h) ** 2 + (p_hd - o_hd) ** 2) / 2


def _rps(probs: list, actual_idx: int) -> float:
    """账本字段计算入口（兼容 None）"""
    if not probs:
        return None
    return round(rps_score(probs, actual_idx), 4)


def _prob_band(conf: float) -> str:
    """MBS 概率分段（8/3 自检实证：50-60% 段最危险）"""
    if conf < 0.40:
        return "<40%"
    if conf < 0.50:
        return "40-50%"
    if conf < 0.60:
        return "50-60%"
    return "60%+"


def _goal_framework_hit(pred: dict, total_goals: int) -> bool | None:
    """进球框架：模型总进球≥3 概率 >50% → 预测 ≥3球；否则 ≤2球。
    与实际总进球比较。MBS 用文字框架（≥3/≤2/2-3中枢），我们用可落盘的 Over2.5 阈值。
    total_goals 存在两种历史格式（2026-08-06 容错）：
      A. 恰好分布 [[goals, p], ...] 且 Σp≈1（如 [[1,0.35],[0,0.25],[2,0.24],...]）
      B. 累积分布 [[goals, p], ...] 递减（至少N球，如 [[1,0.77],[2,0.53],[3,0.31],...]）
    """
    tg = pred.get("total_goals") or pred.get("top_total_goals")
    if not tg:
        return None
    if not isinstance(tg, (list, tuple)):
        return None
    items = []
    for it in tg:
        if (isinstance(it, (list, tuple)) and len(it) >= 2
                and isinstance(it[0], (int, float)) and isinstance(it[1], (int, float))):
            items.append((int(it[0]), float(it[1])))
    if not items:
        return None
    items.sort(key=lambda x: x[0])  # 按进球数升序
    probs = {g: p for g, p in items}
    # 判定格式：累积分布特征 = 首项（最低进球数）概率 >= 0.5 且严格递减
    g0, p0 = items[0]
    cum = p0 >= 0.5 and all(items[i][1] > items[i + 1][1] for i in range(len(items) - 1))
    if cum:
        # 累积：P(≥3球) = 进球数3对应项（若缺，用 2 与 4 线性内插的保守值）
        p_over25 = probs.get(3)
        if p_over25 is None:
            p2, p4 = probs.get(2), probs.get(4)
            p_over25 = (p2 + p4) / 2 if p2 is not None and p4 is not None else None
    else:
        # 恰好分布：P(≥3球) = Σ goals>=3
        p_over25 = sum(p for g, p in items if g >= 3)
    if p_over25 is None or p_over25 <= 0 or p_over25 >= 1:
        return None
    pred_over = p_over25 > 0.5
    actual_over = total_goals >= 3
    return pred_over == actual_over


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
        """追加复盘记录（按 (date, 竞彩编号) 去重，防止重复结算时账本膨胀）

        幂等 key 用归一化编号而非 match_id 原文：
        "周一201" 与 "2026-07-20_周一201" 是同一场，不得重复记账。
        """
        # 已存在的键（账本可能较大，只在有记录时构建一次）
        existing_keys = self._existing_keys()
        with open(self.path, "a", encoding="utf-8") as f:
            for r in reviews:
                key = (r.date, _norm_match_no(r.match_id))
                if key in existing_keys:
                    continue
                f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
                existing_keys.add(key)

    def _existing_keys(self) -> set:
        """账本中已有的 (date, 竞彩编号) 集合"""
        keys = set()
        if not self.path.exists():
            return keys
        for line in self.path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                keys.add((rec.get("date", ""), _norm_match_no(rec.get("match_id", ""))))
            except (json.JSONDecodeError, TypeError):
                continue
        return keys

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

    def __init__(self, data_dir: Path, config: dict | None = None, chain_version: str = "v2"):
        self.data_dir = data_dir
        self.cfg = config or {}
        self.chain_version = chain_version  # 新结算样本的融合链版本标记
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
            fixture = _extract_fixture(mid)
            if fixture:
                pred_map[fixture] = p
            # 完整竞彩编号（如"周六001"），优先于裸数字，避免跨星期误配
            pno = _extract_pno(mid)
            if pno:
                pred_map[pno] = p
            # 也用 "主队_vs_客队" 建索引（原文 + 归一化 + 宽松，译名变体兜底）
            hm = p.get("home_team", "")
            aw = p.get("away_team", "")
            if hm and aw:
                pred_map[f"{hm}_vs_{aw}"] = p
                pred_map[f"{normalize_team(hm)}_vs_{normalize_team(aw)}"] = p
                pred_map[f"{loose_normalize(hm)}_vs_{loose_normalize(aw)}"] = p

        reviews = []
        seen_teams: set = set()
        seen_preds: set = set()
        for r in results:
            mid = r.get("match_id", "")
            hs, as_ = r.get("home_score"), r.get("away_score")
            if hs is None or as_ is None:
                continue

            # 同一场比赛可能以多个ID存在于 results.json（旧结算遗留），按队名去重
            _tkey = (r.get("home_team", ""), r.get("away_team", ""))
            if _tkey in seen_teams:
                continue
            seen_teams.add(_tkey)

            pred = pred_map.get(mid)
            if not pred:
                # 完整编号匹配（如 results.json 里存的是"周六001"）
                pno = _extract_pno(mid)
                if pno:
                    pred = pred_map.get(pno)
            if not pred:
                fixture = _extract_fixture(mid)
                if fixture:
                    pred = pred_map.get(fixture)
            if not pred:
                # fallback: 旧格式
                fixture = mid.split("_", 1)[-1] if "_" in mid else mid
                pred = pred_map.get(fixture)
            if not pred:
                # 用队伍名匹配（原文 → 归一化 → 宽松，译名变体兜底）
                hm = r.get("home_team", "")
                aw = r.get("away_team", "")
                if hm and aw:
                    pred = (pred_map.get(f"{hm}_vs_{aw}")
                            or pred_map.get(f"{normalize_team(hm)}_vs_{normalize_team(aw)}")
                            or pred_map.get(f"{loose_normalize(hm)}_vs_{loose_normalize(aw)}"))
            if not pred:
                continue

            # 同一预测只复盘一次：results.json 中同一场比赛可能有多种 ID/队名形式
            # （如 "周五002" 与 "2026-07-31_周五002"、新浪缩写队名 vs 竞彩全名），
            # 用预测的 match_id 去重，避免一场比赛在账本里记多条。
            _pmid = pred.get("match_id", "")
            if _pmid in seen_preds:
                continue
            seen_preds.add(_pmid)

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

            # 赔率档：用最大概率方向（预测选择）的赔率，而不是主客赔率的最小值
            # 方向口径：必须与 engine/main.py 的 _pick_direction / 结算主循环完全一致
            # （2026-08-12 修复：此前账本用纯 argmax，未同步 draw_alert 平局改判，
            #  导致 9 条账本 hit 与 predictions.direction_correct 分裂；R1 场次
            #  结算时账本会记错 hit——平衡盘口触发时必爆）
            # （2026-08-17 三修：R1 league_draw 改判实盘证伪已停用（8/13 起 8 场
            #  0 中，5 场把正确 argmax 改错）。全部平局改判停用，统一纯 argmax，
            #  与 _pick_direction / 结算主循环口径一致。draw_alert 仅作展示标记）
            best_sel = final_prob.index(max(final_prob))
            _sel_odds_key = ("home_odds", "draw_odds", "away_odds")[best_sel]
            sel_odds = pred.get(_sel_odds_key) or 2.0
            band = self._odds_band(sel_odds)

            # 命中
            hit = best_sel == actual_idx

            # 比分命中位置（闭环核心）：实际比分在 top_scores 中的排名
            # 1=最可能, 2=次可能, ..., 0=未进候选列表
            top_scores = pred.get("top_scores") or []
            score_rank = 0
            if hs is not None and as_ is not None:
                for _i, _it in enumerate(top_scores):
                    if (isinstance(_it, (list, tuple)) and len(_it) >= 2
                            and int(_it[0]) == hs and int(_it[1]) == as_):
                        score_rank = _i + 1
                        break
            score_top3_hit = 1 <= score_rank <= 3
            score_top5_hit = 1 <= score_rank <= 5
            score_top8_hit = 1 <= score_rank <= 8

            review = MatchReview(
                match_id=pred.get("match_id") or mid,  # 统一用预测完整 match_id（账本幂等键稳定）
                date=date_str,
                chain=self.chain_version,
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
                pnl=pred.get("pnl", 0),
                brier_model=brier_score(model_raw, actual_idx) if model_raw else None,
                brier_market=brier_score(market_fair, actual_idx) if market_fair else None,
                brier_djyy=brier_score(djyy_prob, actual_idx) if djyy_prob else None,
                brier_final=brier_score(final_prob, actual_idx),
                home_xg=pred.get("home_xg", 0),
                away_xg=pred.get("away_xg", 0),
                total_goals_actual=hs + as_,
                score_rank=score_rank,
                score_hit=score_rank > 0,
                score_top3_hit=score_top3_hit,
                score_top5_hit=score_top5_hit,
                score_top8_hit=score_top8_hit,
                # 双源比分 + 盘口信号（2026-08-05 结构升级）
                score_djyy_rank=int(pred.get("djyy_score_rank", -1) or -1),
                score_mc_rank=int(pred.get("mc_score_rank", -1) or -1),
                market_signal_hit=pred.get("market_signal_hit"),
                # 分层评价（2026-08-06 借鉴 MBS 方法论）
                log_loss_final=_log_loss(final_prob, actual_idx) if final_prob else None,
                rps_final=_rps(final_prob, actual_idx),
                rps_market=_rps(market_fair, actual_idx),
                rps_model=_rps(model_raw, actual_idx),
                goal_framework_hit=_goal_framework_hit(pred, hs + as_),
                prob_band=_prob_band(max(final_prob) if final_prob else 0),
                freshness_risk=(pred.get("freshness") or {}).get("risk", ""),
                as_of=pred.get("as_of", ""),
                kickoff=pred.get("kickoff", ""),
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

        # 比分命中分层（2026-08-05 闭环：推荐前三→前5的实证依据）
        # 注意 top1/3/5/8 是嵌套包含关系，命中 top1 也同时命中 top3/5/8
        score_stats = {
            "top1": sum(1 for r in reviews if r.score_rank == 1),
            "top3": sum(1 for r in reviews if r.score_top3_hit),
            "top5": sum(1 for r in reviews if r.score_top5_hit),
            "top8": sum(1 for r in reviews if r.score_top8_hit),
            "any": sum(1 for r in reviews if r.score_hit),
        }
        score_rank_hist = {}  # 命中位置分布: {1: n, 2: n, ...}
        for r in reviews:
            if r.score_rank > 0:
                score_rank_hist[r.score_rank] = score_rank_hist.get(r.score_rank, 0) + 1

        # 双源比分命中（2026-08-05）：DJYY vs MC 谁更准 → 数据决定融合权重
        def _src_hit(attr):
            items = [r for r in reviews if getattr(r, attr, -1) >= 0]
            return {"n": len(items), "hits": sum(1 for r in items if 1 <= getattr(r, attr) <= 5)}

        score_source_stats = {
            "djyy": _src_hit("score_djyy_rank"),
            "mc": _src_hit("score_mc_rank"),
        }
        # 盘口信号命中（累积验证盘口信号有效性）
        msig_items = [r for r in reviews if r.market_signal_hit is not None]
        market_signal_stats = {
            "n": len(msig_items),
            "hits": sum(1 for r in msig_items if r.market_signal_hit),
        }

        # 分层评价（2026-08-06 借鉴 MBS 8/3 批次自检）：结果/进球框架/比分 分开统计
        # LogLoss 概率质量（主指标，不随命中次数波动）
        _ll = [r.log_loss_final for r in reviews if r.log_loss_final is not None]
        # 进球框架命中（预测 Over2.5 方向 vs 实际总进球）
        _gf = [r for r in reviews if r.goal_framework_hit is not None]
        # 概率分段（MBS 实证 50-60% 段最危险 → 单独跟踪）
        _bands: dict[str, dict] = {}
        for r in reviews:
            if not r.prob_band:
                continue
            b = _bands.setdefault(r.prob_band, {"n": 0, "hits": 0})
            b["n"] += 1
            if r.hit:
                b["hits"] += 1
        # 新鲜度风险分层（验证护栏有效性）
        _fresh_groups: dict[str, dict] = {}
        for r in reviews:
            if not r.freshness_risk:
                continue
            g = _fresh_groups.setdefault(r.freshness_risk, {"n": 0, "hits": 0})
            g["n"] += 1
            if r.hit:
                g["hits"] += 1

        layered = {
            "log_loss_final": round(sum(_ll) / len(_ll), 4) if _ll else None,
            # RPS（2026-08-29）：新账本记录 rps_* 字段，老账本即时算
            "rps_final": round(sum(rps for r in reviews if (rps := getattr(r, "rps_final", None)) is not None)
                               / max(1, len([1 for r in reviews if getattr(r, "rps_final", None) is not None])), 4),
            "rps_market": round(sum(rps for r in reviews if (rps := getattr(r, "rps_market", None)) is not None)
                                / max(1, len([1 for r in reviews if getattr(r, "rps_market", None) is not None])), 4),
            "goal_framework": {
                "n": len(_gf),
                "hits": sum(1 for r in _gf if r.goal_framework_hit),
            },
            "prob_bands": {
                k: {"n": v["n"], "hit_rate": round(v["hits"] / v["n"], 4)}
                for k, v in sorted(_bands.items())
            },
            "freshness_groups": {
                k: {"n": v["n"], "hit_rate": round(v["hits"] / v["n"], 4)}
                for k, v in sorted(_fresh_groups.items())
            },
        }

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
            "score_stats": score_stats,
            "score_rank_hist": score_rank_hist,
            "score_source_stats": score_source_stats,
            "market_signal_stats": market_signal_stats,
            "layered": layered,
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
