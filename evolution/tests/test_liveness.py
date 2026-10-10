"""活体复现门控的测试：闭环必须收敛，不能反复提已修好的问题。

这是闭环最重要的性质之一：它读的是账本历史数据，但 bug 的载体是当前代码。
如果分不清「代码已修」与「账本里还留着旧记录」，闭环会永不收敛——
每次扫描都重新 "发现" 同一个已经修好的巴甲 draw 钉死问题。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from evolution.harness import liveness as LV


def _make_repo() -> Path:
    """搭一个最小可复现的假仓库目录树。"""
    t = Path(tempfile.mkdtemp())
    # data/state/league_params.json
    sp = t / "data" / "state"
    sp.mkdir(parents=True, exist_ok=True)
    return t


class TestLivenessGate(unittest.TestCase):

    def test_draw_baseline_fixed_is_gated(self):
        """巴甲 draw_baseline 已经迁移成真实平局率 → 不应再提。"""
        repo = _make_repo()
        lp = repo / "data" / "state" / "league_params.json"
        lp.write_text(json.dumps({"巴甲": {"draw_baseline": 0.266}}), encoding="utf-8")
        allow, reason = LV.gate("DRAW-ANCHOR-JUDGMENT-AS-RATE", repo)
        self.assertFalse(allow, reason)
        self.assertIn("已修复", reason)

    def test_draw_baseline_broken_still_reproduced(self):
        """巴甲 draw_baseline 仍是 0.6 → 必须仍提。"""
        repo = _make_repo()
        lp = repo / "data" / "state" / "league_params.json"
        lp.write_text(json.dumps({"巴甲": {"draw_baseline": 0.6}}), encoding="utf-8")
        allow, reason = LV.gate("DRAW-ANCHOR-JUDGMENT-AS-RATE", repo)
        self.assertTrue(allow, reason)

    def test_combo_boost_off_is_gated(self):
        """combo_boost 已在 config 关闭 → 不再提。"""
        repo = _make_repo()
        (repo / "config").mkdir(parents=True)
        cfg = {"fusion": {"post_fusion": {"combo_boost": False}}}
        (repo / "config" / "prediction.json").write_text(
            json.dumps(cfg), encoding="utf-8")
        allow, reason = LV.gate("COMBO-BOOST-ENTROPY-REDUCTOR", repo)
        self.assertFalse(allow, reason)

    def test_probe_unavailable_is_conservative(self):
        """无探针的 issue 保守不提（不误报为健康）。"""
        repo = _make_repo()
        allow, reason = LV.gate("SOME-UNKNOWN-ISSUE", repo)
        self.assertFalse(allow, reason)
        self.assertIn("保守", reason)

    def test_probe_missing_file_is_conservative(self):
        """探针要读的文件不存在 → 保守不提，而不是当成已修复。"""
        repo = _make_repo()  # 没有 league_params.json
        allow, reason = LV.gate("DRAW-ANCHOR-JUDGMENT-AS-RATE", repo)
        self.assertFalse(allow, reason)
        self.assertIn("探针", reason)


if __name__ == "__main__":
    unittest.main()