#!/usr/bin/env python3
"""无头续期 NJStats 登录态——在过期前主动换新，不需要人工过验证码。

原理：seed_login 用的持久 profile（~/.football-engine/pwprofile）里除会话级 SESSION
外还存着长效 remember-me cookie。无头启动 chromium，站点会用它静默换发新 SESSION/JWT，
抓下来写回即可，全程不出现验证码、不弹窗。remember-me 过期（约 14 天）才需人工播种。

2026-11-09 改（配合 duty 的主动续期）：
  · **5xx 与「登录态死了」必须分开**：厂商网关故障时带不带 cookie 都 502（11-04→07
    实测），旧版把 502 判成「登录态已失效」，会喊人白跑、还会拿空结果覆盖能用的登录态。
    现在 5xx / 网络错误 = `vendor`（不覆盖、不喊人），只有 401/403 这类才 `auth_dead`。
  · 结论写 `~/.football-engine/njstats_session_health.json`，ops_digest 据此出
    「登录态将失效」的**提前**红线（缓存 JWT 还有约 20h，人可以从容播种，而不是等断了才救）。
  · 200 时顺带把最新 JWT 也写回 njstats_jwt（站点只给 JWT 不给 SESSION 时也能续）。

用法：
    .venv/bin/python3 scripts/njstats_refresh_session.py          # 续期并写文件
    .venv/bin/python3 scripts/njstats_refresh_session.py --check  # 只探测不写（连 health 也不写）

退出码：0 已续期；1 登录态真的死了（需人工播种）；2 厂商 5xx/网络不可判定（不覆盖、不喊人）。
"""
from __future__ import annotations

import argparse
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

RC_OK, RC_DEAD, RC_VENDOR = 0, 1, 2


def classify_status(st: int) -> str:
    """把 /api/user/current 的状态码翻译成「能不能续」。

    5xx 与 0/负数（请求根本没发出去）都算 vendor：**不可判定**，不是登录态死亡。
    把它们当归登录死亡会喊人白跑一趟，还会覆盖掉本来能用的登录态。
    """
    if st == 200:
        return "ok"
    if st <= 0 or 500 <= st < 600:
        return "vendor"
    return "auth_dead"


def _record(result: str, detail: str) -> None:
    try:
        HEALTH.write_text(json.dumps(
            {"result": result, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "detail": detail}, ensure_ascii=False), encoding="utf-8")
        HEALTH.chmod(0o600)
    except OSError:
        pass


def _probe(page) -> tuple[int, str | None]:
    """一次取齐：登录态状态码 + 最新 JWT（接口不给就退回 localStorage）。"""
    try:
        res = page.evaluate("""async () => {
          try {
            const r = await fetch('/api/user/current', {credentials:'include', cache:'no-store'});
            let token = null;
            if (r.status === 200) { const d = await r.json(); token = d?.data?.token || null; }
            return {status: r.status, token};
          } catch (e) { return {status: -1, token: null}; }
        }""")
    except Exception:
        return -1, None
    status = int((res or {}).get("status", -1))
    token = (res or {}).get("token")
    if not token:
        try:
            token = page.evaluate("() => localStorage.getItem('token')")
        except Exception:
            token = None
    return status, token


def apply_probe(status: int, token: str | None, cookies: dict, check_only: bool = False) -> int:
    """按探测结果决定写什么、返回哪个码。纯决策 + 落盘，可单测（不碰浏览器）。"""
    kind = classify_status(status)
    if kind == "vendor":
        msg = f"厂商 /api/user/current HTTP {status}（5xx/网络，与登录态无关），本次不续不覆盖"
        print(msg)
        if not check_only:
            _record("vendor", msg)
        return RC_VENDOR
    if kind == "auth_dead":
        msg = f"登录态已失效（HTTP {status}），需人工播种（跑 scripts/njstats_seed_login.py）"
        print(msg)
        if not check_only:
            _record("auth_dead", msg)
        return RC_DEAD
    if check_only:
        print(f"可无头续期（SESSION {'有' if 'SESSION' in cookies else '无'}，"
              f"JWT {'有' if token else '无'}）")
        return RC_OK
    saved = []
    if "SESSION" in cookies:
        OUT.write_text(json.dumps({"SESSION": cookies["SESSION"],
                                   "seeded_at": time.strftime("%F %T")}), encoding="utf-8")
        OUT.chmod(0o600)
        saved.append("SESSION")
    if token:
        JWT_FILE.write_text(token.strip(), encoding="utf-8")
        JWT_FILE.chmod(0o600)
        saved.append("JWT")
    if not saved:
        msg = "站点返回 200 但既没给 SESSION 也没给 token（登录态无法落盘），需人工播种"
        print(msg)
        _record("auth_dead", msg)
        return RC_DEAD
    msg = "已续期：" + "、".join(saved)
    print(msg)
    _record("ok", msg)
    return RC_OK


def refresh(check_only: bool = False) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright，无法无头续期（需修环境后重试）")
        return RC_DEAD
    if not PROFILE.exists():
        print(f"无持久 profile {PROFILE}，需人工播种（跑 scripts/njstats_seed_login.py）")
        return RC_DEAD

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, viewport={"width": 1100, "height": 800},
            # 站点在国内、直连实测可用；不吃系统代理，Clash 死不能把续期也停掉
            args=["--no-proxy-server"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(BASE + "/", wait_until="domcontentloaded", timeout=45000)
            status, token = _probe(page)
            cookies = {c["name"]: c["value"] for c in ctx.cookies()
                       if c.get("domain", "").endswith("njstats.cn")}
        finally:
            ctx.close()
    return apply_probe(status, token, cookies, check_only=check_only)


def main() -> int:
    ap = argparse.ArgumentParser(description="无头续期 NJStats 登录态（SESSION + JWT）")
    ap.add_argument("--check", action="store_true", help="只检测是否可续期，不写文件")
    return refresh(ap.parse_args().check)


if __name__ == "__main__":
    sys.exit(main())
