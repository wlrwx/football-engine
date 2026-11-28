from datetime import date
import re
from html.parser import HTMLParser

import engine.build_site as bs
from engine.sources.sporttery import SportterySource, _parse_single


class _DivBalanceChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.extra_closes = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "div":
            self.stack.append(dict(attrs).get("id", ""))

    def handle_endtag(self, tag):
        if tag.lower() == "div":
            if not self.stack:
                self.extra_closes.append(self.getpos()[0])
            else:
                self.stack.pop()


def _sample_sporttery_pred(**overrides):
    pred = {
        "match_id": "2026-11-28_周六001",
        "competition": "英超",
        "home_team": "阿森纳",
        "away_team": "西汉姆",
        "kickoff": "2026-11-28 04:00",
        "home_win_prob": 0.64,
        "draw_prob": 0.21,
        "away_win_prob": 0.15,
        "direction": "home",
        "confidence": 0.64,
        "home_odds": 1.48,
        "draw_odds": 4.10,
        "away_odds": 5.80,
        "had_single": True,
        "hhad_single": False,
        "handicap": -1.0,
        "handicap_ev": {
            "handicap": -1.0,
            "probs": {"home": 0.42, "draw": 0.25, "away": 0.33},
            "odds": {"home": 2.55, "draw": 3.45, "away": 2.25},
            "edges": {"home": 0.071, "draw": -0.1375, "away": -0.2575},
        },
        "play_ev": {"ttg": 0.12},
        "crs_odds": {"s01s00": "7.50", "s02s01": "8.00", "s02s00": "6.80", "s01s00f": "1"},
        "ttg_odds": {"s0": "12.00", "s1": "5.20", "s2": "3.60", "s3": "4.20", "s7": "19.00"},
        "hafu_odds": {"hh": "2.20", "dh": "4.30", "dd": "5.50", "aa": "10.00"},
        "htft": {"HH": 0.48, "DH": 0.18, "DD": 0.11, "AA": 0.07},
        "top_scores": [[2, 0, 0.14], [1, 0, 0.15], [2, 1, 0.11]],
        "total_goals": [[0, 0.07], [1, 0.19], [2, 0.29], [3, 0.22], [7, 0.03]],
        "home_xg": 1.95,
        "away_xg": 0.72,
        "model_raw": {"home": 0.60, "draw": 0.23, "away": 0.17},
        "market_fair": [0.62, 0.22, 0.16],
        "fusion_effective_weights": {"model": 0.15, "market": 0.85, "djyy": 0.0},
    }
    pred.update(overrides)
    return pred


def test_parse_single_pool_and_fetch_fixtures(monkeypatch):
    assert _parse_single({}, {"single": 1}, "HAD") is True
    assert _parse_single({}, {"single": "0"}, "HHAD") is False
    item = {"poolList": [{"poolCode": "HAD", "bettingSingle": 1}, {"poolCode": "HHAD", "bettingSingle": 0}]}
    assert _parse_single(item, {}, "HAD") is True
    assert _parse_single(item, {}, "HHAD") is False
    assert _parse_single({}, {}, "HAD") is None

    payload = {
        "value": {
            "matchInfoList": [
                {
                    "businessDate": "2026-11-28",
                    "subMatchList": [
                        {
                            "matchNumStr": "周六001",
                            "leagueAbbName": "英超",
                            "homeTeamAbbName": "主队A",
                            "awayTeamAbbName": "客队B",
                            "matchDate": "2026-11-28",
                            "matchTime": "20:00",
                            "had": {"h": "1.80", "d": "3.40", "a": "4.00", "single": 1},
                            "hhad": {"goalLine": "-1", "h": "3.20", "d": "3.50", "a": "1.90", "single": 0},
                        }
                    ],
                }
            ]
        }
    }
    src = SportterySource()
    monkeypatch.setattr(src, "_fetch_json", lambda *args, **kwargs: payload)
    fixtures = src.fetch_fixtures(date(2026, 11, 28))
    assert len(fixtures) == 1
    assert fixtures[0].had_single is True
    assert fixtures[0].hhad_single is False


def test_match_card_sporttery_badges_and_dual_market_cells():
    pred = _sample_sporttery_pred()
    html = bs._match_card(pred, set(), 0)
    assert 'class="single-tag"' in html
    assert 'class="cutoff-tag"' in html
    assert "早截单" in html
    assert "总进球 +12%" in html
    assert 'data-mkt="had"' in html
    assert 'data-mkt="hhad"' in html
    assert 'data-sel="hh"' in html
    assert 'data-single="1"' in html
    assert "体彩全玩法" in html

    # Settled match should suppress 早截单 warning
    settled_html = bs._match_card(pred, set(), 0, results_map={pred["match_id"]: {"home_score": 2, "away_score": 0}})
    assert 'class="cutoff-tag"' not in settled_html


def test_sporttery_focus_bar_and_distribution_multi_play():
    p1 = _sample_sporttery_pred()
    p2 = _sample_sporttery_pred(
        match_id="2026-11-28_周六002",
        home_team="狼队",
        away_team="伯恩茅斯",
        kickoff="2026-11-28 23:00",
        home_win_prob=0.38,
        draw_prob=0.32,
        away_win_prob=0.30,
        had_single=False,
        handicap=None,
        handicap_ev=None,
        play_ev={},
        draw_alert="balanced_draw",
        league_forbidden=True,
    )
    focus_html = bs._sporttery_focus_bar([p1, p2])
    assert 'class="focus-strip"' in focus_html
    assert "最佳玩法机会" in focus_html
    assert "低损稳胆参考" in focus_html
    assert "头号防冷避坑" in focus_html
    assert "禁投联赛" in focus_html

    dist_html = bs._tab_distribution(p1, "m0")
    assert "含体彩官方波胆赔率" in dist_html
    assert "@7.5" in dist_html
    assert "含体彩官方总进球赔率" in dist_html
    assert "@5.2" in dist_html
    assert "半全场预测 TOP6" in dist_html
    assert "胜胜" in dist_html
    assert "@2.20" in dist_html


def test_render_html_workbench_toolbar_slip_and_structure():
    p1 = _sample_sporttery_pred()
    p2 = _sample_sporttery_pred(
        match_id="2026-11-28_周六002",
        home_team="拜仁",
        away_team="多特",
        competition="德甲",
        kickoff="2026-11-28 22:30",
        home_win_prob=0.61,
        draw_prob=0.22,
        away_win_prob=0.17,
        had_single=False,
    )
    ticket = {
        "stable": [],
        "value": [],
        "lottery": [],
        "total_stake": 0,
        "parlay": [],
        "parlay_calibration": {"table": {"0.6": 0.58}, "overall": 0.45, "n": 40, "min_samples": 8},
    }
    html = bs._render_html(
        "2026-11-28",
        [p1, p2],
        {},
        ticket,
        {},
        {},
        results=[{"match_id": "2026-11-28_周六002", "home_score": 3, "away_score": 1}],
        results_preds=[p1, p2],
        all_dates=["2026-11-28"],
    )

    assert 'class="board-toolbar"' in html
    assert 'data-qf="single"' in html
    assert 'data-sort="ev"' in html
    assert 'id="slip-fab"' in html
    assert 'id="slip-panel"' in html
    assert "var PARLAY_CAL =" in html
    assert "4球" in html  # 3-1 post-match total goals in _results_section

    checker = _DivBalanceChecker()
    checker.feed(html)
    assert checker.stack == []
    assert checker.extra_closes == []

    defined = set(re.findall(r"(--[a-zA-Z0-9_-]+)\s*:", html))
    used = set(re.findall(r"var\((--[a-zA-Z0-9_-]+)(?:\s*,|\))", html))
    assert not (used - defined)
