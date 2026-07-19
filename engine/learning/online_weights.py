"""
在线权重学习 — 根据近期预测表现动态调整模型权重。

核心思想:
  - 追踪每个子模型最近N场的Brier Score / 命中率
  - 表现好的模型获得更高权重，差的降权
  - 使用指数衰减（近期表现权重更大）
  - 设置最低权重下限，防止某模型完全被忽略
  - 冷启动: 样本不足时使用配置文件的静态权重
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OnlineWeightConfig:
    """在线权重学习参数"""
    window_size: int = 100          # 追踪最近N场
    decay_factor: float = 0.95      # 指数衰减因子（越小越重视近期）
    min_weight: float = 0.10        # 单模型最低权重
    max_weight: float = 0.85        # 单模型最高权重
    min_samples: int = 20           # 低于此样本数用静态权重
    learning_rate: float = 0.1      # 权重更新步长
    metric: str = "brier"           # 评估指标: "brier" / "hit_rate" / "log_loss"


@dataclass
class ModelPerformance:
    """单模型表现追踪"""
    scores: list[float] = field(default_factory=list)   # 每场的score（越小越好）
    hits: int = 0
    total: int = 0
    weighted_brier: float = 0.0    # 指数加权Brier
    weight_sum: float = 0.0

    def add(self, brier: float, hit: bool, decay: float) -> None:
        self.scores.append(brier)
        if len(self.scores) > 200:
            self.scores = self.scores[-200:]
        self.total += 1
        if hit:
            self.hits += 1
        # 指数加权
        self.weighted_brier = decay * self.weighted_brier + (1 - decay) * brier
        self.weight_sum = decay * self.weight_sum + (1 - decay)

    @property
    def avg_brier(self) -> float:
        """指数加权平均Brier Score"""
        if self.weight_sum < 1e-9:
            return 0.25  # 默认（随机水平）
        return self.weighted_brier / self.weight_sum

    @property
    def hit_rate(self) -> float:
        return self.hits / max(self.total, 1)


class OnlineWeightLearner:
    """
    在线权重学习器。

    用法:
        learner = OnlineWeightLearner(state_path)
        # 获取当前动态权重
        weights = learner.get_weights(default={"dixon_coles": 0.6, "monte_carlo": 0.4})
        # 结算后更新
        learner.update("dixon_coles", brier=0.18, hit=True)
        learner.update("monte_carlo", brier=0.22, hit=False)
    """

    def __init__(
        self,
        state_path: str | Path = "data/state/online_weights.json",
        config: OnlineWeightConfig | None = None,
    ):
        self.cfg = config or OnlineWeightConfig()
        self.state_path = Path(state_path)
        self.models: dict[str, ModelPerformance] = {}
        self.current_weights: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.current_weights = raw.get("weights", {})
            for name, data in raw.get("performances", {}).items():
                perf = ModelPerformance(
                    scores=data.get("scores", []),
                    hits=data.get("hits", 0),
                    total=data.get("total", 0),
                    weighted_brier=data.get("weighted_brier", 0.0),
                    weight_sum=data.get("weight_sum", 0.0),
                )
                self.models[name] = perf

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "weights": self.current_weights,
            "performances": {
                name: {
                    "scores": perf.scores[-100:],  # 只存最近100
                    "hits": perf.hits,
                    "total": perf.total,
                    "weighted_brier": perf.weighted_brier,
                    "weight_sum": perf.weight_sum,
                }
                for name, perf in self.models.items()
            },
        }
        self.state_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_weights(self, default: dict[str, float] | None = None) -> dict[str, float]:
        """
        获取当前动态权重。

        如果样本不足，返回default（静态权重）。
        """
        if default is None:
            default = {"dixon_coles": 0.6, "monte_carlo": 0.4}

        # 冷启动检查
        min_total = min(
            (p.total for p in self.models.values()), default=0
        )
        if min_total < self.cfg.min_samples or not self.current_weights:
            return default

        return dict(self.current_weights)

    def update(self, model_name: str, brier: float, hit: bool) -> None:
        """
        更新单模型表现并重新计算权重。

        Args:
            model_name: 模型名 (e.g. "dixon_coles", "monte_carlo")
            brier: 该场Brier Score (预测概率与实际结果的MSE)
            hit: 是否命中（预测最大概率选项=实际结果）
        """
        if model_name not in self.models:
            self.models[model_name] = ModelPerformance()

        self.models[model_name].add(brier, hit, self.cfg.decay_factor)
        self._recalculate_weights()
        self.save()

    def update_batch(
        self, results: list[dict]
    ) -> None:
        """
        批量更新（结算时调用）。

        results: [{model: str, brier: float, hit: bool}, ...]
        """
        for r in results:
            name = r["model"]
            if name not in self.models:
                self.models[name] = ModelPerformance()
            self.models[name].add(r["brier"], r["hit"], self.cfg.decay_factor)

        self._recalculate_weights()
        self.save()

    def _recalculate_weights(self) -> None:
        """基于表现重新计算权重"""
        if len(self.models) < 2:
            return

        names = list(self.models.keys())

        # 检查样本量
        for name in names:
            if self.models[name].total < self.cfg.min_samples:
                return  # 样本不足，不更新

        if self.cfg.metric == "brier":
            # Brier越小越好 → 取倒数作为"实力分"
            raw_scores = {}
            for name in names:
                brier = self.models[name].avg_brier
                # 防止除零，Brier范围[0,1]
                raw_scores[name] = 1.0 / max(brier, 0.01)
        elif self.cfg.metric == "hit_rate":
            raw_scores = {name: self.models[name].hit_rate for name in names}
        elif self.cfg.metric == "log_loss":
            raw_scores = {}
            for name in names:
                brier = self.models[name].avg_brier
                raw_scores[name] = 1.0 / max(brier, 0.01)
        else:
            raw_scores = {name: 1.0 / max(self.models[name].avg_brier, 0.01)
                         for name in names}

        # Softmax归一化（温度=1）
        max_score = max(raw_scores.values())
        exp_scores = {n: math.exp(s - max_score) for n, s in raw_scores.items()}
        total_exp = sum(exp_scores.values())
        new_weights = {n: e / total_exp for n, e in exp_scores.items()}

        # 应用上下限
        for n in new_weights:
            new_weights[n] = max(self.cfg.min_weight, min(self.cfg.max_weight, new_weights[n]))

        # 重新归一化
        total_w = sum(new_weights.values())
        new_weights = {n: w / total_w for n, w in new_weights.items()}

        # 平滑更新（避免权重剧烈跳变）
        if self.current_weights:
            lr = self.cfg.learning_rate
            for n in new_weights:
                old = self.current_weights.get(n, new_weights[n])
                new_weights[n] = (1 - lr) * old + lr * new_weights[n]
            # 再次归一化
            total_w = sum(new_weights.values())
            new_weights = {n: w / total_w for n, w in new_weights.items()}

        self.current_weights = new_weights

    def status_report(self) -> dict:
        """状态摘要"""
        report = {"weights": self.current_weights, "models": {}}
        for name, perf in self.models.items():
            report["models"][name] = {
                "total": perf.total,
                "hit_rate": round(perf.hit_rate, 4),
                "avg_brier": round(perf.avg_brier, 4),
            }
        return report
