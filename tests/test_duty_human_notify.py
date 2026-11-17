"""HUMAN_ONLY 红线即时推送（2026-11-17）。

红线此前只写 duty.log，等着 22:40 分诊会话来读——机器那时睡着，带时限的预警
（登录将到期、真值快照断更…）就被静默顺延。钩子必须做到：同集合不重复弹、
解除时提醒一次消失、osascript 失败/无名单都不拖垮 duty 本轮。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_notify", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_notify"] = duty
SPEC.loader.exec_module(duty)


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / ".football-engine").mkdir()
    monkeypatch.setattr(duty, "HOME", tmp_path)
    logs, calls = [], []
    monkeypatch.setattr(duty, "log", lambda m: logs.append(m))
    monkeypatch.delenv("DUTY_SKIP_HUMAN_NOTIFY", raising=False)

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(duty.subprocess, "run", fake_run)
    return logs, calls, tmp_path / ".football-engine" / "human_redline_notified.json"


def _flags(*codes):
    return [{"code": c, "detail": f"d\n{c} 详情", "action": "a"} for c in codes]


def test_first_human_flag_notifies_once_and_persists(env):
    logs, calls, state = env
    duty._notify_human_flags(_flags("njstats_session_expiring"))
    assert len(calls) == 1 and calls[0][0] == "osascript"
    # 文本被压成单行（换行会打断 AppleScript 字符串）
    assert "\n" not in calls[0][3]
    assert json.loads(state.read_text(encoding="utf-8"))["codes"] == ["njstats_session_expiring"]
    assert "红线推送: 已通知" in logs[-1]


def test_same_set_does_not_renotify(env):
    logs, calls, _ = env
    duty._notify_human_flags(_flags("account_gap"))
    duty._notify_human_flags(_flags("account_gap"))
    assert len(calls) == 1, "同一红线集合每轮重弹 = 通知噪声，很快被无视"


def test_new_code_in_set_notifies_again(env):
    _, calls, state = env
    duty._notify_human_flags(_flags("account_gap"))
    duty._notify_human_flags(_flags("account_gap", "error_mint"))
    assert len(calls) == 2
    assert json.loads(state.read_text(encoding="utf-8"))["codes"] == ["account_gap", "error_mint"]


def test_clearance_logged_once_then_silent(env):
    logs, calls, _ = env
    duty._notify_human_flags(_flags("error_branch"))
    duty._notify_human_flags([])
    assert any("已解除" in x for x in logs)
    duty._notify_human_flags([])
    assert sum(1 for x in logs if "推送" in x) == 2, "解除后再来空集不该刷屏"


def test_nonhuman_flags_are_ignored(env):
    _, calls, state = env
    duty._notify_human_flags([{"code": "made_up_auto_code", "detail": "x", "action": "y"}])
    assert calls == [] and not state.exists()


def test_osascript_failure_does_not_raise(env, monkeypatch):
    logs, _, state = env

    def boom(args, **kwargs):
        raise OSError("no window server")
    monkeypatch.setattr(duty.subprocess, "run", boom)
    duty._notify_human_flags(_flags("error_mint"))  # 不许抛
    assert "osascript 失败" in logs[-1]
    assert state.exists(), "失败也记账：否则每轮重试轰炸"


def test_skip_env(env, monkeypatch):
    _, calls, _ = env
    monkeypatch.setenv("DUTY_SKIP_HUMAN_NOTIFY", "1")
    duty._notify_human_flags(_flags("account_gap"))
    assert calls == []


def test_flag_set_is_same_source_as_dispatch(env):
    import agent_dispatch
    assert "njstats_session_expiring" in agent_dispatch.HUMAN_ONLY
    assert "error_git" in agent_dispatch.HUMAN_ONLY
