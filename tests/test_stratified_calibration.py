"""分层校准偏差只读诊断测试（2026-11-07）。

覆盖：赔率档边界、Brier 同式、Wilson 区间、样本筛选（v2/污染/旧链）、
赔率连接与合成赔率排除、层统计手算核对、排名小样本过滤、端到端只读。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import stratified_calibration as sc  # noqa: E402


def _row(mid="m1", final=(0.7, 0.2, 0.1), best=0, actual=0, hit=True,
         market=(0.6, 0.25, 0.15), model=None, league="意甲",
         chain="v2", contaminated=False, date="2026-10-01"):
    r = {
        "match_id": mid, "date": date, "league": league,
        "final_prob": list(final), "best_selection": best, "actual_idx": actual,
        "hit": hit, "market_fair": list(market) if market is not None else None,
        "model_raw": list(model) if model is not None else None,
        "brier_final": sc.brier3(list(final), actual),
    }
    if chain is not None:
        r["chain"] = chain
    if contaminated:
        r["contaminated"] = True
    return r


class TestTierOf:
    def test_boundaries(self):
        assert sc.tier_of(1.599) == "<1.60"
        assert sc.tier_of(1.60) == "1.60-2.50"
        assert sc.tier_of(2.50) == "1.60-2.50"
        assert sc.tier_of(2.501) == ">2.50"
        assert sc.tier_of(1.01) == "<1.60"
        assert sc.tier_of(9.9) == ">2.50"

    def test_invalid(self):
        for o in (None, 1.0, 0.9, -2, float("nan"), float("inf"), "abc"):
            assert sc.tier_of(o) is None


class TestBrier3:
    def test_matches_post_match_formula(self):
        from engine.review.post_match import brier_score
        for probs, actual in (([0.7, 0.2, 0.1], 0), ([0.2, 0.3, 0.5], 2),
                              ([1 / 3] * 3, 1), ([0.0, 0.0, 1.0], 0)):
            assert sc.brier3(probs, actual) == brier_score(probs, actual)

    def test_known_values(self):
        assert sc.brier3([1.0, 0.0, 0.0], 0) == 0.0
        assert sc.brier3([0.0, 0.0, 1.0], 0) == 2.0
        assert math.isclose(sc.brier3([1 / 3] * 3, 0), 2 / 3)
        assert sc.brier3([], 0) == 1.0
        assert sc.brier3(None, 0) == 1.0


class TestWilson:
    def test_extremes(self):
        lo, hi = sc.wilson(0, 10)
        assert lo == 0.0 and 0 < hi < 0.5
        lo, hi = sc.wilson(10, 10)
        assert hi == 1.0 and 0.5 < lo < 1.0
        assert sc.wilson(0, 0) == (0.0, 1.0)

    def test_half_half(self):
        lo, hi = sc.wilson(50, 100)
        assert 0.39 < lo < 0.42 and 0.58 < hi < 0.61


class TestSelectSample:
    def test_filters(self):
        ledger = [
            _row(mid="a"),
            _row(mid="b", chain=None),            # 旧链
            _row(mid="c", chain="v1"),            # 其他链
            _row(mid="d", contaminated=True),     # 污染
        ]
        clean, excluded = sc.select_sample(ledger)
        assert [r["match_id"] for r in clean] == ["a"]
        assert excluded == {"legacy_or_other_chain": 2, "contaminated": 1}


class TestEnrich:
    def test_join_and_side(self):
        rows = [_row(mid="a", best=0), _row(mid="b", best=2, final=(0.1, 0.2, 0.7))]
        idx = {
            "a": {"odds": [1.5, 4.0, 6.0], "synthetic": False},
            "b": {"odds": [5.0, 4.0, 1.7], "synthetic": True},
        }
        out = sc.enrich(rows, idx)
        assert out[0]["sel_odds"] == 1.5 and out[0]["tier"] == "<1.60"
        assert out[0]["side"] == "home" and out[0]["odds_synthetic"] is False
        assert out[1]["sel_odds"] == 1.7 and out[1]["tier"] == "1.60-2.50"
        assert out[1]["side"] == "away" and out[1]["odds_synthetic"] is True
        # 不改原 dict
        assert "tier" not in rows[0]

    def test_missing_prediction(self):
        out = sc.enrich([_row(mid="ghost")], {})
        assert out[0]["sel_odds"] is None and out[0]["tier"] is None


class TestStratumMetrics:
    def _rows(self):
        rows = [
            _row(mid="r1", final=(0.7, 0.2, 0.1), best=0, actual=0, hit=True),
            _row(mid="r2", final=(0.6, 0.25, 0.15), best=0, actual=2, hit=False, market=None),
            _row(mid="r3", final=(0.2, 0.3, 0.5), best=2, actual=2, hit=True, market=None),
            _row(mid="r4", final=(0.4, 0.35, 0.25), best=0, actual=1, hit=False,
                 market=(0.4, 0.3, 0.3)),
        ]
        idx = {
            "r1": {"odds": [1.5, 4.0, 6.0], "synthetic": False},
            "r2": {"odds": [1.8, 3.5, 4.5], "synthetic": False},
            "r3": {"odds": [5.0, 4.0, 3.0], "synthetic": True},   # 合成 → 排除赔率统计
            "r4": {"odds": [2.0, 3.2, 3.8], "synthetic": False},
        }
        return sc.enrich(rows, idx)

    def test_hand_computed(self):
        m = sc.stratum_metrics(self._rows())
        assert m["n"] == 4 and m["hits"] == 2
        assert m["hit_rate"] == 0.5
        assert math.isclose(m["mean_p_sel"], 0.55)
        assert math.isclose(m["calib_gap"], 0.05)
        assert math.isclose(m["brier_final"], 0.5775)
        # 合成赔率行 r3 不进赔率统计
        assert m["n_odds"] == 3
        assert math.isclose(m["hit_rate_odds"], 1 / 3)
        assert math.isclose(m["breakeven"], (1 / 1.5 + 1 / 1.8 + 1 / 2.0) / 3)
        assert math.isclose(m["edge"], 1 / 3 - (1 / 1.5 + 1 / 1.8 + 1 / 2.0) / 3)
        assert math.isclose(m["roi"], -0.5)
        assert math.isclose(m["roi_se"], 0.5)
        # 市场 Brier 仅配对 r1/r4
        assert m["n_market"] == 2
        assert math.isclose(m["brier_market"], (0.245 + 0.74) / 2)
        assert math.isclose(m["brier_final_paired_market"], (0.14 + 0.645) / 2)
        assert m["small"] == "不可结论"

    def test_empty(self):
        m = sc.stratum_metrics([])
        assert m["n"] == 0 and m["hit_rate"] is None and m["edge"] is None


class TestStratifyRank:
    def test_stratify_skips_none(self):
        rows = sc.enrich([_row(mid="a", league="意甲"), _row(mid="b", league="")], {})
        strata = sc.stratify(rows, lambda r: r.get("league") or None)
        assert set(strata) == {"意甲"}

    def test_rank_filters_small_n(self):
        strata = {
            "big_over": {"n": 200, "n_odds": 200, "calib_gap": 0.10, "edge": -0.02},
            "big_under": {"n": 150, "n_odds": 150, "calib_gap": -0.12, "edge": -0.08},
            "tiny": {"n": 10, "n_odds": 10, "calib_gap": 0.50, "edge": -0.50},
            "no_odds": {"n": 100, "n_odds": 5, "calib_gap": 0.01, "edge": -0.30},
        }
        rank = sc.rank_biases(strata)
        # 校准偏差排名只看 n（no_odds 的 n=100 合格）；赔率缺口排名看 n_odds
        assert [k for k, _ in rank["calib"]] == ["big_under", "big_over", "no_odds"]
        assert [k for k, _ in rank["edge"]] == ["big_under", "big_over"]


class TestEndToEnd:
    def _fixture(self, tmp_path):
        ledger = [
            _row(mid="r1", final=(0.7, 0.2, 0.1), best=0, actual=0, hit=True),
            _row(mid="r2", final=(0.6, 0.25, 0.15), best=0, actual=2, hit=False, market=None),
            _row(mid="legacy", chain=None),
            _row(mid="dirty", contaminated=True),
        ]
        lp = tmp_path / "review_ledger.jsonl"
        lp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in ledger) + "\n",
                      encoding="utf-8")
        day = tmp_path / "daily" / "2026-10-01"
        day.mkdir(parents=True)
        preds = [
            {"match_id": "r1", "home_odds": 1.5, "draw_odds": 4.0, "away_odds": 6.0,
             "odds_synthetic": False},
            {"match_id": "r2", "home_odds": 1.8, "draw_odds": 3.5, "away_odds": 4.5,
             "odds_synthetic": False},
        ]
        (day / "predictions.json").write_text(json.dumps(preds), encoding="utf-8")
        return lp, tmp_path / "daily"

    def test_analyze_readonly(self, tmp_path):
        lp, daily = self._fixture(tmp_path)
        before = lp.read_bytes()
        res = sc.analyze(lp, daily)
        assert res["n"] == 2
        assert res["excluded"] == {"legacy_or_other_chain": 1, "contaminated": 1}
        assert res["ledger_total"] == 4
        assert res["by_tier"]["<1.60"]["n"] == 1
        assert res["by_tier"]["1.60-2.50"]["n"] == 1
        assert res["overall"]["hits"] == 1
        assert res["brier_recheck_max_diff"] == 0.0
        # 只读：账本字节不变
        assert lp.read_bytes() == before

    def test_main_json(self, tmp_path, capsys):
        lp, daily = self._fixture(tmp_path)
        rc = sc.main(["--ledger", str(lp), "--daily-root", str(daily), "--json"])
        assert rc == 0
        res = json.loads(capsys.readouterr().out)
        assert res["n"] == 2

    def test_main_markdown(self, tmp_path, capsys):
        lp, daily = self._fixture(tmp_path)
        rc = sc.main(["--ledger", str(lp), "--daily-root", str(daily)])
        assert rc == 0
        out = capsys.readouterr().out
        for section in ("## 总体", "## 按赔率档", "## 按选向", "## 按联赛",
                        "## 赔率档×选向", "## 系统性偏差排名", "## 小样本层"):
            assert section in out