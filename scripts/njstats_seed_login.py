#!/usr/bin/env python3
"""一次性播种登录：弹出真浏览器窗口 → 用户手工登录 njstats.cn → 保存登录态。

保存两样东西：
  · JWT（localStorage 的 token）→ ~/.football-engine/njstats_jwt，duty 下单/取数直接用；
  · SESSION cookie（若站点给了）→ ~/.football-engine/njstats_session.json，供无头 mint 续期用。

2026-11-09 修：旧版只靠 `/api/user/current` 判成功、且只存 SESSION。厂商那个端点
5xx 时（11-04→07、11-09 都挂过），用户明明登录成功、token 已写进 localStorage，
脚本却永远判不了成功、又因抓不到 SESSION 一直空转到超时——表现为「登录后跳空白页、
没返回成功」。现在优先读 localStorage 的 token（登录成功前端必写，不依赖那个端点），
拿到就存 JWT；SESSION 有则一并存、没有也不阻塞。

用法：./.venv/bin/python3 scripts/njstats_seed_login.py   （5 分钟窗口内完成登录）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HOME = Path.home()
PROFILE = HOME / ".football-engine" / "pwprofile"
OUT = HOME / ".football-engine" / "njstats_session.json"
JWT_FILE = HOME / ".football-engine" / "njstats_jwt"
HEALTH = HOME / ".football-engine" / "njstats_session_health.json"
BASE = "https://www.njstats.cn"


def _read_token(page) -> str | None:
    """先读 localStorage（不依赖厂商端点），读不到再退回 /api/user/current。"""
    try:
        tok = page.evaluate("() => localStorage.getItem('token')")
        if tok:
            return tok
    except Exception:
        pass
    try:
        return page.evaluate("""async () => {
          const r = await fetch('/api/user/current',{credentials:'include',cache:'no-store'});
          if (r.status !== 200) return null;
          const d = await r.json(); return d?.data?.token || null;
        }""")
    except Exception:
        return None


def _save(ctx, tok: str) -> bool:
    """存 JWT（必存）+ SESSION（有则存），并把健康状态置回 ok。返回是否拿到了 SESSION。"""
    JWT_FILE.write_text(tok.strip(), encoding="utf-8")
    JWT_FILE.chmod(0o600)
    cookies = {c["name"]: c["value"] for c in ctx.cookies([BASE])
               if c["domain"].endswith("njstats.cn")}
    # 播种成功 = 登录态全新，把摘要的「登录态将失效」预警立刻清掉；
    # 不等下一次无头续期才恢复，否则手工播种后红线还会挂着误导人。
    try:
        HEALTH.write_text(json.dumps(
            {"result": "ok", "at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "detail": "人工播种"}, ensure_ascii=False), encoding="utf-8")
        HEALTH.chmod(0o600)
    except OSError:
        pass
    if cookies.get("SESSION"):
        OUT.write_text(json.dumps({"SESSION": cookies["SESSION"],
                                   "seeded_at": time.strftime("%F %T")}), encoding="utf-8")
        OUT.chmod(0o600)
        return True
    return False


def main() -> int:
    # playwright 只在本机播种时用，CI 不装它；延迟导入，_read_token/_save 才能被 CI 真跑。
    from playwright.sync_api import sync_playwright

    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE), headless=False, viewport={"width": 1100, "height": 800},
                                                   args=["--no-proxy-server"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/login", wait_until="domcontentloaded")
        # 清掉残留 token：之后凡是读到 token，必然是这次新登录写进去的，
        # 不会把上一轮的旧 token 误当成「已登录」。
        try:
            page.evaluate("() => localStorage.removeItem('token')")
        except Exception:
            pass
        print("浏览器已弹出：请完成登录（邮箱+密码+验证码），成功后本窗口自动关闭并保存（最长 5 分钟）")
        deadline = time.time() + 300
        while time.time() < deadline:
            tok = _read_token(page)
            if tok:
                has_session = _save(ctx, tok)
                print("✅ JWT 已保存 →", JWT_FILE)
                if has_session:
                    print("✅ SESSION cookie 已保存 →", OUT)
                else:
                    print("ℹ 站点未给 SESSION cookie（不影响，duty 直接用 JWT）")
                ctx.close()
                return 0
            time.sleep(2)
        print("超时未完成登录，未保存")
        ctx.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
