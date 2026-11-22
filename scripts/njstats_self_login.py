#!/usr/bin/env python3
"""自助重登（2026-11-22 用户裁决：登录态死了不再叫人手工会话）。

流程 = 当晚人工实测通的路径：无头开 /login → 点「密码登录」切模式 →
填邮箱+密码 → 点提交 → 从 localStorage 或 /api/user 响应体截 JWT → 走
seed 同款 _save（必存 JWT、有 SESSION 也存、健康置 ok 清红线）。

凭据读 `~/.football-engine/njstats_account`（第一行邮箱、第二行密码，0600，
**永不进仓库/日志/对话**）。

红线：图形码按 2026-11-23 用户裁决解禁——**离线 ddddocr 识别、上限 2 次、
失败立刻退 21 叫人工**（反复试码会触发风控，封号/封 IP 连累整条链的出口）；
图片与凭据不出本机，不接任何第三方打码服务。滑块/点选类人机验证仍然**绝不尝试**。
退出码：0 成功；1 一般失败（网络/按钮找不到）；21 需要人工（OCR 两次未过或非图形码）。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
CREDS_FILE = HOME / ".football-engine" / "njstats_account"
BASE = "https://www.njstats.cn"
TOKEN_WAIT_S = 90

_spec = importlib.util.spec_from_file_location(
    "njstats_seed_login", ROOT / "scripts" / "njstats_seed_login.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def read_creds() -> tuple[str, str] | None:
    """凭据文件形状校验；任何异常都当「没有凭据」，绝不外抛（不把内容带进日志）。"""
    try:
        lines = CREDS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2 or "@" not in lines[0] or not lines[1].strip():
        return None
    return lines[0].strip(), lines[1].strip()


def _looks_like_captcha(visible_text: str) -> bool:
    """**可见文本**里是否有人机验证提示（用于超时后的失败分类）。

    两层教训：
    1) 「邮箱验证码/验证码登录」是模式切换标签，不是人机验证——只认硬特征词；
    2) 2026-11-23 演练实锤：**不能拿全量渲染 HTML 判定**——手机号
       登录 tab 的隐藏 DOM 里带图形验证码控件，正常密码页会被误判成 captcha。
       因此本函数只喂 `inner_text('body')`（渲染后可见文本）。
    """
    return bool(re.search(r"滑块|拖动|拼图|人机验证|图形验证|captcha|geetest|recaptcha|hcaptcha",
                          visible_text, re.I))


# 试码硬上限：第 3 次起疑似暴力破解，会招风控封号/封 IP；采集与下单共用同一
# 出口，赌不起。测死在 tests/test_njstats_self_login.py。
MAX_OCR_ATTEMPTS = 2


def _solve_image_captcha(page) -> bool:
    """图形码求解：img[alt=验证码]（实测为 base64 PNG）→ 离线 OCR → 填输入框。

    由调用方在填好后重新点提交。上一轮填过还停在验证码页 = 认错，先点图刷新。
    `SELF_LOGIN_NO_OCR=1` 或 ddddocr 缺失（CI）→ 直接放弃，走叫人兜底。
    """
    if os.environ.get("SELF_LOGIN_NO_OCR") == "1":
        return False
    try:
        import ddddocr
    except ImportError:
        print("ddddocr 未安装：不解图形码，交人工")
        return False
    try:
        img = page.locator("img[alt='验证码']").first
        box = page.locator("input[placeholder*='图形验证码']").first
        if not img.count() or not box.count():
            return False
        if (box.input_value() or "").strip():          # 上一发码错了：点图换一张
            img.click()
            time.sleep(1.2)
        code = (ddddocr.DdddOcr(show_ad=False).classification(img.screenshot()) or "").strip()
        if not code:
            return False
        box.fill(code)
        print(f"图形码已离线识别填入（{len(code)} 位，内容不落日志）")
        return True
    except Exception:  # noqa: BLE001  识别链路任何异常都不升级成风险
        return False


def main() -> int:
    creds = read_creds()
    if not creds:
        print("无可用凭据文件（njstats_account），跳过自助重登")
        return 1
    email, pwd = creds
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright 未安装（CI 环境），跳过自助重登")
        return 1
    with sync_playwright() as pw:
        # 2026-11-23 真机对比实锤：**无头模式下点「登录」发不出登录 POST**（疑似被
        # bot 检测吞掉），有头才走通。默认有头——触发频率是 12h/6h 级，窗口闪几秒
        # 可接受；无显示器/launchd 无 GUI 会话时启动会抛错 → 退 1 叫人工，安全兜底。
        # `SELF_LOGIN_HEADLESS=1` 可切回无头（仅调试用）。
        headless = os.environ.get("SELF_LOGIN_HEADLESS") == "1"
        ctx = pw.chromium.launch_persistent_context(
            str(seed.PROFILE), headless=headless, args=["--no-proxy-server"],
            viewport={"width": 1200, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        captured: dict = {}

        def on_resp(resp):
            try:
                if not captured.get("tok") and "/api/user/" in resp.url and resp.status == 200:
                    tok = seed._jwt_from_body(resp.text())
                    if tok:
                        captured["tok"] = tok
            except Exception:  # noqa: BLE001
                pass
        page.on("response", on_resp)
        try:
            page.goto(BASE + "/login", wait_until="domcontentloaded", timeout=45000)
            # 关键：先清掉残留 token——否则 profile 里上一次登录的旧 JWT 会被
            # _read_token 直接读到，「假成功」自欺（#116/#120 同款坑，验证登录
            # 通路必须确认拿到的是**本次**提交产物）。清完再等，任何读到的都只能是新 token。
            try:
                page.evaluate("() => localStorage.removeItem('token')")
            except Exception:  # noqa: BLE001
                pass
            captured.clear()
            time.sleep(2)
            switch = page.get_by_text("密码登录", exact=False).first
            if switch.count():
                switch.click()
                time.sleep(1)
            page.locator("input[type='email']").first.fill(email)
            page.locator("input[type='password']").first.fill(pwd)
            # 提交按钮文案跟模式走（2026-11-23 实测定案）：邮箱验证码模式=「登录 / 注册」，
            # **密码模式=「登录」**——只找前者永远 count=0。exact 匹配防「密码登录/验证码登录」
            # 标签误点；两种都试，兼容停在任一模式渲染的情况。
            btn = page.get_by_role("button", name="登录", exact=True).first
            for _ in range(20):
                if btn.count():
                    break
                btn = page.get_by_role("button", name="登录 / 注册", exact=True).first
                time.sleep(1)
            if not btn.count():
                print("找不到提交按钮，登录页形状变了（退出 1）")
                return 1
            btn.click()
            deadline = time.time() + TOKEN_WAIT_S
            ocr_tries = 0
            while time.time() < deadline:
                tok = captured.get("tok") or seed._read_token(page)
                if tok and str(tok).startswith("eyJ"):
                    has_sess = seed._save(ctx, str(tok))
                    print(f"✅ 自助重登成功（JWT {len(str(tok))} 字符已存；"
                          f"SESSION {'已存' if has_sess else '站点未给，duty 用 JWT 即可'}）")
                    return 0
                # 图形码行出现且没超试码上限：离线识别→重提交（用户裁决 2026-11-23）。
                # 计数按「尝试」不按「成功」——识别失败也算一次，杜绝无限重试招风控。
                if ocr_tries < MAX_OCR_ATTEMPTS:
                    try:
                        cap = page.locator("input[placeholder*='图形验证码']").first
                        if cap.count() and cap.is_visible():
                            ocr_tries += 1
                            if _solve_image_captcha(page):
                                btn.click()
                                time.sleep(2)
                            else:
                                time.sleep(2)
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(2)
            # 超时才分类：可见文本有人机验证特征 → 21（叫人）；否则 1（网络/凭证问题，下轮重试）
            visible = ""
            try:
                visible = page.inner_text("body")
            except Exception:  # noqa: BLE001
                pass
            if _looks_like_captcha(visible):
                print("提交后超时，且可见文本含人机验证特征——我不自动解，交人处理（退出 21）")
                return 21
            print("提交后超时未拿到 token，页面无验证码特征（凭证/网络问题，退出 1，下轮重试）")
            return 1
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
