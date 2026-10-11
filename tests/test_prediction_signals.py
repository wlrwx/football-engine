"""平局预警分类 + 市场分歧检测测试（2026-10-11）

从 main.py 的 for 循环体抽出。要求逐位不变——两个字段都落进
predictions.json 并进账本统计，改了会让历史分层结果不可比。
"""

from __future__ import annotations

import random

from engine.prediction.signals import (
    classify_draw_alert,
    detect_market_disagreement,
    market_direction,
)

HIGH = ("巴甲", "意甲", "英超")


def _reference_draw(league, mp, high):
    """抽取前的内联实现（逐字拷贝）。"""
    if not mp:
        return None
    mh, md, ma = mp
    mm = max(mh, md, ma)
    if league in high and 0.20 <= md < 0.30:
        return "league_draw"
    elif mm > 0.50 and md >= 0.25:
        return "cold_draw"
    elif abs(mh - ma) < 0.15 and md >= 0.26:
        return "balanced_draw"
    return None


def _reference_mkt(p):
    """抽取前的内联实现（逐字拷贝）。"""
    d = p.get("direction")
    if not d:
        pr = (p.get("home_win_prob", 0), p.get("draw_prob", 0),
              p.get("away_win_prob", 0))
        d = ["home", "draw", "away"][pr.index(max(pr))]
    out = {}
    for s, pk, ok in (("home", "home_win_prob", "home_odds"),
                      ("draw", "draw_prob", "draw_odds"),
                      ("away", "away_win_prob", "away_odds")):
        if s == d:
            continue
        prob = p.get(pk, 0)
        od = p.get(ok)
        if od and prob * od - 1 > 0.05:
            out[s] = {"prob": round(prob, 3), "odds": od,
                      "ev": round(prob * od - 1, 3)}
    return out


class TestClassifyDrawAlert:
    def test_league_draw_wins_priority(self):
        """R1 联赛平局优先级最高，不能被后两条抢走。"""
        mp = (0.45, 0.25, 0.30)   # 均势且平局≥26%，本会落 balanced_draw
        assert classify_draw_alert("巴甲", mp, HIGH) == "league_draw"

    def test_league_draw_upper_bound_exclusive(self):
        """市场平局 P 必须 <0.30，恰好 0.30 不算 league_draw。"""
        assert classify_draw_alert("巴甲", (0.45, 0.30, 0.25), HIGH) != "league_draw"
        assert classify_draw_alert("巴甲", (0.45, 0.29, 0.26), HIGH) == "league_draw"

    def test_cold_draw(self):
        assert classify_draw_alert("西甲", (0.55, 0.26, 0.19), HIGH) == "cold_draw"

    def test_cold_draw_needs_market_gt_50(self):
        """恰好 0.50 不算"强烈看好"，严格大于。"""
        assert classify_draw_alert("西甲", (0.50, 0.26, 0.24), HIGH) != "cold_draw"

    def test_balanced_draw(self):
        assert classify_draw_alert("西甲", (0.42, 0.30, 0.28), HIGH) == "balanced_draw"

    def test_no_alert(self):
        assert classify_draw_alert("西甲", (0.60, 0.20, 0.20), HIGH) is None

    def test_missing_market_probs(self):
        assert classify_draw_alert("巴甲", None, HIGH) is None
        assert classify_draw_alert("巴甲", [], HIGH) is None


class TestMarketDirection:
    def test_explicit_direction_wins(self):
        assert market_direction({"direction": "away", "home_win_prob": 0.5,
                                 "draw_prob": 0.3, "away_win_prob": 0.2}) == "away"

    def test_fallback_to_argmax(self):
        assert market_direction({"home_win_prob": 0.2, "draw_prob": 0.5,
                                 "away_win_prob": 0.3}) == "draw"

    def test_all_zero_defaults_home(self):
        assert market_direction({}) == "home"


class TestDetectMarketDisagreement:
    def test_no_disagreement_when_direction_only_one(self):
        p = {"direction": "home", "home_win_prob": 0.5, "draw_prob": 0.3,
             "away_win_prob": 0.2, "home_odds": 1.8, "draw_odds": 3.2,
             "away_odds": 4.5}
        # draw: 0.3*3.2-1 = -0.04 <0.05；away: 0.2*4.5-1 = -0.10
        assert detect_market_disagreement(p) == {}

    def test_detects_positive_ev_non_direction(self):
        p = {"direction": "home", "home_win_prob": 0.5, "draw_prob": 0.30,
             "away_win_prob": 0.20, "draw_odds": 4.0}
        # 0.30*4.0-1 = +0.20 >0.05
        got = detect_market_disagreement(p)
        assert got["draw"] == {"prob": 0.3, "odds": 4.0, "ev": 0.2}

    def test_direction_side_always_excluded(self):
        p = {"direction": "home", "home_win_prob": 0.9, "draw_prob": 0.05,
             "away_win_prob": 0.05, "home_odds": 1.05}
        assert "home" not in detect_market_disagreement(p)

    def test_threshold_boundary_float_reality(self):
        """阈值是严格大于，但浮点上 0.25*4.2-1 = 0.050000000000000044。
        这里锁真实行为，不锁"数学上的恰好 0.05"。"""
        p = {"direction": "home", "home_win_prob": 0.5, "draw_prob": 0.25,
             "away_win_prob": 0.25, "draw_odds": 4.2}   # 0.25*4.2-1 = 0.05
        assert detect_market_disagreement(p) == _reference_mkt(p)
        # 真正低于阈值的情况
        p2 = dict(p, draw_odds=4.0)                     # 0.25*4.0-1 = 0.0
        assert detect_market_disagreement(p2) == {}

    def test_none_probability_does_not_crash(self):
        """刻意偏离原实现的一处防御性修复。

        原内联代码 `max((p.get(k, 0), ...))` 在概率为 None 时抛
        TypeError（None 不能与 int 比较）。新实现把 None 视作 0。
        生产上 predictions.json 的三路概率不会为 None（已用 1017 场
        真实数据验证），所以这是纯防御性改动，不影响任何现有行为。
        """
        p = {"home_win_prob": None, "draw_prob": 0, "away_win_prob": 0}
        assert market_direction(p) == "home"
    def test_missing_odds_skipped(self):
        p = {"direction": "home", "home_win_prob": 0.5, "draw_prob": 0.5,
             "away_win_prob": 0.0}
        assert detect_market_disagreement(p) == {}

    def test_custom_threshold(self):
        p = {"direction": "home", "home_win_prob": 0.5, "draw_prob": 0.26,
             "away_win_prob": 0.24, "draw_odds": 4.0}   # EV +0.04
        assert detect_market_disagreement(p) == {}
        assert "draw" in detect_market_disagreement(p, ev_threshold=0.03)


class TestEquivalenceWithInline:
    def test_random_draw_alerts_identical(self):
        rng = random.Random(20261011)
        leagues = list(HIGH) + ["西甲", "德甲"]
        for _ in range(400):
            a, b, c = rng.random(), rng.random(), rng.random()
            s = a + b + c
            mp = (a / s, b / s, c / s)
            lg = rng.choice(leagues)
            assert (classify_draw_alert(lg, mp, HIGH)
                    == _reference_draw(lg, mp, HIGH))

    def test_boundary_draw_alerts_identical(self):
        """边界值逐一对照（0.20/0.25/0.26/0.30/0.50/0.15）。"""
        for md in (0.19, 0.20, 0.2499, 0.25, 0.26, 0.2999, 0.30, 0.40):
            for spread in (0.0, 0.10, 0.15, 0.30):
                rest = 1.0 - md
                mp = ((rest + spread) / 2, md, max(0.0, (rest - spread) / 2))
                for lg in ("巴甲", "西甲"):
                    assert (classify_draw_alert(lg, mp, HIGH)
                            == _reference_draw(lg, mp, HIGH))

    def test_random_disagreements_identical(self):
        rng = random.Random(20261011)
        for _ in range(400):
            probs = [rng.random() for _ in range(3)]
            s = sum(probs)
            p = {
                "home_win_prob": probs[0] / s, "draw_prob": probs[1] / s,
                "away_win_prob": probs[2] / s,
                "home_odds": round(rng.uniform(1.01, 9), 2),
                "draw_odds": round(rng.uniform(1.01, 9), 2),
                "away_odds": round(rng.uniform(1.01, 9), 2),
            }
            if rng.random() < 0.3:
                p["direction"] = rng.choice(["home", "draw", "away"])
            assert detect_market_disagreement(p) == _reference_mkt(p)

    def test_malformed_identical(self):
        """None 概率被上一条单独覆盖（那里是刻意的行为偏离）。
        这里只对照原实现同样能正常处理的畸形输入。"""
        cases = [
            {},
            {"direction": "draw", "home_win_prob": 0.5, "draw_prob": 0.5,
             "away_win_prob": 0.0, "away_odds": 0},
            {"home_win_prob": 0.33, "draw_prob": 0.34, "away_win_prob": 0.33,
             "home_odds": 3.0, "draw_odds": 3.0, "away_odds": 3.0},
        ]
        for p in cases:
            assert detect_market_disagreement(p) == _reference_mkt(p)

