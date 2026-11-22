"""自助重登（2026-11-22 用户裁决：以后自己登录，别叫人手工）。

钉三件事：
1. 凭据文件形状校验（缺失/畸形 → 不可用，绝不抛带内容的异常）；
2. 验证码「只识别不通过」的特征判断；
3. duty 钩子触发条件：remember-me 死（refresh rc=1）且有凭据 → 尝试 self-login；
   rc=0/vendor 不触发；无凭据不触发；DUTY_SKIP_SELF_LOGIN=1 总开关可关。
"""
from __future__ import annotations

import importlib.util
import subprocess as _sp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


selflogin = _load("njstats_self_login_t", "scripts/njstats_self_login.py")


def test_read_creds_ok(tmp_path, monkeypatch):
    f = tmp_path / "njstats_account"
    f.write_text("someone@mail.com\nsecret-pw\n", encoding="utf-8")
    monkeypatch.setattr(selflogin, "CREDS_FILE", f)
    assert selflogin.read_creds() == ("someone@mail.com", "secret-pw")


def test_read_creds_missing_or_malformed(tmp_path, monkeypatch):
    monkeypatch.setattr(selflogin, "CREDS_FILE", tmp_path / "nope")
    assert selflogin.read_creds() is None
    f = tmp_path / "bad"
    f.write_text("no-at-sign\npw\n", encoding="utf-8")
    monkeypatch.setattr(selflogin, "CREDS_FILE", f)
    assert selflogin.read_creds() is None
    f.write_text("a@b.c\n\n", encoding="utf-8")
    assert selflogin.read_creds() is None, "空密码行不算有凭据"


def test_captcha_detection_only_recognizes():
    for s in ("请完成滑块验证", "请拖动滑块完成拼图", "geetest", "hCaptcha", "图形验证码"):
        assert selflogin._looks_like_captcha(s), s
    # 模式切换标签自带「验证码」字样——不是人机验证，必须不报（否则自助重登永远不敢跑）
    for ok in ("邮箱验证码登录 密码登录", "获取邮箱验证码", "验证码登录 tab"):
        assert not selflogin._looks_like_captcha(ok), ok


def test_captcha_never_aborts_before_submit():
    """2026-11-23 演练实锤：手机号 tab 的隐藏 DOM 含图形验证码控件，
    拿全量 HTML 预检会把正常密码页误判成 21。验证码判定只能作为
    **提交超时后的分类器**——源码里不得再有提交前的 _looks_like_captcha 调用。"""
    src = (ROOT / "scripts" / "njstats_self_login.py").read_text(encoding="utf-8")
    pre_click = src.split('btn.click()')[0]
    assert "_looks_like_captcha(" not in pre_click.replace("def _looks_like_captcha(", "")
    assert "page.content()" not in src, "全量 HTML 判定已废弃，只准用可见文本"


# ---- duty 钩子 ----

def _duty_env(tmp_path, monkeypatch, *, refresh_rc=1, creds=True):
    duty = _load("njstats_duty_selflogin_t", "scripts/njstats_duty.py")
    home = tmp_path / "h"
    fe = home / ".football-engine"
    home.mkdir(exist_ok=True)
    fe.mkdir(exist_ok=True)
    (fe / "njstats_session.json").write_text("{}", encoding="utf-8")
    if creds:
        (fe / "njstats_account").write_text("a@b.c\npw\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[1] if len(cmd) > 1 else str(cmd))
        if "refresh_session" in str(cmd):
            return _sp.CompletedProcess(cmd, refresh_rc, stdout="登录态已失效", stderr="")
        return _sp.CompletedProcess(cmd, 0, stdout="✅ 自助重登成功", stderr="")
    monkeypatch.setattr(duty, "HOME", home)
    monkeypatch.setattr(duty, "SESS", fe / "njstats_session.json")
    monkeypatch.setattr(duty, "SESSION_HEALTH", fe / "h.json")
    monkeypatch.setattr(duty, "SESSION_REFRESH_MARKER", fe / ".m")
    monkeypatch.setattr(duty.subprocess, "run", fake_run)
    logs = []
    monkeypatch.setattr(duty, "log", logs.append)
    monkeypatch.delenv("DUTY_SKIP_SESSION_REFRESH", raising=False)
    monkeypatch.delenv("DUTY_SKIP_SELF_LOGIN", raising=False)
    return duty, calls, logs


def test_duty_triggers_self_login_when_remember_me_dead(tmp_path, monkeypatch):
    duty, calls, logs = _duty_env(tmp_path, monkeypatch, refresh_rc=1)
    duty._maybe_proactive_session_refresh()
    assert any("self_login" in c for c in calls), calls
    assert any("self-login" in x for x in logs)
    assert not any("人机验证" in x for x in logs), "成功路径不该顺手吓人"


def test_duty_skips_self_login_on_success_or_vendor(tmp_path, monkeypatch):
    duty, calls, _ = _duty_env(tmp_path, monkeypatch, refresh_rc=0)
    duty._maybe_proactive_session_refresh()
    assert not any("self_login" in c for c in calls)


def test_duty_skips_without_creds_and_with_env(tmp_path, monkeypatch):
    duty, calls, _ = _duty_env(tmp_path, monkeypatch, refresh_rc=1, creds=False)
    duty._maybe_proactive_session_refresh()
    assert not any("self_login" in c for c in calls)
    duty2, calls2, _ = _duty_env(tmp_path, monkeypatch, refresh_rc=1)
    monkeypatch.setenv("DUTY_SKIP_SELF_LOGIN", "1")
    duty2._maybe_proactive_session_refresh()
    assert not any("self_login" in c for c in calls2), "总开关必须能关"


def test_clears_stale_token_before_submit():
    """登录通路必须自证拿的是**本次**提交的新 token，不能读 profile 残留旧 JWT
    （#116/#120 同款自欺坑）。goto 后必须 removeItem('token') + captured.clear()。"""
    src = (ROOT / "scripts" / "njstats_self_login.py").read_text(encoding="utf-8")
    pre = src.split('btn.click()')[0]
    assert "removeItem('token')" in pre, "提交前没清残留 token → 可能假成功"
    assert "captured.clear()" in pre, "提交前没清响应缓存 → 可能读到上一轮残留"


# ---- 图形码离线 OCR（2026-11-23 用户裁决解禁；护栏必须钉死） ----
def test_ocr_attempt_cap_is_two():
    """第 3 次试码就疑似暴力破解 → 封号/封 IP 会连累整条链的出口 IP。"""
    assert selflogin.MAX_OCR_ATTEMPTS == 2


def test_ocr_switch_off_env_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv("SELF_LOGIN_NO_OCR", "1")
    assert selflogin._solve_image_captcha(object()) is False  # 不碰 page


def test_ocr_attempts_counted_per_try_not_success():
    """尝试计数在调用求解前就 +1：识别失败也消耗额度，杜绝失败后无限重试。"""
    src = (ROOT / "scripts" / "njstats_self_login.py").read_text(encoding="utf-8")
    seg = src.split("if ocr_tries < MAX_OCR_ATTEMPTS")[1].split("time.sleep(2)")[0]
    assert "ocr_tries += 1" in seg.split("_solve_image_captcha")[0], \
        "必须先计数再求解——写在 if solve() 分支里就是失败不计数"
