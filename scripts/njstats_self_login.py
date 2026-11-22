#!/usr/bin/env python3
"""自助重登（2026-11-22 用户裁决：登录态死了不再叫人手工会话）。

流程 = 当晚人工实测通的路径：无头开 /login → 点「密码登录」切模式 →
填邮箱+密码 → 点提交 → 从 localStorage 或 /api/user 响应体截 JWT → 走
seed 同款 _save（必存 JWT、有 SESSION 也存、健康置 ok 清红线）。

凭据读 `~/.football-engine/njstats_account`（第一行邮箱、第二行密码，0600，
**永不进仓库/日志/对话**）。

红线：**绝不自动解验证码**——页面上出现人机验证元素、或提交后 90s 拿不到
token，即退出码 21，由 duty.log/摘要红线叫人（这是唯一允许打扰人的形状）。
退出码：0 成功；1 一般失败（网络/按钮找不到）；21 需要人工（验证码）。
"""
from __future__ import annotations

import importlib.util
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


def _looks_like_captcha(snapshot: str) -> bool:
    """页面是否存在**真人机会验证**元素。只识别，绝不尝试通过。

    教训（写测试时抓到的假阳性）：登录页本身带「邮箱验证码/验证码登录」字样
    （模式切换标签），拿「验证码」三字当特征会把正常密码页误判成captcha。
    只认硬特征：图形/滑块/拼图/人机 等控件名与三方验证服务域名。
    """
    return bool(re.search(r"滑块|拖动|拼图|人机验证|图形|captcha|geetest|recaptcha|hcaptcha",
                          snapshot, re.I))


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
        ctx = pw.chromium.launch_persistent_context(
            str(seed.PROFILE), headless=True, args=["--no-proxy-server"],
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
            time.sleep(2)
            switch = page.get_by_text("密码登录", exact=False).first
            if switch.count():
                switch.click()
                time.sleep(1)
            page.locator("input[type='email']").first.fill(email)
            page.locator("input[type='password']").first.fill(pwd)
            snap = ""
            try:
                snap = page.content()
            except Exception:  # noqa: BLE001
                pass
            if _looks_like_captcha(snap):
                print("检测到人机验证元素——我不自动解，交人处理（退出 21）")
                return 21
            btn = page.get_by_text("登录 / 注册", exact=False).first
            if not btn.count():
                print("找不到提交按钮，登录页形状变了（退出 1）")
                return 1
            btn.click()
            deadline = time.time() + TOKEN_WAIT_S
            while time.time() < deadline:
                tok = captured.get("tok") or seed._read_token(page)
                if tok and str(tok).startswith("eyJ"):
                    has_sess = seed._save(ctx, str(tok))
                    print(f"✅ 自助重登成功（JWT {len(str(tok))} 字符已存；"
                          f"SESSION {'已存' if has_sess else '站点未给，duty 用 JWT 即可'}）")
                    return 0
                time.sleep(2)
            print("提交后超时未拿到 token（可能被验证码拦或凭证错误，不猜测）")
            return 21
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
