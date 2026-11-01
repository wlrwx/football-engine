"""断网 ≠ 登录失效（2026-10-30 17:35~11-01 11:35 实测 42h、81 轮误报「需人工登录」）。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_mint_net", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_mint_net"] = duty
SPEC.loader.exec_module(duty)


def test_mint_action_routes_network_and_login_apart():
    code, msg = duty._mint_action(duty.MINT_NET)
    assert code == 0 and "不需人工登录" in msg
    code, msg = duty._mint_action(None)
    assert code == 20 and "需人工登录" in msg
    assert duty._mint_action("eyJhbGciOiJIU") == (None, "")


def test_mint_returns_sentinel_on_ssl_error(tmp_path, monkeypatch):
    sess = tmp_path / "session.json"
    sess.write_text(json.dumps({"SESSION": "cookie-value"}), encoding="utf-8")
    monkeypatch.setattr(duty, "SESS", sess)

    def boom(*a, **k):
        raise duty.requests.exceptions.SSLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain")
    monkeypatch.setattr(duty.requests, "get", boom)
    monkeypatch.setattr(duty, "log", lambda m: None)

    assert duty.mint() == duty.MINT_NET
