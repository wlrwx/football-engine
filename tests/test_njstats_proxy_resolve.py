"""代理单点回归（2026-11-13 事故）：Clash 进程死不能停掉采集/挑战/续期链。

api_submit 原先 Mac 写死 127.0.0.1:7897：代理死 → 网关全 ProxyError → duty 整轮跳过；
而同一真实地址直连实测 200。现在按 环境变量 > 探测端口 > 直连 决定，直连必须显式
None（macOS 上 proxies=None 仍回落系统代理设置，同样指向死 Clash）。
续期/播种两个 playwright 浏览器 likewise 不吃系统代理（--no-proxy-server）。
"""
import importlib.util
import os
import socket
import sys
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["NJSTATS_NOPROXY"] = "1"   # 导入时不探真实端口
SPEC = importlib.util.spec_from_file_location("njstats_api_submit", ROOT / "scripts" / "njstats_api_submit.py")
api = importlib.util.module_from_spec(SPEC)
sys.modules["njstats_api_submit"] = api
SPEC.loader.exec_module(api)

DIRECT = {"http": None, "https": None}


def test_noproxy_env_disables_explicitly():
    assert api._resolve_proxy() == DIRECT


def test_explicit_proxy_env_honored(monkeypatch):
    monkeypatch.delenv("NJSTATS_NOPROXY", raising=False)
    monkeypatch.setenv("NJSTATS_PROXY", "http://127.0.0.1:9999")
    assert api._resolve_proxy() == {"http": "http://127.0.0.1:9999",
                                    "https": "http://127.0.0.1:9999"}


def test_dead_proxy_falls_back_to_direct(monkeypatch):
    monkeypatch.delenv("NJSTATS_NOPROXY", raising=False)
    monkeypatch.delenv("NJSTATS_PROXY", raising=False)

    def dead(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", dead)
    assert api._resolve_proxy() == DIRECT, "代理死必须显式 None 直连，None 会回落 macOS 系统代理"


def test_live_proxy_is_used(monkeypatch):
    monkeypatch.delenv("NJSTATS_NOPROXY", raising=False)
    monkeypatch.delenv("NJSTATS_PROXY", raising=False)
    seen = []

    def alive(addr, timeout=None):
        seen.append(addr[1])
        return nullcontext()

    monkeypatch.setattr(socket, "create_connection", alive)
    assert api._resolve_proxy() == {"http": "http://127.0.0.1:7897",
                                    "https": "http://127.0.0.1:7897"}
    assert seen == [7897]


def test_browsers_bypass_system_proxy():
    for name in ("njstats_refresh_session.py", "njstats_seed_login.py"):
        txt = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "--no-proxy-server" in txt, f"{name} 仍吃系统代理，Clash 死会把续期/播种也停掉"