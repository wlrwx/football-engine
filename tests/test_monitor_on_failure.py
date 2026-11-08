"""失败轮也必须监控（2026-11-07 三天停摆事故）。

事故：session 死后 duty 在 mint 处提前 return，而摘要与派单都挂在成功轮末尾，
于是 ops_digest 停在 11-04 16:05、red_flags 为空长达 73 小时；每日分诊只能靠
「摘要过期」猜成合盖/launchd 掉了，报不出真因；每日探索工单也断了 3 天。
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


duty = _load("duty_monitor_failure", "scripts/njstats_duty.py")
disp = _load("disp_monitor_failure", "scripts/agent_dispatch.py")


def test_mint_failure_still_runs_digest_and_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(duty, "ensure_on_main", lambda: True)
    monkeypatch.setattr(duty, "watch_main_writes", lambda: [])
    monkeypatch.setattr(duty.os, "chdir", lambda p: None)

    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(duty, "sh", lambda *a, **k: R())
    monkeypatch.setattr(duty, "mint", lambda: None)
    monkeypatch.setattr(duty, "_mint_action", lambda t: (20, "本轮：mint 失败，需人工登录"))
    monkeypatch.setattr(duty, "_cached_jwt_usable", lambda *a, **k: None)
    monkeypatch.setattr(duty, "_net_fail_note", lambda ts: "")
    monkeypatch.setattr(duty, "log", lambda m: None)
    monkeypatch.setattr(duty, "_maybe_ops_digest", lambda: calls.append("digest"))
    monkeypatch.setattr(duty, "_maybe_agent_dispatch", lambda: calls.append("dispatch"))

    assert duty.main() == 20
    assert calls == ["digest", "dispatch"], f"失败轮没跑监控: {calls}"


def test_credential_root_cause_suppresses_symptom_tickets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(disp, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(disp, "DIGEST_FILE", tmp_path / "digest.json")
    monkeypatch.setattr(disp, "TOKEN_FILE", tmp_path / "nope")
    (tmp_path / "digest.json").write_text(json.dumps({"red_flags": [
        {"code": "error_mint", "detail": "session 死", "action": "人工播种"},
        {"code": "snapshot_not_committed", "detail": "73h 未入库", "action": "查提交链"},
        {"code": "today_site_odds_missing", "detail": "今天缺", "action": "查 duty"},
    ]}), encoding="utf-8")
    assert disp.main(["--dry-run", "--no-ci", "--no-scout"]) == 0
    out = capsys.readouterr().out
    assert "症状类不派给 agent" in out, out
    assert "将建 issue" not in out, "登录死了还派 agent 去修症状"


def test_without_root_cause_symptoms_are_still_dispatched(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(disp, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(disp, "DIGEST_FILE", tmp_path / "digest.json")
    monkeypatch.setattr(disp, "TOKEN_FILE", tmp_path / "nope")
    (tmp_path / "digest.json").write_text(json.dumps({"red_flags": [
        {"code": "snapshot_not_committed", "detail": "30h", "action": "查提交链"},
    ]}), encoding="utf-8")
    assert disp.main(["--dry-run", "--no-ci", "--no-scout"]) == 0
    assert "将建 issue" in capsys.readouterr().out
