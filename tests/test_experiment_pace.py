"""E8/E10 攒注速度巡检的分池与截止分支测试（2026-10-22）。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import experiment_pace as ep  # noqa: E402


def _rec(day: str, exp: str | None = None, gate: str = "ev_site") -> dict:
    r = {"match": "A vs B", "pick": "主胜@1.80", "gate": gate,
         "submitted_at": f"{day} 12:00:00"}
    if exp:
        r["exp"] = exp
    return r


def test_pools_split_by_exp_and_gate():
    recs = [
        _rec("2026-10-22"),                 # E8（无 exp）
        _rec("2026-10-22", "E8"),           # E8
        _rec("2026-10-22", "E10"),          # E10
        _rec("2026-10-21"),                 # 实验前，排除
        _rec("2026-10-22", gate="edge"),    # 旧闸门，排除
    ]
    assert len(ep.pool(recs, "E8")) == 2
    assert len(ep.pool(recs, "E10")) == 1


def test_e10_closed_never_says_can_close():
    """E10 已于 2026-11-16 终判失败关闭——n≥60 对它只是历史，不得再喊「可收尾」。"""
    recs = [_rec(f"2026-11-{d:02d}", "E10") for d in range(1, 20)] * 4   # 76 注
    s = ep.summarize(recs, "E10", date(2026, 11, 20))
    assert "已终判关闭" in s["status"] and "收尾" not in s["status"], s["status"]
    line = ep.format_line(s)
    assert "已终判关闭" in line, line
    # E8 同形状仍走原分支——关闭语义不许传染给活跃实验
    e8 = ep.summarize([_rec("2026-11-01", "E8") for _ in range(61)], "E8", date(2026, 11, 20))
    assert "可按判据收尾" in e8["status"], e8["status"]


def test_early_days_do_not_extrapolate():
    recs = [_rec("2026-10-22")]
    s = ep.summarize(recs, "E8", date(2026, 10, 22))
    assert s["n"] == 1
    assert "观察期" in s["status"]
    assert s["days_left"] == 31
    assert s["required"] == (60 - 1) / 31


def test_projected_shortfall_triggers_preregistered_extension():
    recs = [_rec("2026-10-22") for _ in range(5)]
    s = ep.summarize(recs, "E8", date(2026, 11, 1))
    assert s["elapsed"] == 11
    assert s["projected"] < 60
    assert "预登记延期" in s["status"]
    assert s["ext_projected"] > s["projected"]


def test_on_track_projection_does_not_extend():
    recs = [_rec("2026-10-22") for _ in range(40)]
    s = ep.summarize(recs, "E8", date(2026, 11, 1))
    assert s["projected"] >= 60
    assert s["status"] == "在轨"


def test_deadline_day_extends_instead_of_reverting_edge():
    recs = [_rec("2026-10-22") for _ in range(4)]
    s = ep.summarize(recs, "E8", date(2026, 11, 22))
    assert s["days_left"] == 0
    assert "预登记延期" in s["status"]
    assert "回退" not in s["status"].replace("不自动回退 edge", "")


def test_extension_exhausted_is_inconclusive():
    recs = [_rec("2026-10-22") for _ in range(10)]
    s = ep.summarize(recs, "E8", date(2027, 1, 1))
    assert "证据不足" in s["status"]
    assert "回退" in s["status"]  # 明确写「不自动回退 edge」


def test_target_reached():
    recs = [_rec("2026-10-22") for _ in range(60)]
    s = ep.summarize(recs, "E8", date(2026, 11, 10))
    assert s["status"].startswith("已达 n=60")


def test_cli_prints_both_pools(capsys, tmp_path, monkeypatch):
    import json

    p = tmp_path / "state.json"
    p.write_text(json.dumps({"records": [_rec("2026-10-22"),
                                         _rec("2026-10-22", "E10")]}),
                 encoding="utf-8")
    monkeypatch.setattr(ep, "STATE", p)
    ep.main(["--today", "2026-10-22"])
    out = capsys.readouterr().out
    assert "E8: n=1/60" in out
    assert "E10: n=1/60" in out
