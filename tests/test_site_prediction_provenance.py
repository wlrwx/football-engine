import engine.build_site as bs


def _prediction(**overrides):
    pred = {
        "match_id": "2026-11-27_周五001",
        "competition": "澳超",
        "home_team": "主队",
        "away_team": "客队",
        "home_win_prob": 0.30,
        "draw_prob": 0.25,
        "away_win_prob": 0.45,
        "direction": "away",
        "model_raw": {"home": 0.45, "draw": 0.30, "away": 0.25},
        "market_fair": [0.50, 0.25, 0.25],
        "fusion_effective_weights": {"model": 0.0625, "market": 0.9375, "djyy": 0.0},
        "confidence": 0.45,
        "home_odds": 2.6,
        "draw_odds": 3.35,
        "away_odds": 2.2,
    }
    pred.update(overrides)
    return pred


def test_provenance_helpers_handle_missing_sources():
    assert bs._prediction_provenance({"home_win_prob": 0.3, "draw_prob": 0.3, "away_win_prob": 0.4}) == {
        "model": None,
        "market": None,
        "final": (2, 0.4),
    }
    assert bs._format_direction_view(None) == "—"
    assert bs._market_reading({}) == ""
    assert bs._effective_weight_text({}) == ""


def test_final_direction_honours_post_fusion_override():
    p = _prediction(direction="home")
    assert bs._prediction_provenance(p)["final"] == (0, 0.30)


def test_match_card_shows_compact_and_expanded_provenance():
    html = bs._match_card(_prediction(), set(), 0)
    assert "模型 主胜 45% · 市场 主胜 50% · 最终 客胜 45%" in html
    assert "市场比最终倾向更看好主胜；最终融合更看好客胜" in html
    assert "实际权重：模型 6% · 市场 94% · DJYY 0%" in html
    assert 'class="provenance-box"' in html
    assert "DC+MC 原始" in html
    assert "最终融合" in html


def test_market_dependence_summary_counts_direction_and_weight():
    text = bs._market_follow_summary([
        _prediction(),
        _prediction(match_id="b", direction="home"),
        _prediction(match_id="c", market_fair=None, fusion_effective_weights=None),
    ])
    assert "最终跟随市场 1/2 场" in text
    assert "平均市场权重 94%" in text


def test_battle_board_shows_market_dependence_without_singles():
    html = bs._battle_board(
        {"stable": [], "value": [], "lottery": [], "score_parlay": [{"stake": 2}]},
        [_prediction()],
        [],
    )
    assert "今日无单注价值推荐" in html
    assert "最终跟随市场 0/1 场" in html
    assert "平均市场权重 94%" in html


def test_daily_brief_separates_sources_and_weights(tmp_path):
    path = bs._build_daily_brief(
        tmp_path, "2026-11-27", [_prediction()], {}, None, None, None,
    )
    text = path.read_text(encoding="utf-8")
    assert "原始模型 = 独立统计判断" in text
    assert "| 原始模型 | 市场 | 最终 |" in text
    assert "主胜 45% | 主胜 50% | 客胜 45%" in text
    assert "6%/94%/0%" in text


def test_daily_brief_does_not_call_score_parlay_empty(tmp_path):
    ticket = {
        "stable": [],
        "value": [],
        "lottery": [],
        "total_stake": 0,
        "parlay_stake": 16,
        "total_exposure": 16,
        "score_parlay": [{
            "type": "比分2串1",
            "stake": 16,
            "total_odds": 280,
            "legs": [
                {"home": "主队", "away": "客队", "score": "1-0"},
                {"home": "甲队", "away": "乙队", "score": "0-1"},
            ],
        }],
    }
    path = bs._build_daily_brief(tmp_path, "2026-11-27", [], ticket, None, None, None)
    text = path.read_text(encoding="utf-8")
    assert "无单场方案" in text
    assert "高风险串关" in text
    assert "合计 **¥16**" in text
    assert "比分 1-0" in text
    assert "空仓不出手" not in text


def test_ev_section_tables_scroll_without_expanding_mobile_page():
    html = bs._ev_section({
        "total": {"n": 3, "hit_rate": 0.5, "roi": 0.1},
        "layers": {"L1": {"n": 3, "hit_rate": 0.5, "roi": 0.1, "verdict": "观察"}},
        "leagues": {"澳超": {"n": 3, "hit_rate": 0.5, "roi": 0.1}},
        "takeaways": ["样本累积中"],
    })
    assert 'class="ev-grid"' in html
    assert html.count('class="ev-table-wrap"') == 2
