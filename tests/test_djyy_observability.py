"""DJYY 可观测性回归测试（2026-10-11）

## 背景

2026-10-11 实测确诊：**从 GitHub Actions 访问 djyylive.com 返回 HTTP 403**，
同一时刻本地返回 200。而 `djyy_prob` 从 **2026-08-15 起就在账本里恒为 null**，
持续 3.5 周无人察觉。

原因是 `_get_json` 此前把一切异常吞成 `return None`，
于是"被 Cloudflare 拦截"与"当天没数据"在日志里**完全一样**。

本测试锁住：403 必须可区分、必须不重试、必须给出可执行的修复指引。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.sources import djyy as djyy_mod  # noqa: E402


class _FakeResp:
    def __init__(self, status_code=200, payload=None, raw=b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._raw = raw

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture()
def patched_get(monkeypatch):
    """把 requests.get 替换成可控假实现，并记录调用次数。"""
    calls = []

    def _install(resp):
        def fake(url, **kw):
            calls.append(url)
            return resp() if callable(resp) else resp
        monkeypatch.setattr(djyy_mod.requests, "get", fake)
        djyy_mod.LAST_ERROR.clear()
        return calls

    return _install


# ---------- 403 必须可观测 ----------

def test_403_is_distinguished_from_empty(patched_get, capsys):
    """被拦截 ≠ 当天没数据。这个区分是靠 LAST_ERROR + 控制台输出做的。"""
    patched_get(_FakeResp(403, raw=b"<html>blocked</html>"))
    src = djyy_mod.DJYYSource()
    assert src._get_json("/api/leagues/fixtures") is None
    assert djyy_mod.LAST_ERROR["kind"] == "blocked_by_edge"
    assert "403" in djyy_mod.LAST_ERROR["detail"]
    out = capsys.readouterr().out
    assert "blocked_by_edge" in out and "403" in out, "控制台必须能看到 403"


def test_401_also_treated_as_blocked(patched_get):
    patched_get(_FakeResp(401))
    djyy_mod.DJYYSource()._get_json("/api/match/1/comparison")
    assert djyy_mod.LAST_ERROR["kind"] == "blocked_by_edge"


def test_blocked_is_not_retried(patched_get):
    """被拦截时重试无意义（不是限流），应当立即返回避免白等。"""
    calls = patched_get(_FakeResp(403))
    djyy_mod.DJYYSource()._get_json("/api/leagues/fixtures", retries=3)
    assert len(calls) == 1, "403 不应重试"


def test_other_http_errors_are_retried_then_reported(patched_get):
    calls = patched_get(_FakeResp(500))
    djyy_mod.DJYYSource()._get_json("/api/leagues/fixtures", retries=2)
    assert len(calls) == 2, "5xx 属于瞬时故障，应当重试"
    assert djyy_mod.LAST_ERROR["kind"] == "http_error"


def test_network_error_kind(patched_get, monkeypatch):
    import requests as rq

    def boom(url, **kw):
        raise rq.ConnectionError("dns fail")

    monkeypatch.setattr(djyy_mod.requests, "get", boom)
    djyy_mod.LAST_ERROR.clear()
    djyy_mod.DJYYSource()._get_json("/api/leagues/fixtures", retries=1)
    assert djyy_mod.LAST_ERROR["kind"] == "network"


def test_success_clears_last_error(patched_get):
    patched_get(_FakeResp(200, payload={"ok": 1}))
    djyy_mod.DJYYSource()._get_json("/api/leagues/fixtures")
    assert djyy_mod.LAST_ERROR == {}, "成功后必须清空上一次错误，否则状态文件会长期挂着旧错"


def test_error_records_whether_key_configured(patched_get):
    patched_get(_FakeResp(403))
    djyy_mod.DJYYSource()._get_json("/x")
    assert "djyy_key_configured" in djyy_mod.LAST_ERROR, \
        "必须记录 DJYY_KEY 是否配置，否则排查时看不出是缺密钥还是缺放行"


# ---------- 密钥注入 ----------

def test_key_header_only_when_env_set(monkeypatch):
    import importlib
    monkeypatch.setenv("DJYY_KEY", "  s3cret  ")
    m = importlib.reload(djyy_mod)
    try:
        assert m.DJYY_KEY == "s3cret", "应当 strip 空白"
        assert m.HEADERS.get("X-DJYY-Key") == "s3cret"
    finally:
        monkeypatch.delenv("DJYY_KEY", raising=False)
        importlib.reload(m)


def test_no_key_header_when_env_absent(monkeypatch):
    import importlib
    monkeypatch.delenv("DJYY_KEY", raising=False)
    m = importlib.reload(djyy_mod)
    try:
        assert m.DJYY_KEY == ""
        assert "X-DJYY-Key" not in m.HEADERS, \
            "未配置时不得发送该 header（保持修复前行为，便于灰度）"
    finally:
        importlib.reload(m)


def test_missing_key_hint_in_403_message(patched_get, capsys, monkeypatch):
    """没配密钥时，报错要直接说"当前进程未读到 DJYY_KEY"。"""
    monkeypatch.delenv("DJYY_KEY", raising=False)
    import importlib
    m = importlib.reload(djyy_mod)
    try:
        patched_get(_FakeResp(403))
        m.DJYYSource()._get_json("/x")
        out = capsys.readouterr().out
        assert "DJYY_KEY" in out, "403 提示里应点名 DJYY_KEY"
    finally:
        monkeypatch.delenv("DJYY_KEY", raising=False)
        importlib.reload(m)


# ---------- 状态落盘带上真实原因 ----------

def test_manager_status_records_block_reason(tmp_path, monkeypatch):
    """enrich_from_djyy 写状态时必须带上 djyy 的真实失败原因。"""
    from engine.sources.manager import SourceManager
    m = SourceManager(tmp_path)
    patched_get = lambda r: monkeypatch.setattr(djyy_mod.requests, "get", r)  # noqa: E731
    patched_get(lambda *a, **k: _FakeResp(403))
    djyy_mod.LAST_ERROR.clear()
    m.enrich_from_djyy([], __import__("datetime").date(2026, 10, 12))
    st = json.loads((tmp_path / "djyy_status.json").read_text(encoding="utf-8"))
    assert st["healthy"] is False
    assert "403" in (st["error"] or ""), f"状态文件应记录 403，实际: {st['error']!r}"
    assert "blocked_by_edge" in (st["error"] or "")


# ---------- CI 探针 ----------

def test_api_test_probe_wired():
    yml = (ROOT / ".github" / "workflows" / "api-test.yml").read_text(encoding="utf-8")
    code = "\n".join(l for l in yml.splitlines() if not l.strip().startswith("#"))
    assert "DJYY_KEY" in code, "探针必须能带密钥"
    assert 'import subprocess, json, re, os' in code, "探针脚本需要 import os"
    assert "blocked by edge" in code.lower(), "403 必须有专门的分支说明"
    # 只看 DJYY 那一段 —— 2026-07-20 在 SINA 探针里是合法的（SINA 不提供当日历史）
    djyy_block = code[code.index("4. DJYY"):code.index("5. SINA")]
    assert "2026-07-20" not in djyy_block, \
        "DJYY 探针不得硬编码三个月前的日期窗口（否则正常时也只报 0 fixtures）"
    assert "date.today()" in djyy_block, "DJYY 探针应用当天窗口"


def test_api_test_probe_script_is_valid_python():
    """YAML `run: |` 里的内嵌脚本必须能被抽出并解析。

    刻意不 import yaml —— CI 的 requirements-dev.txt 只有 pytest 和 ruff，
    没有 PyYAML（PR#11 已在这个问题上红过一次）。
    直接按 PYEOF 标记做文本抽取即可。
    """
    import ast
    txt = (ROOT / ".github" / "workflows" / "api-test.yml").read_text(encoding="utf-8")
    lines = txt.splitlines()
    start = next(i for i, l in enumerate(lines) if "PYEOF" in l and "<<" in l)
    end = next(i for i, l in enumerate(lines) if i > start and "PYEOF" in l)
    # YAML 块标量统一缩进 10 空格，剥离后即为脚本本体
    body = []
    for l in lines[start + 1:end]:
        body.append(l[10:] if l.startswith(" " * 10) else l)
    ast.parse("\n".join(body))
