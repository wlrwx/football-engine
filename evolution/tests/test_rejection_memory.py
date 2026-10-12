"""拒绝记忆与自适应候选扩充的测试。

这两块是闭环的后两个短板：
  (A) 候选池在第 2 轮起会穷尽 —— 粗网格跑完就没了，进化退化为重复采样；
  (B) 主线否决后闭环会反复提同一个改动 —— 白白消耗裁决带宽。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evolution.harness import proposer as P
from evolution.harness import rejection_memory as RM

BASE = json.loads((ROOT / "demo" / "base_prediction.json").read_text(encoding="utf-8"))
CFG = BASE["fusion"]
POST = CFG["post_fusion"]


class TestAdaptivePool(unittest.TestCase):
    """(A) 候选池不应在第 2 轮穷尽。"""

    def test_round2_yields_fresh_candidates(self):
        r1 = P.generate(CFG, POST, max_candidates=60, seed=42, round_idx=0)
        r2 = P.generate(CFG, POST, max_candidates=60, seed=42, round_idx=1,
                        previous=[c.cid for c in r1])
        ids1 = {c.cid for c in r1}
        fresh = [c for c in r2 if c.cid not in ids1]
        self.assertGreater(len(fresh), 0,
                           "第 2 轮无新候选 —— 进化退化为重复采样")

    def test_adaptive_fills_pool_to_target(self):
        cands = P.generate(CFG, POST, max_candidates=60, seed=42, adaptive=True)
        self.assertGreaterEqual(len(cands), 55,
                                f"adaptive 扩充后候选数仍不足：{len(cands)}")

    def test_new_families_present(self):
        cands = P.generate(CFG, POST, max_candidates=60, seed=42, adaptive=True)
        fams = {c.family for c in cands}
        for f in ("fusion_weight_fine", "temperature", "recalibration"):
            self.assertIn(f, fams, f"自适应扩充族缺失：{f}")

    def test_deterministic(self):
        a = [c.cid for c in P.generate(CFG, POST, max_candidates=60, seed=7,
                                       adaptive=True)]
        b = [c.cid for c in P.generate(CFG, POST, max_candidates=60, seed=7,
                                       adaptive=True)]
        self.assertEqual(a, b)


class TestRejectionMemory(unittest.TestCase):
    """(B) 主线否决后不应重复提。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.mem = RM.RejectionMemory(Path(self.td.name) / "m.json", ttl_rounds=5)
        self.cands = P.generate(CFG, POST, max_candidates=60, seed=42, adaptive=True)

    def tearDown(self):
        self.td.cleanup()

    def test_fingerprint_stable_across_seeds(self):
        """指纹不应依赖 seed —— 否则换个种子就绕过了去重。"""
        c1 = P.generate(CFG, POST, max_candidates=60, seed=1, adaptive=True)
        c2 = P.generate(CFG, POST, max_candidates=60, seed=999, adaptive=True)
        m1 = {RM.fingerprint(c) for c in c1}
        m2 = {RM.fingerprint(c) for c in c2}
        self.assertEqual(m1 & m2, m1 & m2)  # 集合可比
        shared = m1 & m2
        self.assertGreater(len(shared), 0, "不同 seed 间无共同指纹，去重形同虚设")

    def test_single_candidate_suppressed(self):
        victim = self.cands[0]
        self.mem.reject(victim, "样本量不足")
        kept, dropped = RM.apply_suppression(self.cands, self.mem)
        self.assertNotIn(victim.cid, [c.cid for c in kept])
        self.assertIn(victim.cid, [d["cid"] for d in dropped])

    def test_family_suppression(self):
        fam = "fusion_weight"
        victim = next(c for c in self.cands if c.family == fam)
        self.mem.reject(victim, "本轮不调权重", suppress_family=True)
        kept, dropped = RM.apply_suppression(self.cands, self.mem)
        kept_fams = {c.family for c in kept}
        self.assertNotIn(fam, kept_fams, "同族候选未被抑制")
        self.assertGreater(len(dropped), 1, "同族抑制只命中 1 条")

    def test_idempotent(self):
        v = self.cands[0]
        self.mem.reject(v, "r1")
        self.mem.reject(v, "r2")
        self.mem.reject(v, "r3")
        self.assertEqual(self.mem.summary()["total"], 1, "重复否决产生了重复条目")

    def test_ttl_expiry_releases(self):
        """否决不应永久锁死候选 —— 样本量翻倍后应解禁。"""
        v = self.cands[0]
        self.mem.start_round(0)
        self.mem.reject(v, "样本不足", ttl_rounds=5)
        kept0, dropped0 = RM.apply_suppression(self.cands, self.mem)
        self.assertEqual(len(dropped0), 1)

        self.mem.start_round(99)
        kept99, dropped99 = RM.apply_suppression(self.cands, self.mem)
        self.assertEqual(len(dropped99), 0, "TTL 到期后仍被抑制")
        self.assertEqual(self.mem.prune(), 1, "过期条目未被剪除")

    def test_persistence_roundtrip(self):
        v = self.cands[0]
        self.mem.start_round(2)
        self.mem.reject(v, "持久化测试")
        reloaded = RM.RejectionMemory(Path(self.td.name) / "m.json")
        self.assertEqual(reloaded.summary()["total"], 1)
        self.assertEqual(reloaded.summary()["current_round"], 2)
        kept, dropped = RM.apply_suppression(self.cands, reloaded)
        self.assertEqual(len(dropped), 1, "重载后抑制失效")

    def test_suppression_is_explainable(self):
        """每条抑制都必须带原因 —— 主线需要知道闭环为什么沉默。"""
        v = self.cands[0]
        self.mem.reject(v, "业务上暂不接受")
        _, dropped = RM.apply_suppression(self.cands, self.mem)
        for d in dropped:
            self.assertTrue(d["reason"].strip(),
                            f"{d['cid']} 被抑制但无原因说明")


if __name__ == "__main__":
    unittest.main()
