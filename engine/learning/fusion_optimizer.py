from __future__ import annotations
"""融合权重优化器 - 基于反事实重放的 champion/challenger 自进化

核心原理: predictions.json 已存 model_raw / market_fair / djyy_model_prob 三路原始概率,
优化器可以对任意权重向量做离线反事实重放 —— 无需重跑模型即可验证新权重的历史Brier。

每日 settlement 后:
1. 从 review_ledger 取滚动窗口
2. 由 per-source Brier 反推目标权重 (softmax of 1/brier)
3. 反事实验证: candidate vs champion 在 val 段比 Brier
4. 守卫栏: max_shift / bounds / min_improvement / rollback
5. promote 当且仅当 candidate 显著优于 champion
"""
import hashlib
import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from ..review.post_match import ReviewLedger, MatchReview, brier_score


@dataclass
class FusionWeights:
    """三路融合权重"""
    # 默认值向市场倾斜（2026-08-04 校准：109场ledger显示 market Brier 0.638 优于 model 0.714）
    model: float = 0.40
    market: float = 0.35
    djyy: float = 0.25

    def normalized(self) -> "FusionWeights":
        total = self.model + self.market + self.djyy
        if total <= 0:
            return FusionWeights(0.60, 0.25, 0.15)
        return FusionWeights(self.model / total, self.market / total, self.djyy / total)

    def to_dict(self) -> dict:
        return {"model": round(self.model, 4), "market": round(self.market, 4), "djyy": round(self.djyy, 4)}


@dataclass
class OptimizerDecision:
    """一次优化决策 - 进 SHA-256 哈希链"""
    timestamp: str
    action: str  # "promote" / "hold" / "rollback" / "cold_start"
    champion: dict
    candidate: dict | None
    metrics: dict
    reason: str
    guard_rails_applied: list
    prev_decision_hash: str
    sha256: str = ""

    def compute_hash(self):
        content = json.dumps({
            "timestamp": self.timestamp,
            "action": self.action,
            "champion": self.champion,
            "candidate": self.candidate,
            "metrics": self.metrics,
            "prev": self.prev_decision_hash,
        }, sort_keys=True)
        self.sha256 = hashlib.sha256(content.encode()).hexdigest()[:32]
        return self.sha256


class FusionOptimizer:
    """融合权重 champion/challenger 优化器"""

    def __init__(self, state_path: Path, ledger: ReviewLedger, config: dict | None = None):
        self.state_path = state_path
        self.ledger = ledger
        self.cfg = config or {}
        # 守卫栏参数 (全部外部化)
        self.min_samples = self.cfg.get("min_samples", 30)
        self.max_shift = self.cfg.get("max_weight_shift_per_day", 0.05)
        self.learning_rate = self.cfg.get("learning_rate", 0.3)
        self.min_improvement = self.cfg.get("min_brier_improvement", 0.005)
        self.rollback_degradation = self.cfg.get("rollback_degradation", 0.03)
        self.temperature = self.cfg.get("temperature", 0.5)
        self.decay_factor = self.cfg.get("decay_factor", 0.95)
        # 2026-08-13 修复：原 bounds 与数据事实相反（账本实测：模型 38.3% 最差 /
        # 市场 50.4% 最好），却强制 model≥0.30、market≤0.50 —— 把最优信号锁死在
        # 50% 以下，优化器 157 次全 hold 无法把权重交给市场。放开 bounds：
        # 市场可以到 0.90，模型/DJYY 从 0 起（无下限，允许完全剔除）。
        self.weight_bounds = self.cfg.get("weight_bounds", {
            "model": [0.00, 0.40],
            "market": [0.00, 0.90],
            "djyy": [0.00, 0.40],
        })
        self.val_matches = self.cfg.get("val_matches", 100)
        # 显著性阈值：晋升要求 improvement ≥ max(min_improvement, min_z × SE)
        # 2026-08-14：n=20 时 SE≈0.015，0.002-0.005 的阈值比噪声小一个量级，
        # 157 次决策 126 hold/16 promote 与随机游走无法区分。窗口提到 100
        # （changelog 建议 ≥100）后再叠加 min_z 显著性门槛。
        self.min_z = self.cfg.get("min_z", 1.96)
        # 日志
        self.log_path = state_path.parent / "optimizer_log.jsonl"

    def get_champion(self, default: FusionWeights | None = None) -> FusionWeights:
        """预测时调用: 获取当前冠军权重（读盘时同样强制边界，杜绝越界状态）。"""
        default = default or FusionWeights()
        if not self.state_path.exists():
            return default
        try:
            state = json.loads(self.state_path.read_text())
            w = state.get("champion", {})
            champ = FusionWeights(
                model=w.get("model", default.model),
                market=w.get("market", default.market),
                djyy=w.get("djyy", default.djyy),
            )
            # 读盘时也裁剪到配置边界：2026-08-13 曾出现 model=0.10(<0.15)、
            # market=0.75(>0.6) 的越界状态静默运行，且无决策链日志背书。
            b = self.weight_bounds
            return FusionWeights(
                model=max(b["model"][0], min(b["model"][1], champ.model)),
                market=max(b["market"][0], min(b["market"][1], champ.market)),
                djyy=max(b["djyy"][0], min(b["djyy"][1], champ.djyy)),
            )
        except Exception:
            return default

    def step(self) -> OptimizerDecision:
        """每日 settlement 调用: 完整闭环优化一步"""
        prev_hash = self._last_decision_hash()
        champion = self.get_champion()

        # 冷启动守卫
        n = self.ledger.count
        if n < self.min_samples:
            decision = OptimizerDecision(
                timestamp=datetime.now().isoformat(),
                action="cold_start",
                champion=champion.to_dict(),
                candidate=None,
                metrics={"n_samples": n, "min_required": self.min_samples},
                reason=f"样本不足({n}<{self.min_samples}), 保持默认权重",
                guard_rails_applied=["cold_start"],
                prev_decision_hash=prev_hash,
            )
            decision.compute_hash()
            self._log(decision)
            return decision

        # train/val 切分
        train, val = self.ledger.split_train_val(self.val_matches)
        if not val:
            val = train[-self.val_matches:]

        # 1. 提议新权重
        candidate = self._propose(train, champion)

        # 2. 守卫栏: max_shift 裁剪
        guard_rails = []
        candidate = self._clip_shift(champion, candidate, guard_rails)

        # 3. 守卫栏: weight_bounds
        candidate = self._clip_bounds(candidate, guard_rails)

        # 4. 归一化
        candidate = candidate.normalized()

        # 5. 反事实验证（配对显著性：improvement 必须超过 z × SE 才算显著）
        champ_brier = self._counterfactual_brier(val, champion)
        cand_brier = self._counterfactual_brier(val, candidate)
        improvement = champ_brier - cand_brier  # >0 = candidate更好

        metrics = {
            "champion_brier": round(champ_brier, 5),
            "candidate_brier": round(cand_brier, 5),
            "improvement": round(improvement, 5),
            "n_train": len(train),
            "n_val": len(val),
        }

        # 6. 晋升判定：必须显著（2026-08-14 起）
        _imp, _se, _nval = self._paired_improvement(val, champion, candidate)
        metrics["improvement_se"] = round(_se, 5)
        metrics["n_val_paired"] = _nval
        _threshold = self.min_improvement
        if _se > 0:
            _threshold = max(self.min_improvement, self.min_z * _se)
        metrics["significance_threshold"] = round(_threshold, 5)
        if improvement >= _threshold:
            action = "promote"
            reason = (f"candidate Brier {cand_brier:.4f} < champion {champ_brier:.4f} "
                      f"(Δ={improvement:.4f} ≥ {_threshold:.4f}, z={self.min_z}, SE={_se:.4f})")
            new_champion = candidate
        else:
            action = "hold"
            reason = (f"improvement {improvement:.4f} < threshold {_threshold:.4f} "
                      f"(SE={_se:.4f}，未达 {self.min_z}σ 显著性)")
            new_champion = champion

        # 7. 回滚检查: champion 近期是否退化
        if action == "hold" and len(train) >= self.min_samples:
            recent_brier = self._counterfactual_brier(val, champion)
            older_brier = self._counterfactual_brier(train[-self.val_matches * 2:-self.val_matches] if len(train) > self.val_matches * 2 else train, champion)
            if recent_brier - older_brier > self.rollback_degradation:
                # 尝试回滚到 previous
                prev_w = self._load_previous()
                if prev_w:
                    prev_brier = self._counterfactual_brier(val, prev_w)
                    if prev_brier < recent_brier:
                        action = "rollback"
                        new_champion = prev_w
                        reason = f"champion退化({recent_brier:.4f}>{older_brier:.4f}), 回滚到previous({prev_brier:.4f})"
                        guard_rails.append("rollback_triggered")

        # 8. 持久化
        self._save_state(new_champion, champion)

        decision = OptimizerDecision(
            timestamp=datetime.now().isoformat(),
            action=action,
            champion=new_champion.to_dict(),
            candidate=candidate.to_dict(),
            metrics=metrics,
            reason=reason,
            guard_rails_applied=guard_rails,
            prev_decision_hash=prev_hash,
        )
        decision.compute_hash()
        self._log(decision)

        return decision

    def _propose(self, train: list[MatchReview], champion: FusionWeights) -> FusionWeights:
        """由 per-source 指数加权 Brier 反推目标权重"""
        # 计算各源的指数加权 Brier
        source_briers = {"model": [], "market": [], "djyy": []}
        for i, r in enumerate(train):
            weight = self.decay_factor ** (len(train) - 1 - i)  # 近期权重高
            if r.brier_model is not None:
                source_briers["model"].append((r.brier_model, weight))
            if r.brier_market is not None:
                source_briers["market"].append((r.brier_market, weight))
            if r.brier_djyy is not None:
                source_briers["djyy"].append((r.brier_djyy, weight))

        # 加权平均 Brier
        avg_briers = {}
        for src, pairs in source_briers.items():
            if pairs:
                total_w = sum(w for _, w in pairs)
                avg_briers[src] = sum(b * w for b, w in pairs) / total_w
            else:
                avg_briers[src] = 1.0  # 无数据则给最差分

        # softmax of 1/brier (Brier越低→权重越高)
        scores = {src: 1.0 / max(b, 0.01) for src, b in avg_briers.items()}
        max_score = max(scores.values())
        exp_scores = {src: math.exp((s - max_score) / self.temperature) for src, s in scores.items()}
        total_exp = sum(exp_scores.values())
        target = FusionWeights(
            model=exp_scores["model"] / total_exp,
            market=exp_scores["market"] / total_exp,
            djyy=exp_scores["djyy"] / total_exp,
        )

        # 平滑: new = (1-lr)*champion + lr*target
        lr = self.learning_rate
        return FusionWeights(
            model=(1 - lr) * champion.model + lr * target.model,
            market=(1 - lr) * champion.market + lr * target.market,
            djyy=(1 - lr) * champion.djyy + lr * target.djyy,
        )

    def _counterfactual_brier(self, reviews: list[MatchReview], w: FusionWeights) -> float:
        """反事实重放: 用权重w重新融合三路概率, 计算Brier
        
        这是闭环廉价的关键 —— 无需重跑模型。
        缺失的源按剩余权重归一化。
        """
        if not reviews:
            return 1.0
        total_brier, n = self._sum_brier(reviews, w)
        return total_brier / max(1, n)

    def _sum_brier(self, reviews: list[MatchReview], w: FusionWeights) -> tuple[float, int]:
        """返回 (总Brier, 有效场次数)。"""
        total_brier = 0.0
        n = 0
        for r in reviews:
            fused = self._fused_probs(r, w)
            if fused is None:
                continue
            total_brier += brier_score(fused, r.actual_idx)
            n += 1
        return total_brier, n

    def _fused_probs(self, r: MatchReview, w: FusionWeights) -> list[float] | None:
        """用权重 w 对单场三路概率反事实融合，返回 [h,d,a] 或 None。

        2026-10-23 修复（评审 P1#8）：原实现自己写了一份"三源加权平均"，
        与生产 `fuse_probabilities` 是两套数学（生产有 djyy 置信门槛、
        方向分歧惩罚、归一化），等于在优化一个生产里不存在的函数。
        现在直接复用生产融合函数，并**关闭全部后处理开关** —— 账本里没有
        same_odds/combo/LGBM 等后处理输入，重放不了；反事实的语义就是
        "源融合阶段"。
        """
        from engine.prediction.fusion import (
            DEFAULT_POST_FUSION,
            FusionInput,
            fuse_probabilities,
        )

        if not (r.model_raw and len(r.model_raw) >= 3):
            return None
        market = (
            tuple(r.market_fair)
            if r.market_fair and len(r.market_fair) >= 3
            else None
        )
        djyy = None
        if r.djyy_prob and len(r.djyy_prob) >= 3 and any(r.djyy_prob):
            djyy = {
                "home": r.djyy_prob[0],
                "draw": r.djyy_prob[1],
                "away": r.djyy_prob[2],
            }
        inp = FusionInput(
            model_probs=tuple(r.model_raw),
            market_probs=market,
            djyy_probs=djyy,
            cfg={
                "model_weight": w.model,
                "market_weight": w.market,
                "djyy_weight": w.djyy,
            },
            post_fusion={k: False for k in DEFAULT_POST_FUSION},
        )
        return list(fuse_probabilities(inp).probs)

    def _paired_improvement(self, reviews: list[MatchReview], champ: FusionWeights, cand: FusionWeights):
        """配对显著性：返回 (mean improvement, SE, n)。

        improvement_i = champ_brier_i - cand_brier_i（>0 = cand 更好）。
        只有 improvement > z × SE 才视为显著（否则是噪声）。
        """
        diffs = []
        for r in reviews:
            fc = self._fused_probs(r, champ)
            fd = self._fused_probs(r, cand)
            if fc is None or fd is None:
                continue
            diffs.append(brier_score(fc, r.actual_idx) - brier_score(fd, r.actual_idx))
        n = len(diffs)
        if n == 0:
            return 0.0, 0.0, 0
        mean = sum(diffs) / n
        var = sum((d - mean) ** 2 for d in diffs) / max(1, n - 1)
        se = math.sqrt(var / n) if var > 0 else 0.0
        return mean, se, n

    def _clip_shift(self, champion: FusionWeights, candidate: FusionWeights,
                    guard_rails: list) -> FusionWeights:
        """单日最大位移裁剪"""
        clipped = False
        result = FusionWeights(
            model=max(champion.model - self.max_shift, min(champion.model + self.max_shift, candidate.model)),
            market=max(champion.market - self.max_shift, min(champion.market + self.max_shift, candidate.market)),
            djyy=max(champion.djyy - self.max_shift, min(champion.djyy + self.max_shift, candidate.djyy)),
        )
        if (abs(result.model - candidate.model) > 0.001 or
            abs(result.market - candidate.market) > 0.001 or
            abs(result.djyy - candidate.djyy) > 0.001):
            clipped = True
        if clipped:
            guard_rails.append("max_shift_clipped")
        return result

    def _clip_bounds(self, w: FusionWeights, guard_rails: list) -> FusionWeights:
        """权重边界裁剪"""
        bounds = self.weight_bounds
        clipped = False
        result = FusionWeights(
            model=max(bounds["model"][0], min(bounds["model"][1], w.model)),
            market=max(bounds["market"][0], min(bounds["market"][1], w.market)),
            djyy=max(bounds["djyy"][0], min(bounds["djyy"][1], w.djyy)),
        )
        if (abs(result.model - w.model) > 0.001 or
            abs(result.market - w.market) > 0.001 or
            abs(result.djyy - w.djyy) > 0.001):
            clipped = True
        if clipped:
            guard_rails.append("bounds_clipped")
        return result

    def _save_state(self, new_champion: FusionWeights, old_champion: FusionWeights):
        """持久化冠军权重 (保留previous供回滚)"""
        state = {
            "champion": new_champion.to_dict(),
            "previous": old_champion.to_dict(),
            "updated_at": datetime.now().isoformat(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2))

    def _load_previous(self) -> FusionWeights | None:
        if not self.state_path.exists():
            return None
        try:
            state = json.loads(self.state_path.read_text())
            prev = state.get("previous")
            if prev:
                w = FusionWeights(prev.get("model", 0.6), prev.get("market", 0.25), prev.get("djyy", 0.15))
                return w.normalized()
        except Exception:
            pass
        return None

    def _last_decision_hash(self) -> str:
        if not self.log_path.exists():
            return "genesis"
        lines = self.log_path.read_text().strip().split("\n")
        if lines and lines[-1].strip():
            try:
                return json.loads(lines[-1]).get("sha256", "unknown")
            except Exception:
                pass
        return "genesis"

    def _log(self, decision: OptimizerDecision):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")
