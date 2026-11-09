"""播种登录的取 token / 存登录态逻辑（2026-11-09）。

钉的是这次真实踩到的坑：厂商 `/api/user/current` 端点 5xx 时，用户明明登录成功、
token 已写进 localStorage，旧脚本却只认那个端点、又只存 SESSION，于是永远判不了
成功、空转到超时（表现为「登录后跳空白页、没返回成功」）。

这里用假浏览器真跑 _read_token / _save，不依赖网络、不开真窗口：
1. 优先读 localStorage，厂商端点挂了也能拿到 token；
2. localStorage 没有才退回 /api/user/current；
3. _save 必存 JWT；SESSION 有则存、没有不阻塞。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "njstats_seed_login", ROOT / "scripts" / "njstats_seed_login.py")
seed = importlib.util.module_from_spec(SPEC)
sys.modules["njstats_seed_login"] = seed
SPEC.loader.exec_module(seed)


class FakePage:
    """按脚本内容分流：localStorage 读 / 清、/api/user/current 读。"""

    def __init__(self, ls_token=None, api_token=None, ls_raises=False):
        self.ls_token = ls_token
        self.api_token = api_token
        self.ls_raises = ls_raises
        self.api_calls = 0

    def evaluate(self, script):
        if "removeItem" in script:
            self.ls_token = None
            return None
        if "localStorage.getItem" in script:
            if self.ls_raises:
                raise RuntimeError("页面还没加载好")
            return self.ls_token
        if "/api/user/current" in script:
            self.api_calls += 1
            return self.api_token
        return None


class FakeCtx:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self, urls):
        return self._cookies


def test_read_token_prefers_localstorage_without_calling_vendor():
    page = FakePage(ls_token="jwt-from-login", api_token="should-not-be-used")
    assert seed._read_token(page) == "jwt-from-login"
    assert page.api_calls == 0, "localStorage 有 token 就不该再打厂商端点"


def test_read_token_falls_back_to_vendor_when_localstorage_empty():
    page = FakePage(ls_token=None, api_token="jwt-from-api")
    assert seed._read_token(page) == "jwt-from-api"
    assert page.api_calls == 1


def test_read_token_survives_localstorage_error_and_uses_vendor():
    page = FakePage(ls_token=None, api_token="jwt", ls_raises=True)
    assert seed._read_token(page) == "jwt"


def test_read_token_none_when_both_empty():
    assert seed._read_token(FakePage()) is None


def test_save_always_writes_jwt_and_session_when_present(tmp_path, monkeypatch):
    jwt = tmp_path / "njstats_jwt"
    sess = tmp_path / "njstats_session.json"
    monkeypatch.setattr(seed, "JWT_FILE", jwt)
    monkeypatch.setattr(seed, "OUT", sess)
    ctx = FakeCtx([{"name": "SESSION", "value": "s3cret", "domain": ".njstats.cn"}])
    assert seed._save(ctx, "  jwt-token  ") is True
    assert jwt.read_text(encoding="utf-8") == "jwt-token", "要去掉首尾空白"
    assert json.loads(sess.read_text(encoding="utf-8"))["SESSION"] == "s3cret"
    assert jwt.stat().st_mode & 0o777 == 0o600


def test_save_writes_jwt_even_without_session_cookie(tmp_path, monkeypatch):
    """今天实测站点没给 SESSION——绝不能因此不存 JWT、更不能判失败。"""
    jwt = tmp_path / "njstats_jwt"
    sess = tmp_path / "njstats_session.json"
    monkeypatch.setattr(seed, "JWT_FILE", jwt)
    monkeypatch.setattr(seed, "OUT", sess)
    ctx = FakeCtx([{"name": "other", "value": "x", "domain": ".njstats.cn"}])
    assert seed._save(ctx, "jwt-token") is False
    assert jwt.read_text(encoding="utf-8") == "jwt-token"
    assert not sess.exists(), "没有 SESSION 就不该写 session 文件"
