"""译名缺口扫描的行为测试（2026-11-17）。

核心纪律：扫描必须复用生产匹配（不造第二套口径），且只报「同场另一队同名」
的高信号对——互含、梯队、纯不同场都不许当成译名缺口，否则又会诱导有人去
「优化匹配」动到口径边缘。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("alias_gap_scan", ROOT / "scripts" / "alias_gap_scan.py")
scan = importlib.util.module_from_spec(SPEC)
sys.modules["alias_gap_scan"] = scan
SPEC.loader.exec_module(scan)


def _pred(h, a):
    return {(h, a): {"home_team": h, "away_team": a}}


def _pool(h, a):
    return [{"home": h, "away": a, "sid": "x", "kick_ts": 0, "odds": [2.0, 3.0, 4.0]}]


def test_finds_notts_county_shape_gap():
    c = scan.detect_gap_candidates(_pred("伯顿", "诺茨郡X"),
                                   _pool("伯顿", "诺斯君X"), matched=set())
    assert any({"诺斯君X", "诺茨郡X"} == set(k) for k in c), c


def test_containment_pair_is_not_reported():
    # 互含（拉赫蒂/拉赫蒂FC 类）属生产既有规则面，不该进译名缺口清单
    c = scan.detect_gap_candidates(_pred("奥胡斯", "拉赫蒂"), _pool("奥胡斯", "拉赫蒂FC"),
                                   matched=set())
    assert all({"拉赫蒂", "拉赫蒂FC"} != set(k) for k in c), "互含属既有规则面，不算译名缺口"


def test_squad_variant_is_not_a_gap():
    # 无梯队守卫时这对会以 ratio≈0.57 混进清单（加拉塔萨雷 vs 加拉塔萨U19 并非同场）
    c = scan.detect_gap_candidates(_pred("里尔", "加拉塔萨U19"), _pool("里尔", "加拉塔萨雷"),
                                   matched=set())
    assert c == {}, "青年队差异不是译名（10-21 实测会一注变两注）"


def test_different_fixture_not_flagged():
    c = scan.detect_gap_candidates(_pred("皇马", "巴萨"), _pool("国米", "尤文"), matched=set())
    assert c == {}


def test_matched_prediction_not_counted_as_lost():
    pred = _pred("伯顿", "诺斯郡")
    c = scan.detect_gap_candidates(pred, _pool("伯顿", "诺斯郡"),
                                   matched={("伯顿", "诺斯郡")})
    assert c == {}
