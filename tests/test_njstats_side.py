"""侧挂适配器的三条硬约束：时点、市场隔离、可缺席。

“侧挂”= 有就读、没有就 0，引擎照常跑。这三条一旦破，侧挂就变成了
“偷看未来 / 目标市场渗入 / 硬依赖”，比不接更糟。
"""
import json
from datetime import datetime, timezone

import pytest

from engine.sources import njstats_side as ns


@pytest.fixture()
def side_dirs(tmp_path, monkeypatch):
    nj = tmp_path / "njstats"
    feat = tmp_path / "features"
    for sub in ("site_odds", "community"):
        (nj / sub).mkdir(parents=True)
    feat.mkdir()
    monkeypatch.setattr(ns, "NJ", nj)
    monkeypatch.setattr(ns, "FEATURES", feat)
    return nj, feat


def _write(p, doc):
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_snapshot_after_kickoff_is_dropped(side_dirs):
    """快照晚于开赛 = 已经知道结果了，必须丢。"""
    nj, _ = side_dirs
    _write(nj / "site_odds" / "2026-10-21.json", {
        "snapshot_at": "2026-10-21 20:00:00",
        "matches": [{"home": "甲", "away": "乙", "kick_ts": 1792500000, "odds": [2.0, 3.2, 4.0]}],
    })
    # kick_ts 对应的开赛时刻早于快照 → 0 场
    assert ns.coverage("2026-10-21")["site_fair"] == 0


def test_kick_ts_is_understood(side_dirs):
    """站点价快照用 epoch 秒，不是 kickoff 字符串；解析错会把整份数据误杀。"""
    nj, _ = side_dirs
    ko = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc).timestamp() - 8 * 3600
    _write(nj / "site_odds" / "2026-10-21.json", {
        "snapshot_at": "2026-10-21 03:00:00",
        "matches": [{"home": "甲", "away": "乙", "kick_ts": int(ko), "odds": [2.0, 3.2, 4.0]}],
    })
    sig = ns.for_fixture("甲", "乙", "2026-10-21")
    assert sig is not None and sig.site_fair is not None
    assert sum(sig.site_fair) == pytest.approx(1.0, abs=1e-9)


def test_target_market_features_never_enter_model(side_dirs):
    """站点价是目标市场（E8 拿它结算），只能留在策略层。"""
    nj, _ = side_dirs
    ko = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc).timestamp()
    _write(nj / "site_odds" / "2026-10-21.json", {
        "snapshot_at": "2026-10-21 03:00:00",
        "matches": [{"home": "甲", "away": "乙", "kick_ts": int(ko), "odds": [2.0, 3.2, 4.0]}],
    })
    sig = ns.for_fixture("甲", "乙", "2026-10-21")
    assert sig.site_fair is not None          # 读得到
    assert sig.to_features() == {}            # 但一个字都不进特征


def test_reference_features_enter(side_dirs):
    """开盘赔率 + 主客分离统计是 reference，允许进。"""
    _, feat = side_dirs
    _write(feat / "2026-10-14.json", {"rows": [{
        "home": "甲", "away": "乙", "kickoff": "2026-10-14 20:00",
        "snapshot_at": "2026-10-13 16:00:00",
        "opening": {"home": 2.0, "draw": 3.2, "away": 4.0},
        "home_stats": {"home": {"ppg": 2.1}},
        "away_stats": {"away": {"ppg": 1.2}},
    }]})
    f = ns.for_fixture("甲", "乙", "2026-10-14").to_features()
    assert f["side_open_home"] == pytest.approx(0.4706, abs=0.001)  # 去水后：0.5/1.0625
    assert f["side_ppg_diff"] == pytest.approx(0.9)


def test_stale_snapshot_is_dropped(side_dirs):
    """开盘价可以老，但不能老过 MAX_LEAD_H——否则是拿一周前的价当今天的。"""
    _, feat = side_dirs
    _write(feat / "2026-10-14.json", {"rows": [{
        "home": "甲", "away": "乙", "kickoff": "2026-10-14 20:00",
        "snapshot_at": "2026-10-01 16:00:00",
        "opening": {"home": 2.0, "draw": 3.2, "away": 4.0},
    }]})
    assert ns.for_fixture("甲", "乙", "2026-10-14") is None


def test_training_side_uses_as_of_not_kickoff(side_dirs):
    """训练侧要比 kickoff 更严：快照晚于当时预测时刻也算偷看。"""
    _, feat = side_dirs
    _write(feat / "2026-10-14.json", {"rows": [{
        "home": "甲", "away": "乙", "kickoff": "2026-10-14 20:00",
        "snapshot_at": "2026-10-14 09:00:00",
        "opening": {"home": 2.0, "draw": 3.2, "away": 4.0},
    }]})
    sig = ns.for_fixture("甲", "乙", "2026-10-14")
    assert sig.features_as_of("2026-10-14 08:00:00") is None   # 预测早于快照 → 不许用
    assert sig.features_as_of("2026-10-14 10:00:00")          # 预测晚于快照 → 可用


def test_signal_absent_returns_none_not_crash(side_dirs):
    assert ns.for_fixture("查无", "此队") is None
    assert ns.coverage()["matches"] == 0


def test_feature_names_append_side_columns_last():
    """原有 21 列顺序不能动（已训练 booster 按列号取值）。"""
    from engine.prediction.lgbm_model import (BASE_FEATURE_NAMES, FEATURE_NAMES,
                                              SIDE_FEATURE_NAMES)
    assert len(BASE_FEATURE_NAMES) == 21
    assert FEATURE_NAMES[:21] == BASE_FEATURE_NAMES
    assert FEATURE_NAMES[21:21 + len(SIDE_FEATURE_NAMES)] == SIDE_FEATURE_NAMES


def test_build_features_fills_side_defaults():
    from engine.prediction.lgbm_model import SIDE_FEATURE_NAMES, build_features
    f = build_features(elo_home=1500, elo_away=1500)
    assert all(f[n] == 0.0 for n in SIDE_FEATURE_NAMES)
    f2 = build_features(elo_home=1500, elo_away=1500,
                        side={"side_open_home": 0.5, "side_ppg_diff": 0.9})
    assert f2["side_open_home"] == 0.5 and f2["side_ppg_diff"] == 0.9
