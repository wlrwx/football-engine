"""NJStats prob 归档 → 特征的提取逻辑测试（2026-10-13）。

为什么值得测：
这个模块把 `data/state/njstats_archive/*_prob_*.json`（第三方 API 的原始形状）
转成模型特征。**上游 schema 变了不会报错**，只会让字段静默变空——
而空字段的下游后果是"开盘赔率覆盖率从 62% 掉到 0%"，
要等到做开收盘对比时才发现，那时归档窗口可能已经过期（试用 10-15 到期）。

所以这里锁三件事：
1. 统计字段路径没错（ppg/进失球/主客分离）
2. 去水口径与 historical_replay.devig() 一致（两处不一致会让对比失真）
3. 开盘缺失不丢记录（降级而不是抛异常）
"""

from __future__ import annotations


import pytest

from engine.sources import njstats_features as nf

pytestmark = pytest.mark.skipif(
    not nf.ARCHIVE.exists(), reason="无 NJStats 归档目录"
)


# ---------------------------------------------------------------- 去水口径

def test_devig_sums_to_one():
    odds = {"home": 3.2, "draw": 3.6, "away": 2.05}
    fair = nf.devig(odds)
    assert fair is not None
    assert abs(sum(fair.values()) - 1.0) < 1e-9


def test_devig_matches_historical_replay():
    """口径必须与 engine/backtest/historical_replay.py 的 devig() 一致。

    两处各写一份去水，是"开盘 vs 收盘"对比失真的经典来源——
    一边等赔法一边乘率法，差值会被误读成 edge。
    """
    from engine.backtest.historical_replay import devig as ref_devig
    odds = {"home": 2.10, "draw": 3.30, "away": 3.00}
    mine = nf.devig(odds)
    ref = ref_devig((odds["home"], odds["draw"], odds["away"]))
    for i, k in enumerate(("home", "draw", "away")):
        assert abs(mine[k] - ref[i]) < 1e-9, f"{k} 去水口径不一致: {mine[k]} vs {ref[i]}"


def test_devig_rejects_garbage():
    """赔率 <=1 或缺失必须返回 None，不能算出负数概率。"""
    assert nf.devig({"home": 0.9, "draw": 3.0, "away": 3.0}) is None
    assert nf.devig({"home": None, "draw": 3.0, "away": 3.0}) is None
    assert nf.devig({}) is None


# ---------------------------------------------------------------- 统计提取

def _sample_record():
    """按 prob 端点真实形状构造的记录（字段名带 _all/_home/_away 后缀）。"""
    return {
        "matchId": "12795903087",
        "leagueName": "阿乙",
        "season": "2026",
        "status": "未完赛",
        "homeName": "科尔多瓦竞技",
        "awayName": "洛斯安第斯",
        "matchTimeStr": "2026-10-13 04:00:00",
        "openingHome": 2.6, "openingDraw": 2.7, "openingAway": 3.0,
        "drawRate": 36,
        "homeCount": 0, "awayCount": 0,
        "home": {
            "overall": {"ppg_all": 1.16, "scoredAVG_all": 0.91,
                        "concededAVG_all": 1.22, "matchesPlayed_all": 32},
            "home": {"ppg_home": 1.81, "scoredAVG_home": 1.31,
                     "concededAVG_home": 0.88, "matchesPlayed_home": 16},
        },
        "away": {
            "overall": {"ppg_all": 1.25, "scoredAVG_all": 0.78,
                        "concededAVG_all": 0.72, "matchesPlayed_all": 32},
            "away": {"ppg_away": 1.06, "scoredAVG_away": 0.75,
                     "concededAVG_away": 0.94, "matchesPlayed_away": 16},
        },
    }


def test_extract_team_uses_scope_suffix():
    """主队取 home 口径、客队取 away 口径——后缀必须对上。

    取错口径（比如主队取了 overall）不会报错，只会让"主场优势"
    这个特征静默消失，而它是我们最想要的东西之一。
    """
    rec = _sample_record()
    hm = nf.extract_team(rec["home"], "home")
    aw = nf.extract_team(rec["away"], "away")
    assert hm["ppg"] == 1.81, "主队必须取 home 口径的 ppg"
    assert aw["ppg"] == 1.06, "客队必须取 away 口径的 ppg"
    # 全局口径是另一个值，证明没有混
    hm_all = nf.extract_team(rec["home"], "all")
    assert hm_all["ppg"] == 1.16


def test_extract_record_full_shape():
    rec = nf.extract_record(_sample_record(), "2026-10-13")
    assert rec["match_id"] == "12795903087"
    assert rec["home"] == "科尔多瓦竞技"
    assert rec["opening"] == {"home": 2.6, "draw": 2.7, "away": 3.0}
    assert rec["opening_fair"] is not None
    assert abs(sum(rec["opening_fair"].values()) - 1.0) < 1e-9
    assert rec["home_stats"]["home"]["ppg"] == 1.81
    assert rec["away_stats"]["away"]["ppg"] == 1.06
    assert rec["draw_rate"] == 36


def test_extract_record_survives_missing_opening():
    """开盘缺失时必须照样输出记录（opening=None），不能丢。

    实测 10-14 的早赛就没有开盘赔率。丢记录会让样本量偏斜，
    而且偏斜方向是"只留下热门比赛"——热门比赛才有开盘。
    """
    rec = _sample_record()
    rec["openingHome"] = None
    out = nf.extract_record(rec, "2026-10-14")
    assert out["opening"] is None
    assert out["opening_fair"] is None
    assert out["match_id"] == "12795903087", "开盘缺失不能丢记录"
    assert out["home_stats"]["home"]["ppg"] == 1.81, "统计字段不受开盘缺失影响"


def test_extract_record_survives_missing_stats():
    """统计缺失时也不能抛异常（早赛只踢 3 场，字段不全）。"""
    rec = _sample_record()
    rec["home"] = {"overall": {}, "home": {}}
    out = nf.extract_record(rec, "2026-10-14")
    assert out["home_stats"]["home"] == {}
    assert out["opening"] is not None


# ---------------------------------------------------------------- 真实归档数据

def test_real_archive_produces_features():
    """跑真实归档：必须有输出、开盘覆盖率非零、主客分离统计非零。

    这三个数任意一个为 0 都说明上游 schema 变了（或归档没跑），
    而它不会报错——所以必须有测试盯着。
    """
    files = sorted(nf.ARCHIVE.glob("*_prob_draw*.json"))
    if not files:
        pytest.skip("无 prob_draw 归档")
    rows = nf.load_day(files[0].name[:10])
    assert rows, "load_day 返回空"
    with_opening = sum(1 for r in rows if r["opening"])
    with_split = sum(
        1 for r in rows
        if r["home_stats"]["home"].get("ppg") and r["away_stats"]["away"].get("ppg")
    )
    assert with_opening > 0, "开盘赔率覆盖率为 0——上游字段名可能变了"
    assert with_split > 0, "主客分离统计为 0——scope 后缀可能变了"
    # 每条都必须有 match_id（下游 join 的键）
    assert all(r["match_id"] for r in rows)


def test_features_dedup_by_match_id():
    """同一 matchId 在多页归档里重复时必须去重。

    prob 端点分页有重叠边界时会出现重复 matchId；不去重会让
    "571 场"变成 "600 场"，样本量悄悄虚高。
    """
    files = sorted(nf.ARCHIVE.glob("*_prob_draw*.json"))
    if len(files) < 2:
        pytest.skip("prob 归档不足 2 页，无法验证去重")
    day = files[0].name[:10]
    rows = nf.load_day(day)
    ids = [r["match_id"] for r in rows]
    assert len(ids) == len(set(ids)), f"有重复 matchId: {len(ids)} → {len(set(ids))}"
