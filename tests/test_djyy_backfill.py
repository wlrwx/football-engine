"""结算行 DJYY 回填逻辑测试（2026-10-11）

背景：CI 被 Cloudflare 屏蔽期间生成的 predictions.json 没有 djyy_model_prob，
结算入账后 djyy_prob 全 null，融合优化器因此把 DJYY 权重压到 0——第三通道
静默死亡 6 周才被发现。修复：结算时按 _djyy_id 从本地 context_cache 回填。
"""
from engine.review.post_match import build_djyy_cache_map, resolve_djyy_prob


def test_build_map_keeps_only_enriched_entries():
    cache = {
        "enrich_19648047": {"djyy_id": 19648047, "match_id": "2026-10-11_周日001",
                            "model_probs": {"home": 0.2, "draw": 0.3, "away": 0.5}},
        "19719094": {"djyy_id": 19719094, "model_probs": None},
        # 富化失败条目：有 home 键但值为 None，不得入索引
        "19714632": {"djyy_id": 19714632,
                     "model_probs": {"home": None, "draw": 0.3, "away": 0.7}},
        # lineup-only 存根：无 djyy_id / model_probs
        "stub": {"lineups": None, "lineups_tried_at": "2026-10-11T00:25:31"},
        "bad": "not-a-dict",
    }
    m = build_djyy_cache_map(cache)
    # 双键：djyy_id（本地预测反查）+ match_id（CI 预测反查）
    assert m["19648047"]["home"] == 0.2
    assert m["2026-10-11_周日001"]["home"] == 0.2
    assert "19719094" not in m and "19714632" not in m


def test_resolve_prefers_prediction_record():
    pred = {"_djyy_id": 1,
            "djyy_model_prob": {"home": 0.4, "draw": 0.3, "away": 0.3}}
    prob, backfilled = resolve_djyy_prob(
        pred, {"1": {"home": 0.1, "draw": 0.2, "away": 0.7}})
    assert prob == [0.4, 0.3, 0.3]
    assert backfilled is False


def test_resolve_backfills_from_cache_by_djyy_id():
    # CI 版预测：无 djyy_model_prob，只有 _djyy_id
    pred = {"_djyy_id": 19648047, "djyy_model_prob": None}
    cache_map = {str(19648047): {"home": 0.1863, "draw": 0.2478, "away": 0.5659}}
    prob, backfilled = resolve_djyy_prob(pred, cache_map)
    assert prob == [0.1863, 0.2478, 0.5659]
    assert backfilled is True


def test_resolve_backfills_by_match_id_when_djyy_id_missing():
    # CI 版预测可能连 _djyy_id 都没有（其富化失败），只能按 match_id 反查
    pred = {"match_id": "2026-10-11_周日001", "djyy_model_prob": None}
    cache_map = {"2026-10-11_周日001": {"home": 0.2, "draw": 0.25, "away": 0.55}}
    prob, backfilled = resolve_djyy_prob(pred, cache_map)
    assert prob == [0.2, 0.25, 0.55]
    assert backfilled is True


def test_resolve_returns_none_when_no_source():
    prob, backfilled = resolve_djyy_prob({"_djyy_id": None, "djyy_model_prob": None}, {})
    assert prob is None
    assert backfilled is False


def test_resolve_never_builds_pseudo_distribution():
    # 缓存条目缺 home（富化失败）不得回填出 [0, x, y] 伪分布污染优化器
    pred = {"_djyy_id": 5, "djyy_model_prob": None}
    prob, backfilled = resolve_djyy_prob(pred, {"5": {"draw": 0.3, "away": 0.7}})
    assert prob is None
    assert backfilled is False
