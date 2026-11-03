"""duty 每轮调用确定性摘要的接线（2026-11-03）。

摘要替代了原来 6 次/天的模型自动化，所以它自己绝不能成为新的静默失败点：
跑不起来、自身出错、没产出，都要在 duty.log 里留下可区分的一行。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_ops_digest", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_ops_digest"] = duty
SPEC.loader.exec_module(duty)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    (tmp_path / ".football-engine").mkdir()
    monkeypatch.setattr(duty, "HOME", tmp_path)
    logs, calls = [], []
    monkeypatch.setattr(duty, "log", lambda m: logs.append(m))
    monkeypatch.delenv("DUTY_SKIP_OPS_DIGEST", raising=False)

    def setup(rc=0, ctx=None, exc=None):
        if ctx is not None:
            (tmp_path / ".football-engine" / "ops_digest.json").write_text(
                json.dumps(ctx, ensure_ascii=False), encoding="utf-8")

        def fake_run(args, **kwargs):
            calls.append(args)
            if exc:
                raise exc
            return subprocess.CompletedProcess(args, rc, stdout="摘要自身出错: X", stderr="")
        monkeypatch.setattr(duty.subprocess, "run", fake_run)
        return calls
    return logs, setup


def test_skip_env_does_not_run_anything(wired, monkeypatch):
    logs, setup = wired
    calls = setup()
    monkeypatch.setenv("DUTY_SKIP_OPS_DIGEST", "1")
    duty._maybe_ops_digest()
    assert calls == [] and logs == []


def test_no_red_flags_logs_one_clean_line(wired):
    logs, setup = wired
    setup(rc=0, ctx={"red_flags": []})
    duty._maybe_ops_digest()
    assert logs == ["ops: 无红线"], logs


def test_red_flags_are_logged_with_action(wired):
    logs, setup = wired
    setup(rc=1, ctx={"red_flags": [
        {"code": "prediction_coverage_gap", "detail": "匹配0", "action": "查今天的 predictions.json"},
        {"code": "snapshot_not_committed", "detail": "30h 未入库", "action": "查 commit_push"}]})
    duty._maybe_ops_digest()
    assert len(logs) == 2 and all(x.startswith("⚠ 运维红线") for x in logs), logs
    assert "prediction_coverage_gap" in logs[0] and "查今天的 predictions.json" in logs[0]


def test_digest_self_error_is_visible_not_fatal(wired):
    logs, setup = wired
    setup(rc=2)
    duty._maybe_ops_digest()
    assert any("自身出错" in x for x in logs), logs


def test_missing_output_is_visible(wired):
    logs, setup = wired
    setup(rc=0)                      # 不写 json
    duty._maybe_ops_digest()
    assert any("没产出可读摘要" in x for x in logs), logs


def test_launch_failure_is_visible(wired):
    logs, setup = wired
    setup(exc=OSError("no python"))
    duty._maybe_ops_digest()
    assert any("跑不起来" in x for x in logs), logs
