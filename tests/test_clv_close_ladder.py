"""CLV 收盘基准阶梯的口径（2026-10-28）

`clv_pinnacle.json` 里 258 条记录全部 `benchmark=b365`，一开始被当成匹配 bug。
查完源数据才发现是结构性的：`new/` 文件 2026 年的行 PSCH 全为空
（BRA 2026 年 319 行，PSCH 非空 0），退到 B365 收盘是唯一选择。

既然口径被迫降级，就必须把它写死成契约并出现在报告里，否则
「vs 尖锐收盘」这句话会持续误导权重与策略判断。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import clv_pinnacle as cp


def test_ladder_prefers_pinnacle_closing():
    row = {"psc_h": 2.10, "psc_d": 3.4, "psc_a": 3.5,
           "b365c_h": 2.05, "b365c_d": 3.3, "b365c_a": 3.6}
    assert cp.pick_close(row, 0) == (2.10, "pinnacle")


def test_ladder_falls_back_to_b365_when_pinnacle_missing():
    row = {"psc_h": None, "psc_d": None, "psc_a": None,
           "b365c_h": 1.71, "b365c_d": 3.6, "b365c_a": 4.75}
    assert cp.pick_close(row, 0) == (1.71, "b365")


def test_ladder_falls_back_to_max_then_avg():
    assert cp.pick_close({"maxc_a": 4.75}, 2) == (4.75, "max_market")
    assert cp.pick_close({"avgc_d": 3.66}, 1) == (3.66, "avg_market")


def test_ladder_returns_none_when_no_close():
    assert cp.pick_close({"psc_h": None, "b365c_h": None}, 0) == (None, None)


def test_selection_index_picks_correct_side():
    row = {"psc_h": 2.0, "psc_d": 3.4, "psc_a": 3.9}
    assert cp.pick_close(row, 2) == (3.9, "pinnacle")


def test_ladder_order_is_contract():
    """顺序本身是契约：改顺序等于改 CLV 口径，必须有人显式改这个测试。"""
    names = [name for _, name in cp._CLOSE_LADDER]
    assert names == ["pinnacle", "b365", "max_market", "avg_market"]
