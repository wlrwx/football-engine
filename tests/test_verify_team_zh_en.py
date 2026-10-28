"""中英队名候选的交叉验证（2026-10-28）

背景：CLV 面板曾因中文队名解不出英文（`no_en` 381 注）而丢掉 30% 样本。
手工补 138 条映射必然出错，而 CLV 是判据——把 A 队收盘价配到 B 队身上
比缺样本更糟，所以先用「同联赛 + 同日 ±1 + 正确主客侧」的 fdc 真实行验证。

这里锁四件事：
1. 正确候选（能对上真实行）通过，并给出证据条数
2. 指错队的候选必须被拒（不能因为「名字里有 Manchester」就放过）
3. 对手可解析且对不上该行时拒绝（防同名球队串行）
4. 联赛不在 LEAGUE_MAP 里时不做任何推断
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import verify_team_zh_en as v


def _preds():
    return [
        {"date": "2026-10-20", "competition": "瑞典超", "home": "厄格里特", "away": "佐加顿斯"},
        {"date": "2026-10-24", "competition": "瑞典超", "home": "佐加顿斯", "away": "代格福什"},
    ]


def _fdc():
    return {
        ("new_SWE", "2026-10-20"): [
            {"src": "new_SWE", "date": "2026-10-20",
             "home_en": "Orgryte", "away_en": "Djurgarden"},
        ],
        ("new_SWE", "2026-10-24"): [
            {"src": "new_SWE", "date": "2026-10-24",
             "home_en": "Djurgarden", "away_en": "Degerfors"},
        ],
    }


def test_correct_candidate_is_verified_with_evidence():
    verified, rejected = v.verify(
        {"佐加顿斯": ["Djurgarden"]}, _preds(), _fdc(),
        {"厄格里特": "Orgryte", "代格福什": "Degerfors"},
    )
    assert not rejected
    assert verified["佐加顿斯"]["en"] == "Djurgarden"
    assert verified["佐加顿斯"]["n"] == 2, "两场都应留下证据"


def test_wrong_team_is_rejected():
    verified, rejected = v.verify(
        {"佐加顿斯": ["Kalmar"]}, _preds(), _fdc(), {},
    )
    assert not verified
    assert rejected and rejected[0]["reason"] == "no_fdc_row"


def test_opponent_mismatch_rejects_to_avoid_cross_team_match():
    """候选能对上主队，但对手（已知）不在该行 → 说明配错了场次，必须拒绝。

    注意语义：对手未知时只用「队名+日期+联赛文件+主客侧」判定（证据弱一档，
    但仍是真实行）；只有对手**已知且对不上**才是否决条件。所以这里把两个对手
    都填成错的，确保两场都被拒——否则第二场会因对手未知而被接受。
    """
    verified, rejected = v.verify(
        {"佐加顿斯": ["Djurgarden"]}, _preds(), _fdc(),
        {"厄格里特": "Kalmar", "代格福什": "Kalmar"},
    )
    assert not verified, "对手对不上时不能采信"
    assert rejected


def test_league_not_in_map_is_not_inferred():
    preds = [{"date": "2026-10-20", "competition": "K1联赛",
              "home": "首尔FC", "away": "蔚山现代"}]
    verified, rejected = v.verify({"首尔FC": ["Seoul"]}, preds, {}, {})
    assert not verified and rejected[0]["reason"] == "no_fdc_row"


def test_date_tolerance_is_one_day():
    """fdc 用当地日期，竞彩用北京日期，跨零点会差一天。"""
    verified, _ = v.verify(
        {"佐加顿斯": ["Djurgarden"]},
        [{"date": "2026-10-20", "competition": "瑞典超",
          "home": "佐加顿斯", "away": "代格福什"}],
        {("new_SWE", "2026-10-21"): [
            {"src": "new_SWE", "date": "2026-10-21",
             "home_en": "Djurgarden", "away_en": "Degerfors"}]},
        {},
    )
    assert verified["佐加顿斯"]["n"] == 1


def test_dictionary_group_is_loadable_by_clv():
    """写回的分组必须能被 clv_pinnacle 的 seed 读取逻辑识别（只吃 dict）。"""
    import json

    data = json.loads((ROOT / "assets" / "team_zh_en.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    groups = [k for k, val in data.items() if isinstance(val, dict)]
    assert v.GROUP in groups
    assert all(isinstance(en, str) and en for en in data[v.GROUP].values())
