"""诊断引擎测试：验证它能从数据里主动发现结构性问题。

关键测试点：
  - 输出退化成常数（巴甲 draw=0.5025 那类）能被自动抓到
  - 融合结果劣于市场能被识别
  - 概率非法会被标记
  - 诊断器出错时显式暴露，不静默吞掉
  - 诊断发现能正确转成候选并走 audit 通道 ACCEPT
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evolution.harness import diagnostics as DG
from evolution.harness import diagnostic_candidates as DC


def _mk_row(draw_prob=0.28, market=None, final=None, league="测试联",
            actual_idx=0, pnl=None, home_xg=1.5, away_xg=1.2, tg=3):
    return {
        "league": league, "actual_idx": actual_idx,
        "final_prob": final or [0.36, draw_prob, 0.64 - draw_prob],
        "model_raw": [0.4, 0.3, 0.3],
        "market_fair": market, "pnl": pnl,
        "home_xg": home_xg, "away_xg": away_xg,
        "total_goals_actual": tg,
        "brier_market": 0.5, "brier_final": 0.5,
        "_has_market": market is not None,
    }


class TestOutputConstant(unittest.TestCase):
    def test_catches_constant_draw(self):
        """巴甲那类：draw 全为同一值 → 必须被发现。"""
        rows = [_mk_row(draw_prob=0.5025, league="巴甲",
                        final=[0.4, 0.5025, 0.0975]) for _ in range(20)]
        out = DG.d_output_constant(rows)
        self.assertTrue(out)
        self.assertEqual(out[0]["league"], "巴甲")

    def test_no_false_positive_on_varied_draw(self):
        rows = [_mk_row(draw_prob=0.25 + (i % 10) * 0.01, league="正常联")
                for i in range(30)]
        out = DG.d_output_constant(rows)
        self.assertEqual(out, [])


class TestWorseThanMarket(unittest.TestCase):
    def test_ignores_when_no_market(self):
        rows = [_mk_row(market=None) for _ in range(40)]
        self.assertEqual(DG.d_worse_than_market(rows), [])

    def test_flags_when_final_beats_market(self):
        rows = []
        for _ in range(40):
            r = _mk_row(market=[0.4, 0.3, 0.3])
            r["brier_market"] = 0.50
            r["brier_final"] = 0.56   # 融合后变差
            rows.append(r)
        out = DG.d_worse_than_market(rows)
        self.assertTrue(any(o["scope"] == "整体" for o in out))


class TestProbNotSumming(unittest.TestCase):
    def test_rounding_error_is_not_flagged(self):
        # round(x,4) 落盘会产生 0.9999 这类，不应误报
        rows = [_mk_row(final=[0.4044, 0.5025, 0.093]) for _ in range(10)]
        self.assertEqual(DG.d_prob_not_summing(rows), [])

    def test_real_invalid_is_flagged(self):
        rows = [_mk_row(final=[0.9, 0.5, 0.3]) for _ in range(5)]  # 和>1 且超界
        out = DG.d_prob_not_summing(rows)
        self.assertTrue(out)


class TestDiagnosticToCandidate(unittest.TestCase):
    def test_factual_goes_audit_channel(self):
        findings = {"output_constant": [{
            "id": "DIAG-OUTPUT-CONSTANT", "severity": "blocking",
            "league": "巴甲", "detail": "draw 钉死 0.5025", "n": 19,
        }]}
        cands = DC.diagnostics_to_candidates(findings)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertTrue(c.auto_mergeable)
        self.assertEqual(c.detect_by, "audit")
        self.assertTrue(c.is_issue, "诊断候选必须被 is_issue 识别，走专用通道")

    def test_effect_type_goes_backtest(self):
        findings = {"xg_drift": [{
            "id": "DIAG-XG-DRIFT", "severity": "medium",
            "league": "德甲", "detail": "低估 0.93", "n": 32,
        }]}
        cands = DC.diagnostics_to_candidates(findings)
        self.assertFalse(cands[0].auto_mergeable)
        self.assertEqual(cands[0].detect_by, "backtest")


class TestRunAllIsolation(unittest.TestCase):
    def test_error_is_exposed_not_swallowed(self):
        """诊断器抛异常时应暴露，不能静默成「无发现」。"""
        # 临时塞一个会抛错的诊断器
        def boom(ledger):
            raise RuntimeError("x")
        DG.ALL_DIAGNOSTICS = DG.ALL_DIAGNOSTICS + (boom,)
        try:
            res = DG.run_all([_mk_row() for _ in range(5)])
            self.assertTrue(any(k.endswith(":ERROR") for k in res))
        finally:
            DG.ALL_DIAGNOSTICS = DG.ALL_DIAGNOSTICS[:-1]


if __name__ == "__main__":
    unittest.main()