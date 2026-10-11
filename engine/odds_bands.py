"""赔率分层口径的**单一真源**（2026-10-11 新增）

## 为什么要有这个模块

修复前系统里有**三套互不一致**的赔率分层：

| 位置 | 分层 |
|---|---|
| `review/post_match.py::_odds_band`（账本） | <1.5 / 1.5-2.0 / 2.0-3.0 / 3.0+ |
| `review/ev_report.py::layer_of` | <1.5 / 1.5-1.8 / 1.8-2.2 / 2.2-3.0 / ≥3.0 |
| `config/strategy.json` favorite_band 闸门 | 边界 1.8 |

账本的 `1.5-2.0` 桶**横跨**了闸门边界 1.8，也横跨了 ev_report 的
L2(1.5-1.8) 与 L3(1.8-2.2)。

后果不是"不好看"，而是**L3 缺口在账本里结构性不可见**：
任何直接按 `odds_band` 做的聚合，都不可能看出"odds 1.8-2.2 这一段
系统性亏损，而闸门恰好放开了它"。这个缺口是2026-10-11 才通过
ev_report 的独立口径发现的——如果账本自己能看见，本可以早两个月发现。

## 口径定义

分层边界必须与下注闸门对齐，否则"分层"这个动作没有决策含义：

- L1 <1.5 / L2 1.5-1.8 → 闸门禁投区（favorite_band.odds_below = 1.8）
- L3 1.8-2.2 / L4 2.2-3.0 / L5 ≥3.0 → 闸门放行区

任何改动分层边界的地方都必须改这里，不要再各自写一份。
"""

from __future__ import annotations

from typing import Any

# 与 config/strategy.json 的 favorite_band.odds_below 保持一致。
# 该值可配置，若闸门边界被改，这里应同步（config_audit 会提示未接线键）。
LAYER_EDGES: tuple[tuple[float, str], ...] = (
    (1.5, "L1 大热(<1.5)"),
    (1.8, "L2 热(1.5-1.8)"),
    (2.2, "L3 中(1.8-2.2)"),
    (3.0, "L4 冷(2.2-3.0)"),
)
LAYER_TOP = "L5 深冷(≥3.0)"


def layer_of(odds: float | None) -> str:
    """赔率 → L1-L5 层名。odds 缺失/非法返回 "unknown"。"""
    if odds is None:
        return "unknown"
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return "unknown"
    if o <= 1.0:
        return "unknown"
    for edge, label in LAYER_EDGES:
        if o < edge:
            return label
    return LAYER_TOP


def layer_short(odds: float | None) -> str:
    """层名的短码（L1..L5），便于聚合统计的键。"""
    full = layer_of(odds)
    return full.split(" ", 1)[0] if full != "unknown" else "unknown"


def gate_blocked_layer_names(band_cfg: dict[str, Any] | None = None) -> set[str]:
    """闸门禁投覆盖的层名集合（由 favorite_band 配置推导，不硬编码）。"""
    cfg = band_cfg or {}
    if not cfg.get("enabled", True):
        return set()
    edge = float(cfg.get("odds_below", 1.8))
    return {label for e, label in LAYER_EDGES if e <= edge}

