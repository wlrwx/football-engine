"""E10：低赔候选独立成池（2026-10-22）。

背景：挑战池的「体彩价 ≥1.60」是 edge 时代留下的筛子。ev_site 口径下
它会系统性偏掉抽样——站点价对体彩价中位溢价约 11.6%，低价热门换到
站点价正好落进正 EV 区。实测 10-21/10-22 两天被挡掉 4 场达标注
（最高 +15.6%），其中萨迪纳摩 vs 奈梅亨 体彩 1.42、站点 1.62、ev_site +4.8%。

实验隔离（关键）：
  - E8 的候选集合、排序、每轮上限不变；E10 只吃 E8 之后的剩余名额。
  - E10 记录带 exp="E10"，E8 裁决必须排除；历史记录无 exp 视为 E8。
  - 默认 min_sporttery_odds=1.60（不启用）；写坏只回落旧筛，不关 ev_site。
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

ch = importlib.import_module("challenge_hourly")
cr = importlib.import_module("challenge_review")


def _setup(tmp_path, monkeypatch, matches, policy=None):
    state = tmp_path / "challenge_state.json"
    monkeypatch.setattr(ch, "STATE", state)
    pol = tmp_path / "challenge_policy.json"
    if policy is not None:
        pol.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(ch, "POLICY", pol)
    monkeypatch.setenv("NJSTATS_JWT", "test-jwt")

    day = dt.datetime.now(ch.BJT).strftime("%Y-%m-%d")
    d = tmp_path / "data" / "daily" / day
    d.mkdir(parents=True, exist_ok=True)
    preds = [{"home_team": m["home"], "away_team": m["away"],
              "home_win_prob": m["p"], "draw_prob": 0.2,
              "away_win_prob": 1 - m["p"] - 0.2,
              "market_fair": [m["p"] - m["edge"], 0.2, 1 - m["p"] - 0.2 + m["edge"]],
              "home_odds": m.get("sp", 2.0), "draw_odds": 3.5, "away_odds": 3.5}
             for m in matches]
    (d / "predictions.json").write_text(json.dumps({"predictions": preds}), encoding="utf-8")
    monkeypatch.setattr(ch, "ROOT", tmp_path)

    soon = int((dt.datetime.now(ch.BJT) + dt.timedelta(hours=6)).timestamp())
    board = [{"sid": m["sid"], "home": m["home"], "away": m["away"], "kick_ts": soon,
              "odds": [m["site"], 3.60, 3.60]}
             for m in matches]
    monkeypatch.setattr(ch.api, "board_with_odds", lambda jwt: board)
    monkeypatch.setattr(ch.api, "my_predictions", lambda jwt: [])
    submitted = []
    monkeypatch.setattr(ch.api, "submit",
                        lambda jwt, sid, mkt, pick: (submitted.append(sid), True)[1])
    return state, submitted


LOW = {"sid": "LOW1", "home": "萨迪纳摩", "away": "奈梅亨",
       "p": 0.647, "edge": -0.0042, "site": 1.62, "sp": 1.42}


def test_default_keeps_legacy_filter(tmp_path, monkeypatch):
    """没有 min_sporttery_odds 就是旧行为：低赔候选不投。"""
    _, submitted = _setup(tmp_path, monkeypatch, [LOW],
                          policy={"gate_mode": "ev_site", "ev_floor": 0.02})
    assert ch.main() == 0
    assert submitted == []


def test_e10_admits_low_odds_and_tags_record(tmp_path, monkeypatch):
    state, submitted = _setup(tmp_path, monkeypatch, [LOW],
                              policy={"gate_mode": "ev_site", "ev_floor": 0.02,
                                      "min_sporttery_odds": 0.0})
    assert ch.main() == 0
    assert submitted == ["LOW1"]
    rec = json.loads(state.read_text())["records"][0]
    assert rec["exp"] == "E10", "低赔注必须能被 E8 裁决排除"
    assert "@1.42" in rec["pick"], "pick 里未记录下单时的体彩价"
    assert rec["site_odds"] == pytest.approx(1.62)
    assert rec["ev_site"] == pytest.approx(0.647 * 1.62 - 1, abs=1e-4)
    assert rec["gate"] == "ev_site"


def test_e10_does_not_displace_e8_candidate(tmp_path, monkeypatch):
    """E10 的 EV 更高也不能挤掉 E8 候选——否则就是改了 E8 抽样。"""
    e8 = {"sid": "E8__", "home": "帕尔马", "away": "都灵",
          "p": 0.52, "edge": 0.0, "site": 1.98, "sp": 1.9}
    _, submitted = _setup(tmp_path, monkeypatch, [LOW, e8],
                          policy={"gate_mode": "ev_site", "ev_floor": 0.02,
                                  "min_sporttery_odds": 0.0})
    assert ch.main() == 0
    assert submitted == ["E8__", "LOW1"], f"E8 必须排在 E10 前：{submitted}"


def test_e10_inactive_under_edge_gate(tmp_path, monkeypatch):
    """E10 依附 ev_site；口径回退 edge 时它必须一起停，不能悄悄放量。"""
    low_with_edge = {"sid": "LOW2", "home": "萨迪纳摩", "away": "奈梅亨",
                     "p": 0.60, "edge": 0.05, "site": 1.62, "sp": 1.42}
    _, submitted = _setup(tmp_path, monkeypatch, [low_with_edge],
                          policy={"min_sporttery_odds": 0.0})
    assert ch.main() == 0
    assert submitted == []


def test_broken_e10_key_keeps_e8_gate(tmp_path, monkeypatch):
    """E10 参数写坏只回落旧筛，不能连带把 ev_site 闸门关掉。"""
    e8 = {"sid": "E8__", "home": "帕尔马", "away": "都灵",
          "p": 0.55, "edge": 0.0, "site": 1.98, "sp": 1.9}
    state, submitted = _setup(tmp_path, monkeypatch, [LOW, e8],
                              policy={"gate_mode": "ev_site", "ev_floor": 0.02,
                                      "min_sporttery_odds": "不是数"})
    assert ch.main() == 0
    assert submitted == ["E8__"], "E8 候选应仍按 ev_site 口径成交"
    assert json.loads(state.read_text())["records"][0]["gate"] == "ev_site"


def test_e10_respects_ev_upper_bound(tmp_path, monkeypatch):
    """脏站点价（EV>25%）在 E10 池里同样要挡。"""
    junk = {"sid": "JUNK", "home": "萨迪纳摩", "away": "奈梅亨",
            "p": 0.55, "edge": -0.01, "site": 5.00, "sp": 1.42}
    _, submitted = _setup(tmp_path, monkeypatch, [junk],
                          policy={"gate_mode": "ev_site", "ev_floor": 0.02,
                                  "min_sporttery_odds": 0.0})
    assert ch.main() == 0
    assert submitted == []


def test_review_preserves_experiment_switches(tmp_path):
    """周复盘不能整份覆盖 policy——否则 E8/E10 会在周一 03:30 静默回退。"""
    p = tmp_path / "challenge_policy.json"
    p.write_text(json.dumps({
        "gate_mode": "ev_site", "ev_floor": 0.02,
        "min_sporttery_odds": 0.0, "note": "E8+E10 开关说明",
    }, ensure_ascii=False), encoding="utf-8")
    cr.write_policy(p, {"generated_at": "2026-10-26 03:30:00",
                        "n_settled": 0, "review_notes": "周复盘"})
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["gate_mode"] == "ev_site"
    assert d["ev_floor"] == 0.02
    assert d["min_sporttery_odds"] == 0.0
    assert d["note"] == "E8+E10 开关说明"
    assert d["review_notes"] == "周复盘"
