"""issue #1：无盘口场次的推荐必须在卡片上标明「不可投注」。

## 背景（外部用户 dzd1234 报的 issue）

用户看到"竞彩未开盘，但生成的预测数据里有胜平负推荐"。

排查后发现**三层里两层早就防住了**：
- 下注层：`engine/main.py:1016` 对 `odds_synthetic` 一律 `continue` 不出注，
  注释里记着曾因此对 8 笔共 ¥67,353 押在模拟赔率上（基本全亏）
- 自检层：`engine/selfcheck.py:124` 有专门告警

**唯一漏的是展示层**：`_pred_pick()` 渲染出"预测 主胜 75%"，
卡片上看不出这场其实没有真实盘口——读者会以为能照着买。
合成赔率是 `1/(final*1.05)` 反推的，去水后≈模型自己的输出，
把它佯装成市场结论才是问题所在。

所以这里锁住：合成赔率必须带「⚠ 无盘口·不可投注」标记，真实盘口不带。
"""

from __future__ import annotations

import pytest

pytest.importorskip("engine.build_site")

from engine.build_site import _pred_pick  # noqa: E402


def test_synthetic_odds_gets_not_bettable_badge():
    """合成赔率必须有不可投注标记（issue #1 的核心）。"""
    p = {"direction": "home", "home_win_prob": 0.75, "odds_synthetic": True}
    html = _pred_pick(p)
    assert "无盘口" in html, "合成赔率场次未标注无盘口"
    assert "不可投注" in html, "合成赔率场次未标注不可投注"
    # 但预测本身仍要显示（不是把推荐藏掉）
    assert "主胜" in html and "75%" in html


def test_real_odds_has_no_badge():
    """真实盘口不能被误标（误标比漏标更糟——会让真推荐显得不可信）。"""
    p = {"direction": "home", "home_win_prob": 0.75, "odds_synthetic": False}
    html = _pred_pick(p)
    assert "无盘口" not in html
    assert "不可投注" not in html
    assert "主胜" in html and "75%" in html


def test_badge_present_without_direction_field():
    """direction 缺失（回退 argmax 分支）时也要带标记。

    两条返回路径都要防——只改一条等于没改。
    """
    p = {"home_win_prob": 0.75, "draw_prob": 0.15, "away_win_prob": 0.10,
         "odds_synthetic": True}
    html = _pred_pick(p)
    assert "无盘口" in html and "不可投注" in html
    assert "主胜" in html


def test_badge_survives_draw_and_away():
    """平局/客胜方向同样要标（不同 label 分支）。"""
    for d, key, label in (("draw", "draw_prob", "平局"),
                          ("away", "away_win_prob", "客胜")):
        p = {"direction": d, key: 0.4, "odds_synthetic": True}
        html = _pred_pick(p)
        assert label in html, f"{d} 方向标签丢失"
        assert "不可投注" in html, f"{d} 方向漏标"


def test_badge_explains_why():
    """标记里要解释原因，不能只是个红字。

    读者需要知道"为什么不能买"——否则只会以为是系统坏了。
    title 里必须提到负 EV / 反推 / 不下注。
    """
    p = {"direction": "home", "home_win_prob": 0.75, "odds_synthetic": True}
    html = _pred_pick(p)
    assert "title=" in html, "标记缺少说明 tooltip"
    assert "负 EV" in html or "反推" in html, "未解释原因"
