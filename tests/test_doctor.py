"""系统医生（engine/doctor.py）数据层检查的单测（2026-10-11）

git/env 层的检查有真实副作用（checkout/reset/push），只测数据流与判定逻辑，
全部通过 monkeypatch 把 DAILY/STATE/LEDGER 指到临时目录。
"""
import json
from pathlib import Path

import engine.doctor as doctor
from engine.doctor import DoctorReport, Finding, run


# ------------------------------------------------------ 报告聚合

def test_report_counters_and_json_shape():
    r = DoctorReport()
    r.add(Finding("a", "git", "ok", "x"))
    r.add(Finding("b", "data", "warn", "y", healable=True, healed=True))
    r.add(Finding("c", "source", "critical", "z"))
    assert r.n_critical == 1 and r.n_healed == 1
    j = r.to_json()
    assert j["summary"] == {"total": 3, "critical": 1, "healed": 1}
    assert len(j["findings"]) == 3


# ------------------------------------------------------ 结算流检查

def _make_day(tmp_path: Path, day: str, n_preds: int = 3, with_results: bool = True):
    d = tmp_path / "daily" / day
    d.mkdir(parents=True, exist_ok=True)
    preds = [{"match_id": f"{day}_周一{i:03d}",
              "home_win_prob": 0.4, "draw_prob": 0.3, "away_win_prob": 0.3}
             for i in range(1, n_preds + 1)]
    (d / "predictions.json").write_text(json.dumps(preds), encoding="utf-8")
    if with_results:
        (d / "results_sina.json").write_text(json.dumps([{"match_id": "x"}]), encoding="utf-8")


def test_settlement_missing_triggers_critical_when_no_heal(tmp_path, monkeypatch):
    day = doctor._beijing_date(1)
    _make_day(tmp_path, day)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "review_ledger.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor, "DAILY", tmp_path / "daily")
    monkeypatch.setattr(doctor, "LEDGER", tmp_path / "state" / "review_ledger.jsonl")

    r = DoctorReport()
    doctor.check_settlement_flow(r, heal=False)
    f = [x for x in r.findings if x.id == "data/yesterday"][0]
    assert f.severity == "critical"          # 有赛果没结算 = 数据流断裂级
    assert f.healed is False


def test_settlement_ok_when_ledger_has_date(tmp_path, monkeypatch):
    day = doctor._beijing_date(1)
    _make_day(tmp_path, day)
    (tmp_path / "state").mkdir()
    row = json.dumps({"date": day, "match_id": f"{day}_周一001"})
    (tmp_path / "state" / "review_ledger.jsonl").write_text(row + "\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "DAILY", tmp_path / "daily")
    monkeypatch.setattr(doctor, "LEDGER", tmp_path / "state" / "review_ledger.jsonl")

    r = DoctorReport()
    doctor.check_settlement_flow(r, heal=False)
    assert r.findings[0].severity == "ok"


def test_settlement_skips_when_no_results(tmp_path, monkeypatch):
    day = doctor._beijing_date(1)
    _make_day(tmp_path, day, with_results=False)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "review_ledger.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor, "DAILY", tmp_path / "daily")
    monkeypatch.setattr(doctor, "LEDGER", tmp_path / "state" / "review_ledger.jsonl")

    r = DoctorReport()
    doctor.check_settlement_flow(r, heal=False)
    assert r.findings[0].severity == "info"  # 无赛果不算事故（可能还没出）


# ------------------------------------------------------ DJYY 存活率

def test_djyy_liveness_critical_on_two_low_days(tmp_path, monkeypatch):
    for i, rate in enumerate([0.0, 0.13, 0.75]):   # 昨天/前天/大前天
        day = doctor._beijing_date(i)
        _make_day(tmp_path, day, n_preds=20)      # 阈值 15 后需 >=15 场才计存活率
        d = tmp_path / "daily" / day
        preds = json.loads((d / "predictions.json").read_text(encoding="utf-8"))
        for k, p in enumerate(preds):
            p["djyy_enriched"] = k < int(rate * 20)
        (d / "predictions.json").write_text(json.dumps(preds), encoding="utf-8")
    monkeypatch.setattr(doctor, "DAILY", tmp_path / "daily")

    r = DoctorReport()
    doctor.check_djyy_liveness(r)
    assert r.findings[0].severity == "critical"
    assert "第三通道" in r.findings[0].detail


def test_djyy_liveness_ok_when_healthy(tmp_path, monkeypatch):
    day = doctor._beijing_date(0)
    _make_day(tmp_path, day, n_preds=20)
    d = tmp_path / "daily" / day
    preds = json.loads((d / "predictions.json").read_text(encoding="utf-8"))
    for p in preds:
        p["djyy_enriched"] = True
    (d / "predictions.json").write_text(json.dumps(preds), encoding="utf-8")
    monkeypatch.setattr(doctor, "DAILY", tmp_path / "daily")

    r = DoctorReport()
    doctor.check_djyy_liveness(r)
    assert r.findings[0].severity == "ok"


# ------------------------------------------------------ 概率健康

def test_probability_health_catches_probsum_break(tmp_path, monkeypatch):
    day = doctor._beijing_date(0)
    _make_day(tmp_path, day, n_preds=10)
    d = tmp_path / "daily" / day
    preds = json.loads((d / "predictions.json").read_text(encoding="utf-8"))
    preds[0]["home_win_prob"] = 0.9   # 三路和 = 1.5，畸变
    (d / "predictions.json").write_text(json.dumps(preds), encoding="utf-8")
    monkeypatch.setattr(doctor, "DAILY", tmp_path / "daily")

    r = DoctorReport()
    doctor.check_probability_health(r)
    assert any(f.id == "model/probsum" and f.severity == "critical" for f in r.findings)


def test_probability_health_catches_draw_out_of_band(tmp_path, monkeypatch):
    day = doctor._beijing_date(0)
    _make_day(tmp_path, day, n_preds=10)
    d = tmp_path / "daily" / day
    preds = json.loads((d / "predictions.json").read_text(encoding="utf-8"))
    for p in preds:
        p["home_win_prob"], p["draw_prob"], p["away_win_prob"] = 0.48, 0.04, 0.48
    (d / "predictions.json").write_text(json.dumps(preds), encoding="utf-8")
    monkeypatch.setattr(doctor, "DAILY", tmp_path / "daily")

    r = DoctorReport()
    doctor.check_probability_health(r)
    assert any(f.id == "model/draw" and f.severity == "critical" for f in r.findings)


def test_probability_health_ok_in_band(tmp_path, monkeypatch):
    day = doctor._beijing_date(0)
    _make_day(tmp_path, day, n_preds=10)
    monkeypatch.setattr(doctor, "DAILY", tmp_path / "daily")

    r = DoctorReport()
    doctor.check_probability_health(r)
    assert any(f.id == "model/draw" and f.severity == "ok" for f in r.findings)


# ------------------------------------------------------ 状态文件

def test_state_files_flags_unnormalized_weights(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "fusion_weights.json").write_text(
        json.dumps({"weights": {"model": 0.5, "market": 0.75, "djyy": 0.2}}),
        encoding="utf-8")
    monkeypatch.setattr(doctor, "STATE", tmp_path / "state")

    r = DoctorReport()
    doctor.check_state_files(r)
    assert any(f.id == "model/weights" and f.severity == "warn" for f in r.findings)


# ------------------------------------------------------ 全量跑（net 关闭，无副作用路径）

def test_run_smoke_no_net(tmp_path, monkeypatch):
    # git/env 检查作用于真实仓库，这里只验证 --no-net 全量跑不抛异常、报告落盘
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(doctor, "STATE", tmp_path / "state")
    r = run(heal=False, net=False)
    assert r.findings, "至少应产出 git 层 findings"
    assert all(f.severity in ("ok", "info", "warn", "critical") for f in r.findings)


def test_no_net_never_touches_the_network(tmp_path, monkeypatch):
    """--no-net 必须真的不联网（2026-11-04）。

    check_git_state 原来无条件 `git fetch origin main`，于是名叫 no_net 的冒烟测试
    在 runner 上偶发 fetch 超时 120s 而红，ubuntu 与 windows 都中过。这里 spy 掉 _git：
    任何 fetch/push/ls-remote 直接判失败；同时要求离线结论注明用的是本地引用。
    """
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(doctor, "STATE", tmp_path / "state")
    real = doctor._git
    calls = []

    def spy(args, cwd=doctor.REPO):
        calls.append(list(args))
        assert args[0] not in ("fetch", "push", "ls-remote"), f"--no-net 竟然联网: {args}"
        return real(args, cwd)

    monkeypatch.setattr(doctor, "_git", spy)
    r = run(heal=False, net=False)
    assert r.findings and calls, "git 层仍应做本地检查"
    backlog = [f for f in r.findings if f.id == "git/backlog"]
    assert backlog and all("离线" in f.detail for f in backlog), \
        "离线结论必须注明用的是本地 origin/main 引用、可能滞后"


# ------------------------------------------------ 报告时间戳（2026-10-19 事故）
def test_generated_at_is_beijing_in_any_runner_tz(monkeypatch):
    """CI runner 是 UTC：`beijing_date + 本地时分` 会拼出超前 8 小时的假时间。

    事故现场：BJT 02:17 由 ci-sync 提交的 data/state/system_health.json 里写着
    "2026-10-19T18:17:05"——日期用 beijing_today()、时分用 time.strftime()（runner
    本地时钟 = UTC）。任何拿 generated_at 判新鲜度/排序的消费方都会被骗 8 小时。
    """
    import datetime as _dt
    import os
    import time as _time

    # 把进程时区强行设为 UTC，复现 runner 环境
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    _time.tzset() if hasattr(_time, "tzset") else None
    try:
        from engine.beijing_time import beijing_now
        got = DoctorReport().to_json()["generated_at"]
        want = beijing_now().strftime("%Y-%m-%dT%H:%M:%S")
        got_dt = _dt.datetime.strptime(got, "%Y-%m-%dT%H:%M:%S")
        want_dt = _dt.datetime.strptime(want, "%Y-%m-%dT%H:%M:%S")
        # 允许跨秒抖动，但绝不允许 8 小时级别的偏移
        assert abs((got_dt - want_dt).total_seconds()) < 120, (
            f"generated_at={got} 应为北京时间 {want}（差 {(got_dt - want_dt).total_seconds()}s）")
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        _time.tzset() if hasattr(_time, "tzset") else None
