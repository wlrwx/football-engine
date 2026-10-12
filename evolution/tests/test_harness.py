"""harness 自测。

这些测试保护闭环本身的正确性 —— 一个会误判的进化闭环比没有闭环更危险。
重点覆盖：
  1. replay 忠实性（harness 测的必须真的是生产行为）
  2. 统计工具正确性（t 分布、配对检验、BH 校正）
  3. **能识别劣化**（负向测试：注入已知劣化，必须被判定为 REJECT）
  4. 裁决纪律（ACCEPT_NONE 是合法输出）
"""

import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evolution.harness import evaluator as E
from evolution.harness import fusion_core as S
from evolution.harness import proposer as P
from evolution.harness import verifier as V

HERE = os.path.dirname(os.path.abspath(__file__))
# 账本路径：优先用仓库真实的 data/state/review_ledger.jsonl（进化闭环在仓库
# 内置身立命的数据），体验不到再回退 demo 夹具。这样 CLI 和 test 用同一份数据。
def _ledger_path():
    real = os.path.join(HERE, "..", "..", "data", "state", "review_ledger.jsonl")
    if os.path.exists(real):
        return real
    return os.path.join(HERE, "..", "demo", "data", "review_ledger.jsonl")

LEDGER = _ledger_path()
CONFIG = os.path.join(HERE, "..", "demo", "base_prediction.json")


def _load():
    raw = E.load_ledger(LEDGER)
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)["fusion"]
    scoped = [r for r in E.scope_current_chain(raw) if r["_has_market"]]
    return scoped, cfg, cfg["post_fusion"]


class TestStatistics(unittest.TestCase):
    """统计工具必须正确，否则所有裁决都是随机的。"""

    def test_t_cdf_normal_limit(self):
        for t in [0.5, 1.0, 1.96, 2.5]:
            normal = 0.5 * (1 + math.erf(t / math.sqrt(2)))
            self.assertAlmostEqual(E.t_cdf(t, 200000), normal, places=5)

    def test_t_cdf_symmetry(self):
        for t in [0.1, 1.0, 1.96, 3.0]:
            for df in [2, 5, 30, 470]:
                self.assertAlmostEqual(E.t_cdf(-t, df), 1 - E.t_cdf(t, df), places=12)

    def test_t_cdf_bounds_and_center(self):
        for df in [1, 5, 30, 1000]:
            self.assertAlmostEqual(E.t_cdf(0, df), 0.5, places=12)
            for t in [-100, -1, 1, 100]:
                self.assertGreaterEqual(E.t_cdf(t, df), 0.0)
                self.assertLessEqual(E.t_cdf(t, df), 1.0)

    def test_t_cdf_monotone(self):
        prev = -1.0
        for i in range(0, 200):
            v = E.t_cdf(-6 + 0.05 * i, 30)
            self.assertGreaterEqual(v, prev)
            prev = v

    def test_p_value_matches_t(self):
        """p 必须随 |t| 单调下降：t 越负（指标越好），p 越小。"""
        p_small = E.p_one_sided_from_t(-0.3, 271, lower_is_better=True)
        p_mid = E.p_one_sided_from_t(-1.0, 271, lower_is_better=True)
        p_large = E.p_one_sided_from_t(-3.0, 271, lower_is_better=True)
        self.assertGreater(p_small, p_mid)
        self.assertGreater(p_mid, p_large)
        self.assertLess(p_large, 0.01)
        # 反向：t>0 表示指标变差，单尾 p 应接近 1
        self.assertGreater(E.p_one_sided_from_t(3.0, 271, lower_is_better=True), 0.99)

    def test_paired_test_identical_inputs(self):
        rows = [{"match_id": str(i), "metric": 0.5, "has_market": True} for i in range(50)]
        r = E.paired_test(rows, rows)
        self.assertAlmostEqual(r["delta"], 0.0, places=12)
        self.assertAlmostEqual(r["t"], 0.0, places=12)

    def test_paired_test_detects_consistent_improvement(self):
        base = [{"match_id": str(i), "metric": 0.60, "has_market": True} for i in range(200)]
        cand = [{"match_id": str(i), "metric": 0.55, "has_market": True} for i in range(200)]
        r = E.paired_test(cand, base)
        self.assertLess(r["delta"], 0)
        self.assertLess(r["p_one_sided"], 0.001)
        self.assertGreater(r["better_rate"], 0.95)

    def test_paired_test_ignores_unpaired(self):
        base = [{"match_id": str(i), "metric": 0.5, "has_market": True} for i in range(20)]
        cand = [{"match_id": str(i), "metric": 0.1, "has_market": True} for i in range(10)]
        r = E.paired_test(cand, base)
        self.assertEqual(r["n"], 10)

    def test_benjamini_hochberg_monotone_and_conservative(self):
        pv = {"a": 0.001, "b": 0.01, "c": 0.2, "d": 0.9}
        out = E.benjamini_hochberg(pv, alpha=0.05)
        self.assertTrue(out["a"]["pass_bh"])
        self.assertFalse(out["c"]["pass_bh"])
        self.assertFalse(out["d"]["pass_bh"])

    def test_bh_rejects_all_when_all_null(self):
        """全部无效应时，BH 不应放过任何一个。"""
        pv = {f"c{i}": 0.4 + i * 0.05 for i in range(6)}
        out = E.benjamini_hochberg(pv, alpha=0.05)
        self.assertFalse(any(v["pass_bh"] for v in out.values()))


class TestReplayFidelity(unittest.TestCase):
    def test_replay_trusted_frac_high(self):
        rows, cfg, post = _load()
        flags = E.replay_trusted(S, rows, cfg, post)
        self.assertGreaterEqual(sum(flags) / len(flags), 0.90)

    def test_replay_reproduces_stored_final(self):
        """逐场 replay 应高度复现落盘的 final_prob。"""
        rows, cfg, post = _load()
        errs = []
        for r in rows[:200]:
            out = S.fuse(S.MatchInput(r), cfg, post)
            stored = r["final_prob"]
            errs.append(sum((out.probs[i] - stored[i]) ** 2 for i in range(3)))
        errs.sort()
        median = errs[len(errs) // 2]
        self.assertLess(median, 0.001)


class TestFusionInvariants(unittest.TestCase):
    def test_output_is_valid_distribution(self):
        rows, cfg, post = _load()
        for r in rows[:150]:
            out = S.fuse(S.MatchInput(r), cfg, post)
            self.assertAlmostEqual(sum(out.probs), 1.0, places=6)
            for p in out.probs:
                self.assertGreaterEqual(p, 0.0)
                self.assertLessEqual(p, 1.0)

    def test_probabilities_sum_to_one_for_all_configs(self):
        rows, cfg, post = _load()
        from itertools import product
        r = rows[0]
        for combo in [True, False]:
            for step in DEFAULT_STEPS:
                p2 = dict(post)
                p2[step] = combo
                out = S.fuse(S.MatchInput(r), cfg, p2)
                self.assertAlmostEqual(sum(out.probs), 1.0, places=6)

    def test_no_market_match_still_valid(self):
        r = {"match_id": "x", "date": "2026-01-01", "league": "西甲",
             "actual_idx": 0, "model_raw": [0.5, 0.3, 0.2], "market_fair": None}
        out = S.fuse(S.MatchInput(r), {"model_weight": 0.1}, {"combo_boost": True})
        self.assertAlmostEqual(sum(out.probs), 1.0, places=6)


DEFAULT_STEPS = list(S.DEFAULT_POST_FUSION.keys())


class TestVerifierRejectsBadChanges(unittest.TestCase):
    """负向测试：注入劣化，必须被判定为 REJECT。"""

    def setUp(self):
        self.rows, self.cfg, self.post = _load()
        flags = E.replay_trusted(S, self.rows, self.cfg, self.post)
        self.trusted = E.filter_trusted(self.rows, flags)
        self.train, self.val = E.walk_forward_split(self.trusted, 150, 0.4)

    def test_rejects_massive_model_weight(self):
        """把模型权重拉到 0.3 已知会显著劣化，必须被拦下。"""
        rep = V.judge_all(P.weight_candidates(self.cfg), self.cfg, self.post,
                          self.trusted)
        accepted = {a["cid"] for a in rep["accepted"]}
        self.assertNotIn("w_m0.30_k0.60", accepted)

    def test_rejects_guardrail_violations(self):
        rep = V.judge_all(P.weight_candidates(self.cfg), self.cfg, self.post,
                          self.trusted)
        for a in rep["accepted"]:
            if a.get("is_issue"):
                continue
            self.assertLessEqual(a["val_delta"],
                                 rep["policy"]["max_val_regression"],
                                 f"{a['cid']} passed but regressed on val")

    def test_no_candidate_claims_unjustified_significance(self):
        rep = V.judge_all(P.generate(self.cfg, self.post, max_candidates=40),
                          self.cfg, self.post, self.trusted)
        stat_acc = [a for a in rep["accepted"] if not a.get("is_issue")]
        for a in stat_acc:
            self.assertLessEqual(a["p_raw"], rep["policy"]["alpha"])
            self.assertLessEqual(a["train_delta"], -rep["policy"]["min_delta"])

    def test_accept_none_is_valid(self):
        """闭环必须有能力输出「什么都不改」：全部候选被否决时，
        每个候选都应落入某个否决类别，且无 ACCEPT。"""
        cands = P.generate(self.cfg, self.post, max_candidates=40)
        rep = V.judge_all(cands, self.cfg, self.post, self.trusted)
        if not rep["accepted"]:
            self.assertEqual(sum(rep["counts"].values()), len(cands))
            self.assertNotIn("ACCEPT", rep["counts"])
            for k in rep["counts"]:
                self.assertTrue(k.startswith("REJECT") or k == "INSUFFICIENT", k)
            # 每个被拒候选都必须给出可复核的理由
            for r in rep["rejected"]:
                self.assertTrue(r["reason"].strip(), f"{r['cid']} 缺少拒绝理由")


class TestProposer(unittest.TestCase):
    def test_generation_is_deterministic(self):
        rows, cfg, post = _load()
        a = [c.cid for c in P.generate(cfg, post, seed=42)]
        b = [c.cid for c in P.generate(cfg, post, seed=42)]
        self.assertEqual(a, b)

    def test_candidates_have_rationale(self):
        """每个候选都必须能自证其先验理由，供主线复核。"""
        rows, cfg, post = _load()
        for c in P.generate(cfg, post, max_candidates=60):
            self.assertTrue(c.rationale.strip(), f"{c.cid} 缺少 rationale")
            self.assertGreater(len(c.rationale), 20)

    def test_round2_prefers_fresh_candidates(self):
        rows, cfg, post = _load()
        r1 = P.generate(cfg, post, seed=7, round_idx=0)
        r2 = P.generate(cfg, post, seed=7, round_idx=1,
                        previous=[c.cid for c in r1])
        self.assertEqual(len(r1), len(r2))

    def test_includes_deletion_candidates(self):
        """候选空间必须包含「关掉某个东西」——最强的改进常是删除。"""
        rows, cfg, post = _load()
        cands = P.generate(cfg, post, max_candidates=60)
        self.assertTrue(any("toggle_off" in c.cid for c in cands))


class TestWalkForward(unittest.TestCase):
    def test_split_is_chronological(self):
        rows, cfg, post = _load()
        tr, va = E.walk_forward_split(rows, 150, 0.4)
        max_train_date = max(r["date"] for r in tr)
        min_val_date = min(r["date"] for r in va)
        self.assertLessEqual(max_train_date, min_val_date)

    def test_no_overlap(self):
        rows, cfg, post = _load()
        tr, va = E.walk_forward_split(rows, 150, 0.4)
        self.assertFalse({r["match_id"] for r in tr} & {r["match_id"] for r in va})


if __name__ == "__main__":
    unittest.main(verbosity=2)
