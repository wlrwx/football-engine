"""自助重登 v2（DrissionPage 真 Chrome）的行为护栏测试（2026-11-23 重写）。

v1（playwright）被真机对比证伪：无头点击发不出登录 POST、有头也多次空转，
用户手开浏览器秒登——问题在工具。v2 用 DrissionPage 接管真实 Chrome。
本套测试钉住 v2 的每个安全不变式：凭据形状、验证码只识别误报排除、
OCR 上限=2 且尝试即计数、提交前必须清残留（#121 假成功教训）、
DrissionPage 懒加载（CI 无库降级叫人工）、默认有头（无头只留调试开关）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "njstats_self_login", ROOT / "scripts" / "njstats_self_login.py")
selflogin = importlib.util.module_from_spec(SPEC)
sys.modules["njstats_self_login"] = selflogin
SPEC.loader.exec_module(selflogin)


def _read_creds_path(tmp_path, monkeypatch, content=None):
    f = tmp_path / "njstats_account"
    if content is not None:
        f.write_text(content, encoding="utf-8")
    monkeypatch.setattr(selflogin, "CREDS_FILE", f)
    return f


def test_read_creds_ok(tmp_path, monkeypatch):
    _read_creds_path(tmp_path, monkeypatch, "someone@mail.com\nsecret-pw\n")
    assert selflogin.read_creds() == ("someone@mail.com", "secret-pw")


def test_read_creds_missing_or_malformed(tmp_path, monkeypatch):
    _read_creds_path(tmp_path, monkeypatch)
    assert selflogin.read_creds() is None
    _read_creds_path(tmp_path, monkeypatch, "no-at-sign\npw\n")
    assert selflogin.read_creds() is None
    _read_creds_path(tmp_path, monkeypatch, "a@b.c\n\n")
    assert selflogin.read_creds() is None


def test_captcha_detection_only_recognizes():
    for s in ("请完成滑块验证", "请拖动滑块完成拼图", "geetest", "hCaptcha", "图形验证码"):
        assert selflogin._looks_like_captcha(s), s
    for ok in ("邮箱验证码登录 密码登录", "获取邮箱验证码", "验证码登录 tab"):
        assert not selflogin._looks_like_captcha(ok), ok


def test_jwt_in_obj_finds_bytes_and_nested():
    j = b"eyJhbGciOiJIUzI1NiJ9." + b"A" * 40 + b"." + b"B" * 30
    assert selflogin._jwt_in_obj({"data": {"token": j.decode()}})[:3] == "eyJ"
    assert selflogin._jwt_in_obj([b"noise", j])[:3] == "eyJ"
    assert selflogin._jwt_in_obj("plain text") is None
    assert selflogin._jwt_in_obj(None) is None


def test_ocr_attempt_cap_is_two():
    assert selflogin.MAX_OCR_ATTEMPTS == 2, "第 3 次试码疑似暴破→风控封号风险"


def test_ocr_switch_off_short_circuits(monkeypatch):
    monkeypatch.setenv("SELF_LOGIN_NO_OCR", "1")
    assert selflogin._try_solve_captcha(object()) is False  # 不碰 tab 即返回


def test_structural_guards():
    src = (ROOT / "scripts" / "njstats_self_login.py").read_text(encoding="utf-8")
    # 1) 提交前清残留 token（#121 假成功教训）
    pre = src.split("btn.click()")[0]
    assert "removeItem('token')" in pre
    # 2) DrissionPage 懒加载：CI 无库时模块可导入、运行期降级
    assert "from DrissionPage import" not in src.split("def main")[0]
    assert "DrissionPage 未安装" in src
    # 3) 尝试计数在求解之前
    seg = src.split("if ocr_tries < MAX_OCR_ATTEMPTS")[1].split("continue")[0]
    assert seg.index("ocr_tries += 1") < seg.index("_try_solve_captcha")
    # 4) 默认有头，无头只在显式开关下
    assert "if headless:" in src and 'SELF_LOGIN_HEADLESS' in src
    # 5) 明确「密码错误」不连发
    assert "不连发重登" in src
