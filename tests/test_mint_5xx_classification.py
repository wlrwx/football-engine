"""5xx 是厂商故障，不是登录态问题（2026-11-07 误诊事故）。

实测：站点首页 200，`/api/*` 全部 502，且**带 cookie 与不带 cookie 都是 502**。
原代码把任何非 200 都判成「session 死」并喊人工登录，11-04→11-07 三天指错方向，
用户白跑登录。5xx 必须与 401/403 分开。
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


duty = _load("duty_5xx", "scripts/njstats_duty.py")
ops = _load("ops_5xx", "scripts/ops_digest.py")
disp = _load("disp_5xx", "scripts/agent_dispatch.py")


class Resp:
    def __init__(self, code):
        self.status_code = code
        self.text = "<html>502 Bad Gateway</html>"

    def json(self):
        return {"data": {"token": "tok"}}


def _sess(tmp_path, monkeypatch):
    f = tmp_path / "session.json"
    f.write_text(json.dumps({"SESSION": "cookie-value"}), encoding="utf-8")
    monkeypatch.setattr(duty, "SESS", f)
    monkeypatch.setattr(duty, "log", lambda m: None)


def test_502_returns_server_sentinel_not_none(tmp_path, monkeypatch):
    _sess(tmp_path, monkeypatch)
    monkeypatch.setattr(duty.requests, "get", lambda *a, **k: Resp(502))
    called = []
    monkeypatch.setattr(duty, "autoheal_session", lambda: called.append(1) or True)
    assert duty.mint() == duty.MINT_SERVER
    assert called == [], "5xx 不该去做无头续期（续期修不好厂商网关）"


def test_503_and_500_also_server_sentinel(tmp_path, monkeypatch):
    _sess(tmp_path, monkeypatch)
    for code in (500, 503, 504):
        monkeypatch.setattr(duty.requests, "get", lambda *a, _c=code, **k: Resp(_c))
        assert duty.mint() == duty.MINT_SERVER, code


def test_401_still_means_session_dead(tmp_path, monkeypatch):
    _sess(tmp_path, monkeypatch)
    monkeypatch.setattr(duty.requests, "get", lambda *a, **k: Resp(401))
    monkeypatch.setattr(duty, "autoheal_session", lambda: False)
    assert duty.mint() is None, "401 仍应判为需人工登录"


def test_action_for_server_is_retry_not_human_login(tmp_path):
    code, msg = duty._mint_action(duty.MINT_SERVER)
    assert code == 0, "厂商故障应等下轮重试，不是退出码 20"
    assert "不需人工登录" in msg and "厂商" in msg, msg
    code2, msg2 = duty._mint_action(None)
    assert code2 == 20 and "需人工登录" in msg2


def test_digest_classifies_5xx_as_upstream_not_mint():
    line = ("本轮：站点 API 服务端故障（5xx）导致 mint 失败，跳过"
            "（厂商侧问题，与登录态无关，不需人工登录）（自 11-04 16:05 起已 73.0h）")
    got = ops.error_summary([("11-07 17", line)])
    assert "upstream" in got, got
    assert "mint" not in got, f"5xx 被误分类成登录问题: {got}"
    assert "不要人工播种" in got["upstream"]["action"]


def test_upstream_root_cause_suppresses_symptom_tickets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(disp, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(disp, "DIGEST_FILE", tmp_path / "digest.json")
    monkeypatch.setattr(disp, "TOKEN_FILE", tmp_path / "nope")
    (tmp_path / "digest.json").write_text(json.dumps({"red_flags": [
        {"code": "error_upstream", "detail": "厂商 502", "action": "等恢复"},
        {"code": "snapshot_not_committed", "detail": "73h", "action": "查提交链"},
    ]}), encoding="utf-8")
    assert disp.main(["--dry-run", "--no-ci", "--no-scout"]) == 0
    out = capsys.readouterr().out
    assert "症状类不派给 agent" in out and "将建 issue" not in out, out
