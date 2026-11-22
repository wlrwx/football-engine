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


def _looks_like_captcha(visible_text: str) -> bool:
    """**可见文本**里是否有真人机验证提示。只识别，绝不尝试通过。

    两层教训：
    1) 「邮箱验证码/验证码登录」是模式切换标签，不是人机验证——只认硬特征词；
    2) 2026-11-23 演练实锤：**不能拿 page.content() 全量 HTML 判定**——手机号
       登录 tab 的隐藏 DOM 里带图形验证码控件，正常密码页会被误判成 captcha。
       因此本函数只喂 `inner_text('body')`（渲染后可见文本），且只在
       「提交后拿不到 token」时用来区分 21/1，**绝不作为提交前的中止条件**。
    """
    return bool(re.search(r"滑块|拖动|拼图|人机验证|图形验证|captcha|geetest|recaptcha|hcaptcha",
                          visible_text, re.I))


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
