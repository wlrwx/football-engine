"""比分候选归一化与双源合并（2026-10-11 从 main.py 抽出）

## 为什么抽出来

这段逻辑原先是 `run_daily_pipeline` 里定义在 for 循环体内的闭包
 `_norm_scores`，外加一坨内联的合并/重排。它是**纯函数**
（只依赖入参），却藏在 1200 行的编排函数里，既没法单独测试，
也没法在别的入口复用。

`engine/prediction/fusion.py` 已经是"纯函数 + 每步 trace"的形态，
这里照同一标准拆。**行为要求逐位不变** —— 比分候选直接影响账本的
`score_rank` / `score_hit`，改了会让历史命中率不可比。

## 口径（不要随意改）

- DJYY 是分析参考不是权威概率，与 MC 泊松模拟**不做加权融合**。
  实测：双源融合稀释命中率（top5 52.2%→48.7%），DJYY 纯源最优。
- DJYY 分布对强弱不敏感（碾压局 xG差>1.5 时实际 59% 高比分，
  top1 只给 12%），故对 xG差 ≥0.8 的场次做温和重排：
  高比分(≥3球)×1.3、1-1/0-0×0.85。
  walk-forward 113 场：top1 9.7%→13.3%，top5 52.2%→54.9%。
"""

from __future__ import annotations

from typing import Any, Iterable


def normalize_scores(ts: Iterable[Any] | None) -> list[tuple[int, int, float]]:
    """归一化比分候选：[(h,a,p),...] → [(h,a,p_norm),...]。

    无概率值时按排名衰减（1/(i+1)）。概率 ≤0 也按排名兜底，
    否则按总和归一时会出现负概率或除零。
    """
    if not ts:
        return []
    out: list[tuple[int, int, float]] = []
    for i, it in enumerate(ts):
        if isinstance(it, (list, tuple)) and len(it) >= 3:
            try:
                p = float(it[2])
            except (TypeError, ValueError):
                p = 0.0
            if p <= 0:
                p = 1.0 / (i + 1)
            out.append((int(it[0]), int(it[1]), p))
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            out.append((int(it[0]), int(it[1]), 1.0 / (i + 1)))
    s = sum(x[2] for x in out) or 1.0
    return [(h, a, p / s) for h, a, p in out]


def merge_score_candidates(
    djyy_ts: Iterable[Any] | None,
    mc_ts: Iterable[Any] | None,
    xdiff: float,
    limit: int = 8,
) -> tuple:
    """双源比分合并。

    返回 `(fused, source, djyy_norm, mc_norm)`：
    - `fused`  取前 limit 条，概率保留 4 位
    - `source` "djyy" / "mc" / None
    - 两路归一化结果原样返回，供账本分源统计命中率

    `xdiff` = |主队xG − 客队xG|，≥0.8 时对 DJYY 做温和重排。
    """
    djyy_norm = normalize_scores(djyy_ts)
    mc_norm = normalize_scores(mc_ts)

    if djyy_norm:
        reranked = list(djyy_norm)
        if xdiff >= 0.8:
            reranked = [
                (h, a, round(p * _rerank_factor(h, a), 6))
                for h, a, p in reranked
            ]
            reranked.sort(key=lambda x: -x[2])
        fused = [(h, a, round(p, 4)) for h, a, p in reranked[:limit]]
        return fused, "djyy", djyy_norm, mc_norm
    if mc_norm:
        return ([(h, a, round(p, 4)) for h, a, p in mc_norm[:limit]],
                "mc", djyy_norm, mc_norm)
    return None, None, djyy_norm, mc_norm


def _rerank_factor(h: int, a: int) -> float:
    """强弱分明场次的比分重排权重。口径见模块 docstring。"""
    if h + a >= 3:
        return 1.3
    if (h, a) in ((1, 1), (0, 0)):
        return 0.85
    return 1.0

