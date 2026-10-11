"""比分候选归一化与双源合并测试（2026-10-11）

这段逻辑从 main.py 的 1200 行编排函数里抽成纯函数。
行为必须逐位不变 —— 比分候选直接影响账本的 score_rank / score_hit。

`_reference_inline` 是抽取前的内联实现逐字拷贝，作为黄金基准。
"""

from __future__ import annotations

import random

import pytest

from engine.prediction.score_candidates import merge_score_candidates, normalize_scores


def _reference_inline(djyy_ts, mc_ts, xdiff, limit=8):
    """抽取前的内联实现（逐字拷贝），用于等价对照。"""

    def _norm_scores(ts):
        if not ts:
            return []
        out = []
        for i, it in enumerate(ts):
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                try:
                    p = float(it[2])
                except (TypeError, ValueError):
                    p = 0.0
                if p <= 0:
                    p = 1.0 / (i + 1)
                out.append((int(it[0]), int(it[1]), p))
            elif isinstance(it, (list, tuple)) and len(it) >= 2:
                out.append((int(it[0]), int(it[1]), 1.0 / (i + 1)))
        s = sum(x[2] for x in out) or 1.0
        return [(h, a, p / s) for h, a, p in out]

    djyy_norm = _norm_scores(djyy_ts)
    mc_norm = _norm_scores(mc_ts)
    if djyy_norm:
        reranked = list(djyy_norm)
        if xdiff >= 0.8:
            fac = lambda h, a: 1.3 if h + a >= 3 else (0.85 if (h, a) in ((1, 1), (0, 0)) else 1.0)
            reranked = [(h, a, round(p * fac(h, a), 6)) for h, a, p in reranked]
            reranked.sort(key=lambda x: -x[2])
        return ([(h, a, round(p, 4)) for h, a, p in reranked[:limit]],
                "djyy", djyy_norm, mc_norm)
    if mc_norm:
        return ([(h, a, round(p, 4)) for h, a, p in mc_norm[:limit]],
                "mc", djyy_norm, mc_norm)
    return None, None, djyy_norm, mc_norm


class TestNormalizeScores:
    def test_sums_to_one(self):
        out = normalize_scores([(1, 0, 0.4), (2, 1, 0.3), (0, 0, 0.3)])
        assert abs(sum(p for _, _, p in out) - 1.0) < 1e-9

    def test_missing_prob_falls_back_to_rank_decay(self):
        out = normalize_scores([(1, 0), (2, 1), (0, 0)])
        # 无概率 → 按排名取 1/1, 1/2, 1/3，总和 11/6，再归一化
        s = 1 + 1 / 2 + 1 / 3
        assert out == [
            (1, 0, (1.0) / s), (2, 1, (1 / 2) / s), (0, 0, (1 / 3) / s),
        ]

    def test_non_positive_prob_falls_back(self):
        out = normalize_scores([(1, 0, -1.0), (2, 1, 0.5)])
        assert all(p > 0 for _, _, p in out)
        assert abs(sum(p for _, _, p in out) - 1.0) < 1e-9

    def test_bad_prob_type_does_not_raise(self):
        assert len(normalize_scores([(1, 0, "abc"), (2, 1, 0.5)])) == 2

    def test_empty_and_none(self):
        assert normalize_scores(None) == []
        assert normalize_scores([]) == []

    def test_zero_sum_does_not_divide_by_zero(self):
        """全 0 概率 → 按排名兜底，归一化后仍为 1"""
        out = normalize_scores([(1, 0, 0.0), (0, 0, 0.0)])
        assert abs(sum(p for _, _, p in out) - 1.0) < 1e-9

    def test_scores_cast_to_int(self):
        out = normalize_scores([("1", "0", 0.5), (2.0, 1.0, 0.5)])
        assert out[0][0] == 1 and isinstance(out[0][0], int)


class TestMergeScoreCandidates:
    def test_djyy_wins_when_both_present(self):
        """实测：双源融合稀释命中率，DJYY 纯源最优。"""
        fused, src, dj, mc = merge_score_candidates([(1, 0, 0.5)], [(2, 1, 0.4)], 0.3)
        assert src == "djyy"
        assert fused == [(1, 0, 1.0)]
        assert mc

    def test_mc_fallback_when_djyy_missing(self):
        fused, src, dj, mc = merge_score_candidates(None, [(2, 1, 0.4)], 0.3)
        assert src == "mc"
        assert fused == [(2, 1, 1.0)]

    def test_both_missing(self):
        fused, src, _, _ = merge_score_candidates(None, None, 1.0)
        assert fused is None and src is None

    def test_rerank_only_when_xdiff_ge_0_8(self):
        ts = [(1, 1, 0.4), (3, 0, 0.35), (0, 0, 0.25)]
        weak, _, _, _ = merge_score_candidates(ts, None, 0.7)
        strong, _, _, _ = merge_score_candidates(ts, None, 0.8)
        assert weak[0][:2] == (1, 1)
        assert strong[0][:2] == (3, 0)

    def test_rerank_demotes_low_scoring_draws(self):
        ts = [(1, 1, 0.30), (0, 0, 0.28), (3, 0, 0.42)]
        strong, _, _, _ = merge_score_candidates(ts, None, 1.5)
        assert strong[0][:2] == (3, 0)

    def test_limit_respected(self):
        ts = [(i, 0, 1.0) for i in range(20)]
        fused, _, _, _ = merge_score_candidates(ts, None, 0.0, limit=8)
        assert len(fused) == 8

    def test_prob_rounded_to_4(self):
        ts = [(1, 0, 1 / 3), (2, 1, 1 / 3), (0, 0, 1 / 3)]
        fused, _, _, _ = merge_score_candidates(ts, None, 0.0)
        assert all(p == round(p, 4) for _, _, p in fused)


class TestEquivalenceWithInline:
    """黄金对照：随机输入下必须与抽取前实现完全一致。"""

    def test_random_inputs_identical(self):
        rng = random.Random(20261011)
        for _ in range(300):
            def gen():
                if rng.random() < 0.25:
                    return None
                return [(rng.randint(0, 4), rng.randint(0, 4),
                         round(rng.random(), 3)) for _ in range(rng.randint(1, 12))]
            djyy_ts, mc_ts, xdiff = gen(), gen(), round(rng.uniform(0, 2.5), 2)
            assert (merge_score_candidates(djyy_ts, mc_ts, xdiff, 8)
                    == _reference_inline(djyy_ts, mc_ts, xdiff, 8))

    @pytest.mark.parametrize("xdiff", [0.0, 0.79, 0.8, 1.0, 3.0])
    def test_boundary_xdiff_identical(self, xdiff):
        ts = [(1, 1, 0.3), (3, 0, 0.3), (0, 0, 0.2), (2, 2, 0.2)]
        assert (merge_score_candidates(ts, None, xdiff)
                == _reference_inline(ts, None, xdiff))

    def test_malformed_inputs_identical(self):
        cases = [
            ([], [], 0.0),
            ([(1, 0)], [(2, 1, 0.5)], 1.0),
            ([(1, 0, "x"), (2, 1, -1)], [(0, 0, 0)], 0.5),
            ([(1, 0, 0.0), (0, 0, 0.0)], None, 2.0),
        ]
        for djyy_ts, mc_ts, xdiff in cases:
            assert (merge_score_candidates(djyy_ts, mc_ts, xdiff)
                    == _reference_inline(djyy_ts, mc_ts, xdiff))

