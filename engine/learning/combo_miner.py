"""
N维组合挖掘 — 特征组合命中率追踪。

来源: jingcai-analysis 组合模式挖掘
核心思想:
  - 追踪 1~4 维特征组合的历史命中率
  - 例如: (联赛=英超, 主队排名<5, 赔率区间=1.5-2.0) → 命中率72%
  - 当某组合样本充足且命中率显著高于基线时，作为加分信号
  - 用于发现"模型没学到但数据里存在"的局部规律
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ComboConfig:
    """组合挖掘参数"""
    max_dims: int = 4              # 最大组合维度
    min_samples: int = 15          # 最低样本数才纳入
    min_lift: float = 0.05         # 最低提升（相对基线）
    baseline_rate: float = 0.45    # 全局基线命中率
    max_combos: int = 5000         # 最多存储组合数


@dataclass
class ComboStat:
    """单个组合的统计"""
    hits: int = 0
    total: int = 0

    @property
    def rate(self) -> float:
        return self.hits / max(self.total, 1)

    @property
    def lift(self) -> float:
        return self.rate  # 外部计算lift时减去baseline


class ComboMiner:
    """
    N维组合模式挖掘器。

    用法:
        miner = ComboMiner(state_path)
        # 记录结果
        features = {"league": "EPL", "home_rank_top": True, "odds_band": "1.5-2.0"}
        miner.record(features, won=True)
        # 查询
        boost = miner.get_boost(features)
        # boost > 0 表示该组合历史表现优于基线
    """

    def __init__(
        self,
        state_path: str | Path = "data/state/combo_stats.json",
        config: ComboConfig | None = None,
    ):
        self.cfg = config or ComboConfig()
        self.state_path = Path(state_path)
        self.stats: dict[str, ComboStat] = {}
        self._load()

    def _load(self) -> None:
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            for key, val in raw.items():
                self.stats[key] = ComboStat(hits=val["hits"], total=val["total"])

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            key: {"hits": s.hits, "total": s.total}
            for key, s in self.stats.items()
        }
        self.state_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def record(self, features: dict[str, str | bool | int], won: bool) -> None:
        """
        记录一场比赛结果，更新所有子组合。

        features: 离散化后的特征字典
        会自动生成 1维、2维...N维 的所有子组合并更新。
        """
        keys = sorted(features.keys())
        n = min(len(keys), self.cfg.max_dims)

        # 生成所有 1~n 维子组合
        for dim in range(1, n + 1):
            for combo in self._combinations(keys, dim):
                combo_key = self._make_key(features, combo)
                if combo_key not in self.stats:
                    if len(self.stats) >= self.cfg.max_combos:
                        continue  # 容量保护
                    self.stats[combo_key] = ComboStat()
                stat = self.stats[combo_key]
                stat.total += 1
                if won:
                    stat.hits += 1

        self.save()

    def get_boost(self, features: dict[str, str | bool | int]) -> float:
        """
        查询当前特征组合的最大正向提升。

        返回:
            lift值（命中率 - 基线），仅当样本充足且lift>min_lift时返回正值
            否则返回 0.0
        """
        keys = sorted(features.keys())
        n = min(len(keys), self.cfg.max_dims)
        best_lift = 0.0

        for dim in range(n, 0, -1):  # 从高维到低维
            for combo in self._combinations(keys, dim):
                combo_key = self._make_key(features, combo)
                stat = self.stats.get(combo_key)
                if stat is None or stat.total < self.cfg.min_samples:
                    continue
                lift = stat.rate - self.cfg.baseline_rate
                if lift > self.cfg.min_lift and lift > best_lift:
                    best_lift = lift

        return round(best_lift, 4)

    def get_top_combos(self, min_samples: int | None = None, top_n: int = 20) -> list[dict]:
        """获取表现最好的组合"""
        ms = min_samples or self.cfg.min_samples
        results = []
        for key, stat in self.stats.items():
            if stat.total < ms:
                continue
            lift = stat.rate - self.cfg.baseline_rate
            if lift > self.cfg.min_lift:
                results.append({
                    "combo": key,
                    "hits": stat.hits,
                    "total": stat.total,
                    "rate": round(stat.rate, 4),
                    "lift": round(lift, 4),
                })
        results.sort(key=lambda x: x["lift"], reverse=True)
        return results[:top_n]

    def get_worst_combos(self, min_samples: int | None = None, top_n: int = 20) -> list[dict]:
        """获取表现最差的组合（负向信号）"""
        ms = min_samples or self.cfg.min_samples
        results = []
        for key, stat in self.stats.items():
            if stat.total < ms:
                continue
            lift = stat.rate - self.cfg.baseline_rate
            if lift < -self.cfg.min_lift:
                results.append({
                    "combo": key,
                    "hits": stat.hits,
                    "total": stat.total,
                    "rate": round(stat.rate, 4),
                    "lift": round(lift, 4),
                })
        results.sort(key=lambda x: x["lift"])
        return results[:top_n]

    @staticmethod
    def _combinations(keys: list[str], dim: int) -> list[tuple[str, ...]]:
        """生成dim维组合"""
        from itertools import combinations
        return list(combinations(keys, dim))

    @staticmethod
    def _make_key(features: dict, combo: tuple[str, ...]) -> str:
        """生成组合键: 'league=EPL|odds_band=1.5-2.0'"""
        parts = [f"{k}={features[k]}" for k in combo]
        return "|".join(parts)
