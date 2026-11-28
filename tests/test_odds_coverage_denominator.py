"""收盘覆盖率指标的分母与标签（2026-10-22 修）。

回归动机有两层：

1. 原先 `ratio = within_close / total`，而 total 里混着还没开赛的场次——
   它们结构上不可能有收盘快照，等于给指标加了一个会随抓取窗口漂移的分母。
2. 更要命的是原先那行 `not_started = sum(1 for g in lead_last if g > 0)`：
   字段名叫 not_started_yet，看着像"这些场次还没开赛所以无法评估"，
   真实含义却是"末快照早于开赛"= **采集在收盘前就停了**，正是本指标要抓的病灶。
   2026-10-22 复核 PR#9 时我就被这个字段名骗过，差点把"覆盖率只有 3.5%"
   判成"指标分母坏了"，从而漏掉"扩 --days 跑了 10 天根本没用"这个结论。

一个会骗人的字段名比一个不准的数值更贵，所以两个都单独钉住。
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import odds_series_coverage as osc  # noqa: E402
from engine.beijing_time import beijing_now  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"


def _write_series(tmp_path: Path, name: str, kickoff, snaps) -> None:
    """snaps = [(距开赛几小时, home/draw/away 赔率), ...]，按时间顺序。"""
    lines = []
    for lead_h, trio in snaps:
        ts = kickoff - timedelta(hours=lead_h)
        lines.append(json.dumps({
            "ts": ts.strftime(FMT), "match_time": kickoff.strftime(FMT),
            "home_team": "甲", "away_team": "乙", "league": "测试",
            "euro": {"home": trio[0], "draw": trio[1], "away": trio[2]},
        }, ensure_ascii=False))
    (tmp_path / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _measure(monkeypatch, tmp_path, cases) -> dict:
    d = tmp_path / "odds_series"
    d.mkdir()
    for i, (kickoff, snaps) in enumerate(cases):
        _write_series(d, f"m{i}.jsonl", kickoff, snaps)
    monkeypatch.setattr(osc, "SERIES_DIR", d)
    return osc.measure()


def test_pending_matches_leave_the_denominator(monkeypatch, tmp_path):
    """未开赛的场次不该进分母：它们不可能有收盘快照。"""
    now = beijing_now().replace(tzinfo=None)
    rep = _measure(monkeypatch, tmp_path, [
        # 已开赛、开赛前 1h 有快照 → 唯一一次"拿到收盘价"
        (now - timedelta(hours=3), [(9.0, (2.0, 3.5, 4.0)), (1.0, (2.1, 3.4, 4.1))]),
        # 已开赛、末快照停在开赛前 9h → 分母里有它，分子里没有
        (now - timedelta(hours=2), [(11.0, (2.0, 3.5, 4.0)), (9.0, (2.0, 3.5, 4.0))]),
        # 未开赛（3 天后）→ 不进分母
        (now + timedelta(days=3), [(20.0, (2.0, 3.5, 4.0)), (18.0, (2.05, 3.5, 4.0))]),
    ])
    c = rep["closing_coverage"]
    assert c["assessable"] == 2, f"分母应只含已开赛的两场，实际 {c}"
    assert c["pending_kickoff"] == 1
    assert c["n"] == 1
    assert c["ratio"] == 0.5, "换成已开赛分母后，这个板的收盘覆盖率是 50%"
    assert rep["healthy"] is True, "达到 50% 门槛就该判健康（原分母下永远达不到）"


def test_label_matches_its_real_meaning(monkeypatch, tmp_path):
    """stopped_before_kickoff 数"采集提前停"，post_kickoff_last 数"末快照在赛后"。"""
    now = beijing_now().replace(tzinfo=None)
    rep = _measure(monkeypatch, tmp_path, [
        (now - timedelta(hours=2), [(9.0, (2.0, 3.5, 4.0)), (7.0, (2.1, 3.4, 4.1))]),
        (now - timedelta(hours=1), [(0.5, (2.0, 3.5, 4.0)), (-0.5, (1.9, 3.6, 4.2))]),
    ])
    c = rep["closing_coverage"]
    assert c["stopped_before_kickoff"] == 1
    assert c["post_kickoff_last"] == 1
    assert "not_started_yet" not in c, "误导性字段名必须消失"


def test_no_started_matches_is_not_healthy(monkeypatch, tmp_path):
    """全是未开赛场次时不能报 healthy（分母兜底不能变成"自动通过"）。"""
    now = beijing_now().replace(tzinfo=None)
    rep = _measure(monkeypatch, tmp_path, [
        (now + timedelta(days=2), [(20.0, (2.0, 3.5, 4.0))]),
    ])
    assert rep["closing_coverage"]["assessable"] == 0
    assert rep["healthy"] is False


def test_real_dataset_still_reports_the_measured_35_percent():
    """真实 odds_series 上的现状：收盘覆盖是个位数，不是指标坏了。

    PR#9 扩 --days 满 10 天的裁决就在这里 —— 3.6%→3.5%，方向是采集频率
    （见 docs/EXPERIMENTS.md「odds_series 采集窗口」节）。这条断言写得很难看
    （上限 20%），但它的用处是：一旦有人把分母改回含未开赛的口径，
    或者覆盖率真的被修好了，这条会跳出来要求重新审视结论。
    """
    rep = osc.measure()
    assert rep["total_matches"] > 300, "真实数据缺失，先修测试环境"
    assert 0.0 <= rep["closing_coverage"]["ratio"] < 0.20, \
        f"收盘覆盖率已离开个位数区间（{rep['closing_coverage']}），需要重判 CLV 可用性结论"


def test_last_snapshot_is_picked_by_time_not_by_line_position(monkeypatch, tmp_path):
    """并发追加（git `merge=union`，2026-11-28 ci-sync #201）后文件的**最后一行
    不一定是最后一次观测**——本指标全靠"末快照距开赛"，倒挂会把 1h 内的收盘价
    读成 9h 前的旧价，closing_coverage 直接被低估。"""
    now = beijing_now().replace(tzinfo=None)
    ko = now - timedelta(hours=3)  # 已开赛，进分母
    # 文件里先写 1h（较晚）、后写 9h（较早）：行序与时间序相反
    rep = _measure(monkeypatch, tmp_path, [
        (ko, [(1.0, (2.1, 3.4, 4.1)), (9.0, (2.0, 3.5, 4.0))]),
    ])
    assert rep["closing_coverage"]["n"] == 1, "末快照按行序取，把 1h 内的收盘价读丢了"
    assert rep["lead_to_kickoff_hours"]["median_last"] == 1.0
    assert rep["lead_to_kickoff_hours"]["median_first"] == 9.0
