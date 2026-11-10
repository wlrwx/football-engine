"""duty 的登录态主动续期（2026-11-09）。

被动续期只在 mint 已经失败时才跑，那时缓存 JWT 最多再撑 20h、人去处理已经赶。
这里锁「主动」的触发条件：距上次**成功**续期超 12h 才续、未成功时最多每 6h 重试一次
（否则厂商 5xx 期间会每轮起一次浏览器）、开关可关、失败只留一行日志不影响本轮。
真正的续期决策在 njstats_refresh_session.py（另有用例）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
BJT = timezone(timedelta(hours=8))


def _load_duty():
    spec = importlib.util.spec_from_file_location(
        "njstats_duty_session", ROOT / "scripts" / "njstats_duty.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["njstats_duty_session"] = mod
    spec.loader.exec_module(mod)
    return mod


def _setup(tmp_path, monkeypatch, runs, *, health=None, health_h=0.0, rc=0,
           stdout="已续期：SESSION、JWT", marker_h=None, sess=True):
    duty = _load_duty()
    sess_path = tmp_path / "njstats_session.json"
    if sess:
        sess_path.write_text(json.dumps({"SESSION": "x", "seeded_at": "2026-10-20 02:05:32"}),
                             encoding="utf-8")
    health_path = tmp_path / "njstats_session_health.json"
    if health:
        ts = datetime.now(BJT) - timedelta(hours=health_h)
        health_path.write_text(json.dumps({"result": health, "at": ts.strftime("%F %T"),
                                           "detail": "x"}), encoding="utf-8")
    marker = tmp_path / ".refresh_attempt"
    if marker_h is not None:
        marker.write_text(str(int(time.time() - marker_h * 3600)), encoding="utf-8")
    monkeypatch.setattr(duty, "SESS", sess_path)
    monkeypatch.setattr(duty, "SESSION_HEALTH", health_path)
    monkeypatch.setattr(duty, "SESSION_REFRESH_MARKER", marker)
    monkeypatch.setattr(duty.subprocess, "run", lambda cmd, **kw: (
        runs.append((cmd, kw)), SimpleNamespace(returncode=rc, stdout=stdout, stderr=""))[1])
    lines: list[str] = []
    monkeypatch.setattr(duty, "log", lines.append)
    return duty, lines


def test_recent_success_is_not_refreshed_again(tmp_path, monkeypatch):
    runs = []
    duty, _ = _setup(tmp_path, monkeypatch, runs, health="ok", health_h=1)
    duty._maybe_proactive_session_refresh()
    assert runs == [], "12h 内成功续过就不用再起浏览器"


def test_success_older_than_12h_triggers_refresh(tmp_path, monkeypatch):
    runs = []
    duty, lines = _setup(tmp_path, monkeypatch, runs, health="ok", health_h=20)
    duty._maybe_proactive_session_refresh()
    assert len(runs) == 1
    cmd, kw = runs[0]
    assert cmd[-1] == "scripts/njstats_refresh_session.py", cmd
    assert kw["timeout"] <= 180 and kw["cwd"] is duty.ROOT
    assert lines == ["session-refresh: 已续期：SESSION、JWT"], lines


def test_never_renewed_triggers_even_without_health_file(tmp_path, monkeypatch):
    runs = []
    duty, _ = _setup(tmp_path, monkeypatch, runs)
    duty._maybe_proactive_session_refresh()
    assert len(runs) == 1


def test_failure_retry_is_rate_limited_to_6h(tmp_path, monkeypatch):
    runs = []
    duty, _ = _setup(tmp_path, monkeypatch, runs, health="auth_dead", health_h=1,
                     marker_h=1)
    duty._maybe_proactive_session_refresh()
    assert runs == [], "登录态已死时也别每 30 分钟起一次浏览器"


def test_missing_session_file_is_refreshed_despite_recent_ok(tmp_path, monkeypatch):
    """health 说刚续过、但 session 文件不见了 = 状态不一致，必须以文件为准补一次。"""
    runs = []
    duty, _ = _setup(tmp_path, monkeypatch, runs, health="ok", health_h=1, sess=False)
    duty._maybe_proactive_session_refresh()
    assert len(runs) == 1


def test_env_switch_disables_it(tmp_path, monkeypatch):
    runs = []
    duty, _ = _setup(tmp_path, monkeypatch, runs)
    monkeypatch.setenv("DUTY_SKIP_SESSION_REFRESH", "1")
    duty._maybe_proactive_session_refresh()
    assert runs == []


def test_dead_login_is_visible_in_the_log(tmp_path, monkeypatch):
    runs = []
    duty, lines = _setup(
        tmp_path, monkeypatch, runs, rc=1,
        stdout="登录态已失效（HTTP 401），需人工播种（跑 scripts/njstats_seed_login.py）")
    duty._maybe_proactive_session_refresh()
    assert lines == ["session-refresh: 登录态已失效（HTTP 401），需人工播种"
                     "（跑 scripts/njstats_seed_login.py）"], lines
