"""情境特征提取测试（2026-08-30）

_extract_context: 从 DJYY comparison/info/lineups 提取 store-only 情境特征。
这些特征不参与预测，只落盘累积，供日后验证平局信号——但提取本身必须可靠。
"""

from __future__ import annotations

from engine.sources.manager import _extract_context


def test_stakes_from_comparison():
    ctx = _extract_context({"stakes": {"home": "争冠", "away": "无欲无求"}}, None, None)
    assert ctx["stakes"] == {"home": "争冠", "away": "无欲无求"}


def test_referee_weather_coach_from_info():
    info = {"referee": "马宁", "weather": "小雨", "coach": {"home": "A", "away": "B"}}
    ctx = _extract_context(None, info, None)
    assert ctx["referee"] == "马宁"
    assert ctx["weather"] == "小雨"
    assert ctx["coach"] == {"home": "A", "away": "B"}


def test_lineups_formation_and_attackers():
    lineups = {
        "available": True,
        "home": {
            "formation": "4-3-3",
            "starting": [{"position": "F"}, {"position": "FW"}, {"position": "GK"},
                         {"position": "M"}, {"position": "D"}],
        },
        "away": {
            "formation": "5-4-1",
            "starting": [{"position": "ST"}, {"position": "GK"}, {"position": "D"}],
        },
    }
    ctx = _extract_context(None, None, lineups)
    assert ctx["home_formation"] == "4-3-3"
    assert ctx["home_starting_attackers"] == 2
    assert ctx["home_starters"] == 5
    assert ctx["away_formation"] == "5-4-1"
    assert ctx["away_starting_attackers"] == 1


def test_chinese_position_names():
    lineups = {"available": True, "home": {"starting": [{"position_zh": "前锋"}, {"position_zh": "门将"}]}}
    ctx = _extract_context(None, None, lineups)
    assert ctx["home_starting_attackers"] == 1


def test_all_missing_returns_empty():
    assert _extract_context(None, None, None) == {}


def test_unavailable_lineups_ignored():
    ctx = _extract_context(None, None, {"available": False, "home": {"formation": "x"}})
    assert ctx == {}


def test_zero_starter_counts_preserved():
    # 0 攻击手首发是有意义的信息（摆大巴），不能被过滤
    lineups = {"available": True, "home": {"formation": "5-4-1", "starting": [{"position": "D"}] * 5}}
    ctx = _extract_context(None, None, lineups)
    assert ctx["home_starting_attackers"] == 0
    assert ctx["home_starters"] == 5
