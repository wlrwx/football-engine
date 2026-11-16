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


# ---------- 把 daily-forecast 的最近触发日期传给摘要（2026-11-16） ----------
#
# 摘要自己离线，分不清「今天的产物没落库」和「今天这一轮 CI 还没跑」，
# 于是北京 00:05 那轮每晚必报数据洞、连派七张查不出故障的工单。
# duty 本来就为看门狗查这个 API，把日期捎给它即可，判定仍留在摘要侧。

def test_forecast_last_date_is_passed_to_the_digest(wired, monkeypatch):
    logs, setup = wired
    calls = setup(rc=0, ctx={"red_flags": []})
    monkeypatch.setattr(duty, "_daily_last_run_info", lambda: (4.5, "2026-11-15"))
    duty._maybe_ops_digest()
    assert "--forecast-last-date" in calls[0], calls[0]
    assert calls[0][calls[0].index("--forecast-last-date") + 1] == "2026-11-15", calls[0]


def test_unknown_forecast_date_is_not_fabricated(wired, monkeypatch):
    """API 取不到就别传参数：摘要那边要按「未知」保守喊洞，不能拿到一个假日期。"""
    logs, setup = wired
    calls = setup(rc=0, ctx={"red_flags": []})
    monkeypatch.setattr(duty, "_daily_last_run_info", lambda: None)
    duty._maybe_ops_digest()
    assert "--forecast-last-date" not in calls[0], calls[0]


def test_api_shape_change_does_not_kill_the_round(wired, monkeypatch):
    """摘要在失败轮也要跑（11-07 事故的教训），所以多带这一个输入绝不能成为新的崩点。"""
    logs, setup = wired
    calls = setup(rc=0, ctx={"red_flags": []})

    def weird():
        raise AttributeError("'list' object has no attribute 'get'")
    monkeypatch.setattr(duty, "_daily_last_run_info", weird)
    duty._maybe_ops_digest()                      # 不抛
    assert len(calls) == 1 and "--forecast-last-date" not in calls[0], calls
    assert any("触发日期取不到" in x for x in logs), logs
