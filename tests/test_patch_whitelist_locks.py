"""C1/C2/C3 三道锁的回归测试。

放在 `tests/` 而不是 `evolution/tests/`，因为 CI 的 test.yml 只跑
`python -m pytest tests/` —— 写在 evolution/tests/ 里的测试不会被执行，
那等于锁没有回归保护。

三道锁分别对应 workbuddy 的建议：
  C1 patch 白名单禁触 metrics/ backtest/ holdout/ ledger/ expectations.json
  C2 自动 PR 强制附反事实对照（不打补丁 vs 打补丁，差值 + bootstrap CI + n）
  C3 主指标定义 CODEOWNERS 锁死给人
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evolution.harness.patch_engine import (  # noqa: E402
    FORBIDDEN, ForbiddenTarget, PatchError, assert_patchable,
    forbidden_reason, forbidden_symbol_reason,
)
from evolution.harness.counterfactual import (  # noqa: E402
    CounterfactualMissing, paired_counterfactual, render_counterfactual_md,
    require_counterfactual,
)


# ---------------------------------------------------------------- C1

class TestC1PathWhitelist(unittest.TestCase):
    """白名单必须拦得住，且不能是一把锁住空集的假锁。"""

    def test_forbidden_target_raises(self):
        with self.assertRaises(ForbiddenTarget):
            assert_patchable("engine/backtest/gate_test.py", "x = 1\n")

    def test_forbidden_target_is_patch_error(self):
        """调用方 catch PatchError 就该同时兜住越权 —— 继承关系不能断。"""
        self.assertTrue(issubclass(ForbiddenTarget, PatchError))

    def test_allowed_paths_pass(self):
        """当前唯一的补丁目标 engine/prediction/dixon_coles.py 必须放行，
        否则这把锁一上线就把现有补丁全挡死了。"""
        for ok in ("engine/prediction/dixon_coles.py",
                   "engine/prediction/fusion.py",
                   "engine/config_audit.py"):
            assert_patchable(ok, "x = 1\n")

    def test_every_forbidden_entry_is_a_real_repo_path(self):
        """FORBIDDEN 里每一条都必须对应仓库里真实存在的路径。

        workbuddy 原文写的是 metrics/ backtest/ holdout/ ledger/ expectations.json，
        但本仓库没有 metrics/ holdout/ ledger/ 目录，也没有 expectations.json。
        照字面实现就是一把锁住空集的假锁 —— 这里逐条核对，
        防止后来者往 FORBIDDEN 里加一个不存在的路径还以为有防护。
        """
        import subprocess
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=str(ROOT), capture_output=True,
            text=True, check=True).stdout.split()
        tracked = {t.replace("\\", "/") for t in tracked}
        for entry in FORBIDDEN:
            if entry.endswith("/"):
                hits = [t for t in tracked if t.startswith(entry)]
                self.assertTrue(hits, f"{entry} 在仓库里没有任何文件 —— 假锁")
            else:
                self.assertIn(entry, tracked, f"{entry} 不在仓库里 —— 假锁")

    def test_real_evaluation_paths_are_covered(self):
        """反向核对：仓库里现存的评估层文件必须被 FORBIDDEN 覆盖。"""
        self.assertTrue(forbidden_reason("engine/backtest/runner.py"))
        self.assertTrue(forbidden_reason("data/state/review_ledger.jsonl"))
        self.assertTrue(forbidden_reason("data/state/market_baseline.json"))
        self.assertTrue(forbidden_reason("engine/prediction/calibration.py"))
        # 不能误伤
        self.assertEqual(forbidden_reason("engine/prediction/fusion.py"), "")
        self.assertEqual(forbidden_reason("scripts/analyze_market_baseline.py"), "")

    def test_path_normalisation(self):
        """../ 绕过、绝对路径、点号前缀都必须被识别成同一个目标。"""
        for variant in ("./engine/backtest/runner.py",
                        "/engine/backtest/runner.py",
                        "engine\\backtest\\runner.py"):
            self.assertTrue(forbidden_reason(variant),
                            f"{variant} 绕过了白名单")

    def test_symbol_lock_catches_relocated_metric(self):
        """文件不在名单内、但把指标函数搬进来重定义，也必须拦下。"""
        sneaky = "def brier(probs, actual_idx):\n    return 0.0\n"
        self.assertTrue(forbidden_symbol_reason(sneaky))
        with self.assertRaises(ForbiddenTarget):
            assert_patchable("engine/prediction/fusion.py", sneaky)

    def test_symbol_lock_does_not_trip_on_call_sites(self):
        """只是**调用** brier() 不算越权，只有定义才算。"""
        src = "def score(p, y):\n    return brier(p, y) + rps(p, y)\n"
        self.assertEqual(forbidden_symbol_reason(src), "")

    def test_symbol_lock_ignores_syntax_errors(self):
        """语法错误的裁决权归 compiles()，这里不越权判死。"""
        self.assertEqual(forbidden_symbol_reason("def brier(:\n"), "")


# ---------------------------------------------------------------- C2

def _per_match(deltas: dict[str, float], has_market: bool = True) -> list[dict]:
    return [{"match_id": k, "metric": v, "has_market": has_market,
             "hit": 1.0} for k, v in deltas.items()]


class TestC2CounterfactualGate(unittest.TestCase):

    def test_missing_counterfactual_is_rejected(self):
        with self.assertRaises(CounterfactualMissing):
            require_counterfactual({"issue": "X", "verify": "passed"})

    def test_incomplete_counterfactual_is_rejected(self):
        for bad in ({"ok": True, "n": 10, "delta": -0.01},          # 缺 CI
                    {"ok": True, "n": 10, "ci_low": -0.02, "ci_high": 0.01},
                    {"ok": False, "reason": "样本不足"}):
            with self.assertRaises(CounterfactualMissing):
                require_counterfactual({"issue": "X", "counterfactual": bad})

    def test_tiny_n_is_rejected(self):
        with self.assertRaises(CounterfactualMissing):
            require_counterfactual({"issue": "X", "counterfactual": {
                "ok": True, "n": 2, "delta": 0.0, "ci_low": 0.0, "ci_high": 0.0}})

    def test_valid_counterfactual_passes(self):
        cf = paired_counterfactual(
            _per_match({str(i): 0.20 - 0.001 * i for i in range(50)}),
            _per_match({str(i): 0.20 for i in range(50)}))
        require_counterfactual({"issue": "X", "counterfactual": cf})

    def test_delta_and_ci_direction(self):
        """候选全面更优 → delta<0、CI 上端也 <0、significant=True。"""
        cf = paired_counterfactual(
            _per_match({str(i): 0.15 for i in range(200)}),
            _per_match({str(i): 0.20 for i in range(200)}))
        self.assertEqual(cf["n"], 200)
        self.assertAlmostEqual(cf["delta"], -0.05, places=6)
        self.assertLess(cf["ci_high"], 0.0)
        self.assertTrue(cf["significant"])
        self.assertEqual(cf["better_rate"], 1.0)

    def test_no_difference_gives_ci_crossing_zero(self):
        """完全没差别时 CI 必须跨 0、significant=False —— 不能编出显著性。"""
        cf = paired_counterfactual(
            _per_match({str(i): 0.20 for i in range(200)}),
            _per_match({str(i): 0.20 for i in range(200)}))
        self.assertAlmostEqual(cf["delta"], 0.0, places=9)
        self.assertFalse(cf["significant"])
        self.assertLessEqual(cf["ci_low"], 0.0)
        self.assertGreaterEqual(cf["ci_high"], 0.0)

    def test_bootstrap_is_reproducible(self):
        """固定 seed → 同样输入必须给出同样 CI，否则 PR 上的数字无法复核。"""
        cand = _per_match({str(i): 0.20 + (i % 7) * 0.001 for i in range(120)})
        base = _per_match({str(i): 0.20 + (i % 5) * 0.001 for i in range(120)})
        a = paired_counterfactual(cand, base, n_boot=500)
        b = paired_counterfactual(cand, base, n_boot=500)
        self.assertEqual((a["delta"], a["ci_low"], a["ci_high"]),
                         (b["delta"], b["ci_low"], b["ci_high"]))

    def test_pairing_ignores_mismatched_market_flag(self):
        """has_market 不一致的样本不配对，n 必须小于输入条数。"""
        cand = _per_match({str(i): 0.2 for i in range(10)}, has_market=True)
        base = _per_match({str(i): 0.2 for i in range(5)}, has_market=True)
        base += _per_match({str(i): 0.2 for i in range(5, 10)}, has_market=False)
        cf = paired_counterfactual(cand, base)
        self.assertEqual(cf["n"], 5)

    def test_render_contains_required_fields(self):
        cf = paired_counterfactual(
            _per_match({str(i): 0.15 for i in range(100)}),
            _per_match({str(i): 0.20 for i in range(100)}))
        md = render_counterfactual_md(cf)
        for token in ("反事实", "配对样本 n", "bootstrap 95% CI", "差值 delta"):
            self.assertIn(token, md)

    def test_render_marks_missing_cf(self):
        md = render_counterfactual_md({"ok": False, "reason": "样本不足"})
        self.assertIn("❌", md)


# ---------------------------------------------------------------- C3

class TestC3Codeowners(unittest.TestCase):
    """C1 与 C3 是同一份保护清单的代码级与流程级两面，不能各自漂移。"""

    def setUp(self):
        self.codeowners = ROOT / ".github" / "CODEOWNERS"
        self.assertTrue(self.codeowners.exists(), ".github/CODEOWNERS 不存在")
        self.text = self.codeowners.read_text(encoding="utf-8")

    def test_every_forbidden_path_has_an_owner(self):
        """FORBIDDEN 里每条都必须在 CODEOWNERS 里出现，否则 C3 漏了 C1 保护的东西。"""
        lines = [ln.strip() for ln in self.text.splitlines()]
        patterns = [ln.split()[0] for ln in lines
                    if ln and not ln.startswith("#") and ln.split()]
        for entry in FORBIDDEN:
            self.assertTrue(
                any(entry.rstrip("/") in p or p in entry for p in patterns),
                f"CODEOWNERS 未覆盖 FORBIDDEN 条目 {entry}")

    def test_metric_file_has_owner(self):
        self.assertIn("evolution/harness/evaluator.py", self.text)
        self.assertIn("@wlrwx", self.text)

    def test_verdict_definitions_have_owner(self):
        """改判据等于改结论，实验登记与裁决契约必须锁住。"""
        self.assertIn("evolution/policy/MAINLINE_CONTRACT.md", self.text)
        self.assertIn("docs/EXPERIMENTS.md", self.text)


if __name__ == "__main__":
    unittest.main()
