import json
import re
from html.parser import HTMLParser
from pathlib import Path

import engine.build_site as bs


class _DivStructureChecker(HTMLParser):
    """Verify balanced <div> nesting and track key container ancestry."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.div_stack: list[tuple[str, str]] = []
        self.extra_closes: list[int] = []
        self.system_panel_ancestors: list[list[str]] = []
        self.footer_ancestors: list[list[str]] = []
        self._in_raw = False

    def handle_starttag(self, tag: str, attrs):
        tag_l = tag.lower()
        if tag_l in ("script", "style"):
            self._in_raw = True
            return
        if tag_l != "div":
            return
        attr_map = dict(attrs)
        div_id = attr_map.get("id", "")
        div_cls = attr_map.get("class", "")
        if div_id == "system-panel":
            self.system_panel_ancestors.append([x[0] for x in self.div_stack if x[0]])
        if "footer" in div_cls.split():
            self.footer_ancestors.append([x[1] for x in self.div_stack if x[1]])
        self.div_stack.append((div_id, div_cls))

    def handle_endtag(self, tag: str):
        tag_l = tag.lower()
        if tag_l in ("script", "style"):
            self._in_raw = False
            return
        if tag_l != "div":
            return
        if not self.div_stack:
            self.extra_closes.append(self.getpos()[0])
            return
        self.div_stack.pop()


def _check_css_vars(html: str) -> set[str]:
    defined = set(re.findall(r"(--[a-zA-Z0-9_-]+)\s*:", html))
    used = set(re.findall(r"var\((--[a-zA-Z0-9_-]+)(?:\s*,|\))", html))
    return used - defined


def _sample_pred() -> dict:
    return {
        "match_id": "2026-11-27_周五001",
        "competition": "英超",
        "home_team": "阿森纳",
        "away_team": "切尔西",
        "kickoff": "2026-11-27 20:00",
        "home_win_prob": 0.55,
        "draw_prob": 0.25,
        "away_win_prob": 0.20,
        "direction": "home",
        "confidence": 0.55,
        "home_odds": 1.85,
        "draw_odds": 3.50,
        "away_odds": 4.20,
        "market_fair": [0.51, 0.27, 0.22],
        "home_xg": 1.75,
        "away_xg": 1.05,
        "top_scores": [[1, 0, 0.12], [2, 1, 0.11], [1, 1, 0.10]],
        "total_goals": [[0, 0.08], [1, 0.18], [2, 0.25]],
        "draw_alert": "cold_draw",
        "prob_band_5060": True,
        "lgbm_inputs": {
            "cross_market": {
                "pin_implied_home": 0.50,
                "pin_implied_draw": 0.28,
                "pin_implied_away": 0.22,
            }
        },
    }


def _populate_state_dir(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "review_ledger.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "date": "2026-11-25",
                    "league": "英超",
                    "hit": i % 2 == 0,
                    "actual_idx": i % 3,
                    "final_prob": [0.55, 0.25, 0.20],
                    "score_rank": 1 if i == 0 else 4,
                    "score_top3_hit": i == 0,
                    "score_top5_hit": True,
                    "score_top8_hit": True,
                },
                ensure_ascii=False,
            )
            for i in range(6)
        ),
        encoding="utf-8",
    )
    (state_dir / "accuracy_trend.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-11-27T09:00:00",
                "date_range": ["2026-11-01", "2026-11-25"],
                "overall": {"brier": 0.5543, "brier_market": 0.5623, "brier_model": 0.5900},
                "rolling7": {"hit_rate": 0.57, "n": 30},
                "verdict": "持平",
                "daily": [
                    {
                        "date": "2026-11-25",
                        "n": 6,
                        "hits": 4,
                        "hit_rate": 4 / 6,
                        "brier_final": 0.54,
                        "cum_hit_rate": 0.58,
                        "pnl": 12.0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (state_dir / "upgrade_tracker.json").write_text(
        json.dumps(
            {
                "verdict": "IMPROVED",
                "v2_clean": {"n": 1185, "brier": 0.5543, "hit_rate": 0.5713},
                "baseline_pre_upgrade": {"n": 426, "brier": 0.5576, "hit_rate": 0.5704},
                "paired_vs_market": {
                    "n": 1109,
                    "brier_final": 0.5621,
                    "brier_market": 0.5623,
                    "delta_brier": -0.0002,
                    "t": -0.27,
                    "verdict": "TIED",
                },
                "effective_fusion_weights": {
                    "raw": {"model": 0.05, "market": 0.75, "djyy": 0.20},
                    "effective": {"model": 0.0625, "market": 0.9375, "djyy": 0.0},
                    "djyy_hit_rate": 0.2954,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (state_dir / "lgbm_status.json").write_text(
        json.dumps(
            {
                "trained": True,
                "ready": False,
                "usable_rows": 1600,
                "holdout_n": 480,
                "delta_vs_fusion_v2": -0.01934,
                "reason": "影子验证未双基线显著 → 保持关闭",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (state_dir / "league_report.json").write_text(
        json.dumps(
            {
                "leagues": [
                    {
                        "league": "英超",
                        "n": 20,
                        "hit_rate": 0.60,
                        "avg_odds": 2.10,
                        "roi": 0.08,
                        "verdict": "价值区",
                        "recent_n": 5,
                        "recent_hits": 3,
                        "recent_hit_rate": 0.60,
                        "recent_roi": 0.12,
                        "recent10_n": 10,
                        "recent10_hits": 6,
                        "recent10_hit_rate": 0.60,
                        "recent10_roi": 0.10,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (state_dir / "high_conf_reversals.jsonl").write_text(
        json.dumps(
            {
                "date": "2026-11-20",
                "league": "德甲",
                "teams": "拜仁 vs 多特",
                "direction": "home",
                "conf": 0.68,
                "actual_dir": "away",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "weekly_backtest.json").write_text(
        json.dumps(
            {
                "steps": {
                    "horizon_eval": {
                        "best_horizon": "<6h",
                        "buckets": [
                            {"label": "<6h", "n": 25, "brier": 0.55, "hit_rate": 0.60, "total_ev": 1.2}
                        ],
                    },
                    "shrinkage_dc": {
                        "n_evaluated": 50,
                        "verdict": "hold",
                        "shrinkage_dc_brier": 0.57,
                        "final_brier": 0.55,
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (state_dir.parent / "league_matrix.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-13T07:03:39Z",
                "leagues": [
                    {
                        "name_zh": "英超",
                        "category": "tier1",
                        "matches": 380,
                        "avg_goals": 2.9,
                        "avg_xg": 2.8,
                        "btts_pct": 58,
                        "home_win_pct": 46,
                        "draw_pct": 24,
                        "away_win_pct": 30,
                        "clean_sheet_pct": 26,
                        "avg_corners": 10.2,
                        "avg_yellow": 4.1,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_daily_dom_css_exposure_and_scoping(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "ROOT", tmp_path)
    ticket = {
        "stable": [],
        "value": [],
        "lottery": [],
        "total_stake": 0,
        "parlay_stake": 16,
        "total_exposure": 16,
        "expected_roi": 0.0,
        "bankroll": 1000,
        "breaker_multiplier": 1.0,
        "parlay": [{"type": "2串1", "legs": []}],
    }
    html = bs._render_html(
        today="2026-11-27",
        predictions=[_sample_pred()],
        bundle={"bundle_sha256": "abc123", "created_at": "2026-11-27T08:00:00"},
        ticket=ticket,
        breaker={},
        health={},
        all_dates=["2026-11-27"],
    )

    checker = _DivStructureChecker()
    checker.feed(html)
    assert checker.extra_closes == []
    assert checker.div_stack == []
    assert checker.system_panel_ancestors == [["tab-review"]]
    assert any("page" in cls.split() for cls in checker.footer_ancestors[0])

    assert _check_css_vars(html) == set()
    assert "match-board" in html
    assert "m-teams" in html
    assert "m-probs" in html
    assert "今日单注空仓" in html
    assert "实际总敞口" in html
    assert "&yen;16" in html
    assert "单注 &yen;0 &middot; 串关 &yen;16" in html
    assert "var(--dim)\">—<" in html
    assert "track-record.html" in html
    for global_only in ("高置信反向样本库", "预测时点分桶", "联赛分层（送钱区禁投"):
        assert global_only not in html


def test_track_record_dom_css_and_v2_sections(tmp_path):
    state_dir = tmp_path / "data" / "state"
    web_dir = tmp_path / "web"
    web_dir.mkdir(parents=True)
    _populate_state_dir(state_dir)

    name = bs._render_track_record(state_dir, web_dir)
    html = (web_dir / name).read_text(encoding="utf-8")

    checker = _DivStructureChecker()
    checker.feed(html)
    assert checker.extra_closes == []
    assert checker.div_stack == []
    assert _check_css_vars(html) == set()

    for tab_id in ("tr-overview", "tr-betting", "tr-diagnostics"):
        assert tab_id in html
    assert "v2 洁净场次" in html
    assert "v2 vs 市场 (配对)" in html
    assert "DJYY 覆盖 29.5%" in html
    assert "5/75/20" in html
    assert "holdout n=480" in html
    assert "高置信反向样本库" in html
    assert "预测时点分桶" in html
    assert "联赛分层" in html
    assert "已过期" in html
    assert "每日更新" not in html


def test_tab_odds_no_fake_fallback_and_split_edge():
    p = _sample_pred()
    html = bs._tab_odds(p, "m0")
    assert "Bet365</td><td>-</td><td>-</td><td>-</td>" in html
    assert "Pinnacle(隐含公平)" in html
    assert "2.00" in html  # 1 / 0.50 from cross_market.pin_implied_home
    assert "去水边际" in html
    assert "含水 EV" in html
    assert "+4.0pp" in html  # (0.55 - 0.51) * 100
    assert "+1.8%" in html   # (0.55 * 1.85 - 1) * 100


def test_league_matrix_stale_warning():
    lm = {
        "generated_at": "2026-08-13T07:03:39Z",
        "leagues": [
            {
                "name_zh": "英超",
                "category": "tier1",
                "matches": 380,
                "avg_goals": 2.9,
                "avg_xg": 2.8,
                "btts_pct": 58,
                "home_win_pct": 46,
                "draw_pct": 24,
                "away_win_pct": 30,
                "clean_sheet_pct": 26,
                "avg_corners": 10.2,
                "avg_yellow": 4.1,
            }
        ],
    }
    html = bs._league_matrix_section(lm, [_sample_pred()], ref_date="2026-11-27")
    assert "2026-08-13" in html
    assert "已过期 106 天" in html
    assert "每日更新" not in html
