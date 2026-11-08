"""mint 端点挂了不等于不能干活（2026-11-08 四天停摆的真因）。

实测：`/api/user/current`（mint）502 四天，但真正采数据/投注的
`/njserver/openApi/` 网关 200，磁盘上 11-04 的旧 JWT 照样取到全板 235 场 /
222 场带站点赔率。duty 原来把整条链闸在 mint 成功上，明明能干活却四天不采集不下注。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_fallback", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_fallback"] = duty
SPEC.loader.exec_module(duty)


def _jwt(tmp_path, monkeypatch, value):
    f = tmp_path / "njstats_jwt"
    if value is not None:
        f.write_text(value, encoding="utf-8")
    monkeypatch.setattr(duty, "JWT_FILE", f)
    return f


def test_cached_jwt_missing_or_empty_is_unusable(tmp_path, monkeypatch):
    _jwt(tmp_path, monkeypatch, None)
    assert duty._cached_jwt_usable(probe=lambda t: []) is None
    _jwt(tmp_path, monkeypatch, "   ")
    assert duty._cached_jwt_usable(probe=lambda t: []) is None


def test_empty_list_response_is_still_a_valid_token(tmp_path, monkeypatch):
    # 网关接受 token 但账号暂无条目 → 返回 []，这不是失败，不能判成不可用
    _jwt(tmp_path, monkeypatch, "tok-abc")
    assert duty._cached_jwt_usable(probe=lambda t: []) == "tok-abc"
    assert duty._cached_jwt_usable(probe=lambda t: None) is None


def test_probe_exception_is_unusable_not_crash(tmp_path, monkeypatch):
    _jwt(tmp_path, monkeypatch, "tok-abc")

    def boom(t):
        raise RuntimeError("network down")
    assert duty._cached_jwt_usable(probe=boom) is None


def test_resolve_passes_through_good_token(monkeypatch):
    monkeypatch.setattr(duty, "_cached_jwt_usable", lambda: "should-not-be-used")
    tok, code, msg = duty._resolve_mint("fresh-token")
    assert (tok, code, msg) == ("fresh-token", None, "")


def test_server_outage_falls_back_to_cached_jwt(tmp_path, monkeypatch):
    _jwt(tmp_path, monkeypatch, "cached-token")
    monkeypatch.setattr(duty, "_cached_jwt_usable", lambda: "cached-token")
    tok, code, msg = duty._resolve_mint(duty.MINT_SERVER)
    assert tok == "cached-token" and code is None, "网关可用却仍然停摆"
    assert "不需人工播种" in msg and "缓存 JWT" in msg, msg


def test_session_dead_also_falls_back_if_gateway_accepts_cached(tmp_path, monkeypatch):
    _jwt(tmp_path, monkeypatch, "cached-token")
    monkeypatch.setattr(duty, "_cached_jwt_usable", lambda: "cached-token")
    tok, code, _ = duty._resolve_mint(None)
    assert tok == "cached-token" and code is None


def test_no_fallback_when_cached_also_dead(monkeypatch):
    monkeypatch.setattr(duty, "_cached_jwt_usable", lambda: None)
    monkeypatch.setattr(duty, "_net_fail_note", lambda *a, **k: "")
    tok, code, msg = duty._resolve_mint(None)
    assert tok is None and code == 20 and "需人工登录" in msg
    tok2, code2, msg2 = duty._resolve_mint(duty.MINT_SERVER)
    assert tok2 is None and code2 == 0, "厂商故障应等下轮，不是要人登录"
    assert "不需人工登录" in msg2
