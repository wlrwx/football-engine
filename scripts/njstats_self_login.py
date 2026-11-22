#!/usr/bin/env python3
"""自助重登 v2：DrissionPage 驱动**真实 Chrome**（2026-11-23 用户点破重写）。

为什么换枪：playwright 无头点击「登录」连 POST 都发不出（bot 检测），有头也多次
空转；用户手开浏览器秒登——识别的是工具不是流程。DrissionPage 接管本机真实
Chrome（带自有 user-data-dir + 独立调试端口），指纹即真人。

流程：/login → 点「密码登录」→ **先清残留 token（#121 纪律：验证必须是本次产物）**
→ 填邮箱密码 → 起监听点提交 → 双通道取 JWT（localStorage + 网关响应体 eyJ）
→ 存盘（同 seed 语义：JWT 必存、SESSION 有则存、health 置 ok 清红线）。

护栏（全部测死）：
- 图形码：离线 ddddocr（图片不出本机），**上限 2 次且计数在求解前**；再失败退 21 叫人。
- 明确「密码错误」即刻退 1，绝不连发重登（防风控锁定；节奏由 duty 的 12h/6h 闸控制）。
- 滑块/点选类仍绝不尝试。`SELF_LOGIN_HEADLESS=1` 切无头（仅调试）；
  `SELF_LOGIN_NO_OCR=1` 关过码；`SELF_LOGIN_DP_PORT` 换调试端口（默认 9666）。
退出码：0 成功；1 一般失败/凭证被拒/环境缺失；21 需人工（验证码类）。
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
FE = HOME / ".football-engine"
CREDS_FILE = FE / "njstats_account"
JWT_FILE = FE / "njstats_jwt"
SESSION_FILE = FE / "njstats_session.json"
HEALTH_FILE = FE / "njstats_session_health.json"
DP_PROFILE = FE / "dpprofile"
BASE = "https://www.njstats.cn"
TOKEN_WAIT_S = 90
# 试码硬上限：第 3 次疑似暴破 → 风控封号/封 IP，会连累共用出口的采集下单链。
MAX_OCR_ATTEMPTS = 2

JWT_BYTES_RE = re.compile(rb"eyJ[A-Za-z0-9_.\-]{40,}")


def read_creds() -> tuple[str, str] | None:
    """凭据文件形状校验；任何异常都当「没有凭据」，绝不外抛内容。"""
    try:
        lines = CREDS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2 or "@" not in lines[0] or not lines[1].strip():
        return None
    return lines[0].strip(), lines[1].strip()


def _looks_like_captcha(visible_text: str) -> bool:
    """可见文本里是否有人机验证提示（用于超时后的失败分类）。
    只认硬特征词——「邮箱验证码/验证码登录」是模式标签，不算（历史假阳性教训）。"""
    return bool(re.search(r"滑块|拖动|拼图|人机验证|图形验证|captcha|geetest|recaptcha|hcaptcha",
                          visible_text, re.I))


def _jwt_in_obj(obj) -> str | None:
    """在网关响应（bytes/dict/list 混合）里递归找 eyJ… JWT。"""
    if isinstance(obj, bytes):
        m = JWT_BYTES_RE.search(obj)
        return m.group(0).decode("ascii", "ignore") if m else None
    if isinstance(obj, str):
        m = re.search(r"eyJ[A-Za-z0-9_.\-]{40,}", obj)
        return m.group(0) if m else None
    if isinstance(obj, dict):
        for v in obj.values():
            tok = _jwt_in_obj(v)
            if tok:
                return tok
    if isinstance(obj, (list, tuple)):
        for v in obj:
            tok = _jwt_in_obj(v)
            if tok:
                return tok
    return None


def _save_dp(tok: str, cookies) -> bool:
    """与 seed._save 同语义：JWT 必存(600)、SESSION 有则存、health 置 ok 清红线。"""
    JWT_FILE.write_text(tok.strip(), encoding="utf-8")
    JWT_FILE.chmod(0o600)
    sess = None
    for c in cookies or []:
        if c.get("name") == "SESSION" and str(c.get("domain", "")).endswith("njstats.cn"):
            sess = c.get("value")
    if sess:
        SESSION_FILE.write_text(
            '{"SESSION": "' + sess + '", "seeded_at": "' + time.strftime("%F %T") + '"}',
            encoding="utf-8")
        SESSION_FILE.chmod(0o600)
    try:
        HEALTH_FILE.write_text(
            '{"result": "ok", "at": "' + time.strftime("%Y-%m-%d %H:%M:%S") + '", '
            '"detail": "自助重登(DrissionPage)"}', encoding="utf-8")
        HEALTH_FILE.chmod(0o600)
    except OSError:
        pass
    return bool(sess)


def _login_button(tab):
    """按钮按精确文本「登录」查找（同页有 密码登录/验证码登录 标签，模糊会点错）。"""
    for b0 in tab.eles("tag:button"):
        if (b0.text or "").strip() == "登录":
            return b0
    return None


def _inputs_by_type(tab) -> dict:
    """遍历 tag:input 按 attr('type') 归组（真机探针验证的稳定路径；
    CSS 属性选择器在 DP4 locators 里行为不稳，不赌）。"""
    out = {}
    for e in tab.eles("tag:input"):
        t = (e.attr("type") or "text").lower()
        out.setdefault(t, e)
    return out


def _try_solve_captcha(tab) -> bool:
    """图形码：截图 → 离线 ddddocr → 填入。任何异常/缺库 → False 走叫人兜底。"""
    if os.environ.get("SELF_LOGIN_NO_OCR") == "1":
        return False
    try:
        import ddddocr
        img = box = None
        for e in tab.eles("tag:img"):
            if (e.attr("alt") or "") == "验证码":
                img = e
                break
        for e in tab.eles("tag:input"):
            if "图形验证码" in (e.attr("placeholder") or ""):
                box = e
                break
        if not img or not box:
            return False
        if (box.attr("value") or "").strip():        # 上一发错了：点图换一张
            img.click()
            time.sleep(1.2)
        png = img.get_screenshot(as_bytes="png")
        code = (ddddocr.DdddOcr(show_ad=False).classification(png) or "").strip()
        if not code:
            return False
        box.clear()
        box.input(code)
        print(f"图形码已离线识别填入（{len(code)} 位，内容不落日志）")
        return True
    except Exception:  # noqa: BLE001 识别链路异常一律不升级成风险
        return False


def main() -> int:
    creds = read_creds()
    if not creds:
        print("无可用凭据文件（njstats_account），跳过自助重登")
        return 1
    email, pwd = creds
    try:
        from DrissionPage import Chromium, ChromiumOptions
    except ImportError:
        print("DrissionPage 未安装（CI 环境），跳过自助重登")
        return 1
    headless = os.environ.get("SELF_LOGIN_HEADLESS") == "1"
    # 每次跑前重置 DP 临时 profile（纯缓冲，真状态在 njstats_jwt/session 文件）：
    # 残留 cookie 会让 /login 直接跳已登录首页 → 页面上根本没有表单（2026-11-23 实锤）。
    import shutil
    shutil.rmtree(DP_PROFILE, ignore_errors=True)
    DP_PROFILE.mkdir(parents=True, exist_ok=True)
    co = ChromiumOptions()
    co.set_user_data_path(str(DP_PROFILE))
    co.set_local_port(int(os.environ.get("SELF_LOGIN_DP_PORT", "9666")))
    co.set_argument("--no-proxy-server")
    if headless:
        co.headless(True)
    browser = None
    try:
        browser = Chromium(co)
        tab = browser.latest_tab
        tab.get(BASE + "/login")
        time.sleep(2)
        # 先清残留：之后读到的 token 只能是本次登录产物（#121 纪律，防假成功）
        try:
            tab.run_js("try{localStorage.removeItem('token')}catch(e){}")
        except Exception:  # noqa: BLE001
            pass
        # 切到密码模式并轮询等表单水合（一次性 sleep 常赶在渲染前，导致停在
        # 邮箱码模式、没有 password 框——2026-11-23 定位失败的真因）。
        mail = pwbox = btn = None
        for _attempt in range(8):
            sw = tab.ele("text:密码登录", timeout=4)
            if sw:
                try:
                    sw.click()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1)
            ins = _inputs_by_type(tab)
            mail = ins.get("email")
            pwbox = ins.get("password")
            btn = _login_button(tab)
            if mail and pwbox and btn:
                break
        if not (mail and pwbox and btn):
            print(f"登录页形状异常（邮箱={bool(mail)} 密码={bool(pwbox)} 登录钮={bool(btn)}）")
            return 1
        mail.clear().input(email)
        pwbox.clear().input(pwd)
        captured = {"tok": None}
        try:
            tab.listen.start("njserver/openApi")
        except Exception:  # noqa: BLE001 监听失败不致命，localStorage 通道仍在
            pass
        btn.click()
        deadline = time.time() + TOKEN_WAIT_S
        ocr_tries = 0
        while time.time() < deadline:
            if not captured["tok"]:
                try:
                    pkt = tab.listen.wait(timeout=2)
                    if pkt is not None:
                        captured["tok"] = _jwt_in_obj(pkt.response.body)
                except Exception:  # noqa: BLE001
                    pass
            tok = captured["tok"]
            if not tok:
                try:
                    ls = tab.run_js("try{return localStorage.getItem('token')}catch(e){return null}")
                    tok = ls if ls and str(ls).startswith("eyJ") else None
                except Exception:  # noqa: BLE001 提交后跳空白页时 localStorage 会抛异常
                    tok = None
            if tok:
                has_sess = _save_dp(str(tok), tab.cookies())
                print(f"✅ 自助重登成功（JWT {len(str(tok))} 字符已存；"
                      f"SESSION {'已存' if has_sess else '站点未给，duty 用 JWT 即可'}）")
                return 0
            try:
                body_txt = tab.ele("tag:body").text or ""
            except Exception:  # noqa: BLE001
                body_txt = ""
            if "密码错误" in body_txt:
                print("站点回「密码错误」——不连发重登，冷却下轮再试（退出 1）")
                return 1
            if ocr_tries < MAX_OCR_ATTEMPTS and "图形验证码" in body_txt:
                ocr_tries += 1                        # 尝试即计数：识别失败也消耗额度
                if _try_solve_captcha(tab):
                    btn = _login_button(tab) or btn
                    btn.click()
                time.sleep(2)
                continue
            time.sleep(2)
        try:
            final = (tab.ele("tag:body").text or "")[:400]
        except Exception:  # noqa: BLE001
            final = ""
        if _looks_like_captcha(final):
            print("超时且仍卡在人机验证（已用尽 2 次 OCR 额度或滑块类）→ 交人工（退出 21）")
            return 21
        print("超时未拿到 token，页面无验证码特征（网络/风控观察期，退出 1，下轮再试）")
        return 1
    except Exception as exc:  # noqa: BLE001 主链任何异常都不裸奔重试
        print(f"自助重登异常: {type(exc).__name__}（退出 1，不连发）")
        return 1
    finally:
        try:
            if browser is not None:
                browser.quit()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
