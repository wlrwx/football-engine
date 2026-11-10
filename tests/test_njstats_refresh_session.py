"""无头续期的「能不能续」判定（2026-11-09）。

这次的真实坑：厂商 /api/user/current 5xx 时，旧脚本把 502 判成「登录态已失效」，
于是喊人白跑一趟、还可能拿空结果覆盖掉本来能用的登录态。这里锁三件事：
1. 5xx / 网络错误 = vendor（不可判定），只有 401/403 才是 auth_dead；
2. vendor / auth_dead 都**不覆盖**已有登录态文件；
3. 结论落 health 文件，供摘要出「登录态将失效」的提前预警；--check 什么都不写。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "njstats_refresh_session", ROOT / "scripts" / "njstats_refresh_session.py")
rs = importlib.util.module_from_spec(SPEC)
sys.modules["njstats_refresh_session"] = rs
SPEC.loader.exec_module(rs)


def test_classify_status_separates_vendor_from_auth_death():
    assert rs.classify_status(200) == "ok"
    for st in (500, 502, 503, -1, 0):
        assert rs.classify_status(st) == "vendor", f"HTTP {st} 是厂商/网络问题，不是登录态死亡"
    for st in (401, 403, 400):
        assert rs.classify_status(st) == "auth_dead", f"HTTP {st} 才是登录态问题"


def _sandbox(tmp_path, monkeypatch):
    sess = tmp_path / "njstats_session.json"
    jwt = tmp_path / "njstats_jwt"
    health = tmp_path / "njstats_session_health.json"
    monkeypatch.setattr(rs, "OUT", sess)
    monkeypatch.setattr(rs, "JWT_FILE", jwt)
    monkeypatch.setattr(rs, "HEALTH", health)
    return sess, jwt, health


def test_ok_writes_session_jwt_and_health(tmp_path, monkeypatch, capsys):
    sess, jwt, health = _sandbox(tmp_path, monkeypatch)
    rc = rs.apply_probe(200, "tok-1", {"SESSION": "sess-1"})
    assert rc == rs.RC_OK
    assert json.loads(sess.read_text(encoding="utf-8"))["SESSION"] == "sess-1"
    assert jwt.read_text(encoding="utf-8") == "tok-1"
    assert json.loads(health.read_text(encoding="utf-8"))["result"] == "ok"


def test_vendor_5xx_does_not_touch_existing_session(tmp_path, monkeypatch, capsys):
    sess, jwt, health = _sandbox(tmp_path, monkeypatch)
    sess.write_text('{"SESSION": "old", "seeded_at": "2026-10-20 02:05:32"}', encoding="utf-8")
    rc = rs.apply_probe(502, None, {})
    assert rc == rs.RC_VENDOR
    assert json.loads(sess.read_text(encoding="utf-8"))["SESSION"] == "old", "厂商 5xx 不该覆盖登录态"
    assert not jwt.exists()
    assert json.loads(health.read_text(encoding="utf-8"))["result"] == "vendor"


def test_auth_death_is_recorded_for_the_early_warning(tmp_path, monkeypatch, capsys):
    sess, _jwt, health = _sandbox(tmp_path, monkeypatch)
    sess.write_text('{"SESSION": "old"}', encoding="utf-8")
    rc = rs.apply_probe(401, None, {})
    assert rc == rs.RC_DEAD
    assert json.loads(sess.read_text(encoding="utf-8"))["SESSION"] == "old"
    body = json.loads(health.read_text(encoding="utf-8"))
    assert body["result"] == "auth_dead" and "401" in body["detail"]


def test_check_only_writes_nothing(tmp_path, monkeypatch, capsys):
    sess, jwt, health = _sandbox(tmp_path, monkeypatch)
    assert rs.apply_probe(200, "tok", {"SESSION": "s"}, check_only=True) == rs.RC_OK
    assert not sess.exists() and not jwt.exists() and not health.exists()


def test_200_without_anything_saveable_is_death_for_humans(tmp_path, monkeypatch, capsys):
    """站点 200 但既不回 token 也没 SESSION = 登录态无法落盘，必须让人看一眼。"""
    _sess, jwt, health = _sandbox(tmp_path, monkeypatch)
    assert rs.apply_probe(200, None, {}) == rs.RC_DEAD
    assert not jwt.exists()
    assert json.loads(health.read_text(encoding="utf-8"))["result"] == "auth_dead"
