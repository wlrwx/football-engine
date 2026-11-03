#!/usr/bin/env python3
"""大小号身份不变量（2026-11-03）。启动 superagent / 开工单之前跑，任一不满足就非零退出。

为什么需要它：git 的 `user.email` 只是署名，**真正决定身份的是远端用什么凭据**。
本仓库 duty 的 remote 是 SSH 别名 `git@github-football-engine:`（owner 的 key，
admin 权限）。如果 agent 在继承了这份 remote 的 clone/worktree 里干活，就会出现
最坏的一种串台：提交署名是小号，推送认证却是大号——它能直推 main、能合并、能改
保护规则，而 GitHub 审计里看起来像 owner 本人干的。

dsh-superagent 的部署文档建议「从某个本地 clone 加 worktree」，图省事指向 duty
那份 clone 就会踩中上面这条。所以这里把不变量写成断言：

  I1 小号只走 HTTPS + 自己的 fork（`https://github.com/<小号>/…`），且对上游
     `push/admin/maintain` 全为 false；
  I2 小号工作目录的署名不得是大号那套（`bot@football.engine` 等），否则它的提交
     会落进 duty 的 main-watch 基线里静默通过；
  I3 大号工作目录的 remote 必须仍是 SSH 别名——防止哪天被改成 HTTPS 后走小号的
     凭据助手；
  I4 环境里不得有 `GH_TOKEN/GITHUB_TOKEN`（它们会盖掉 gh 的配置目录身份）。

注：CI 闸门 `pr_guard.py` 认的是 PR 作者（认证身份），不是 commit 署名，所以
改 `user.email` 骗不过闸门；这里堵的是本地推送这条不走 PR 的路。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

EXPECTED_AGENT_LOGIN = "dykily"
UPSTREAM = "wlrwx/football-engine"
OWNER_REMOTE_PREFIX = "git@github-football-engine:"
OWNER_COMMIT_EMAILS = {
    "bot@football.engine",
    "wlrwx@users.noreply.github.com",
    "126323268+wlrwx@users.noreply.github.com",
}
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
ROOT = Path(__file__).resolve().parent.parent


def check_agent_login(login: str, expected: str = EXPECTED_AGENT_LOGIN) -> list[str]:
    if (login or "").strip().lower() != expected.lower():
        return [f"gh-agent 的身份是 {login!r}，应为 {expected!r}——凭据目录被别的账号登录了"]
    return []


def check_upstream_permissions(perms: dict) -> list[str]:
    bad = [k for k in ("push", "admin", "maintain", "triage") if perms.get(k)]
    if bad:
        return [f"小号对上游 {UPSTREAM} 竟然有 {'/'.join(bad)} 权限——"
                f"fork 交付方案要求上游只读，否则「只能提 PR」不成立"]
    return []


def check_agent_remotes(urls: list[str], login: str = EXPECTED_AGENT_LOGIN) -> list[str]:
    out: list[str] = []
    if not urls:
        return ["小号工作目录没有任何 remote，无法判断它会推到哪里"]
    want = f"https://github.com/{login.lower()}/"
    for url in urls:
        u = url.strip()
        if u.startswith("git@") or "github-football-engine" in u:
            out.append(f"小号 remote 是 SSH 别名 {u!r}——推送会认证成大号（admin），必须换成自己的 fork")
        elif not u.lower().startswith(want):
            out.append(f"小号 remote {u!r} 不是它自己的 fork（应为 {want}…）")
    return out


def check_agent_commit_email(email: str, login: str = EXPECTED_AGENT_LOGIN) -> list[str]:
    e = (email or "").strip().lower()
    if not e:
        return ["小号工作目录没有设 user.email，会继承大号的署名"]
    if e in OWNER_COMMIT_EMAILS:
        return [f"小号署名 {email!r} 是大号那套——它的提交会混进 duty 的 main-watch 基线"]
    if login.lower() not in e:
        return [f"小号署名 {email!r} 里不含 {login!r}，无法一眼认出是谁提交的"]
    return []


def check_token_env(env: dict) -> list[str]:
    hit = [k for k in TOKEN_ENV if env.get(k)]
    if hit:
        return [f"环境里有 {'/'.join(hit)}——它会盖掉 gh 配置目录里的身份"]
    return []


def check_owner_remote(url: str) -> list[str]:
    if not (url or "").startswith(OWNER_REMOTE_PREFIX):
        return [f"大号工作目录的 remote 变成了 {url!r}，应为 {OWNER_REMOTE_PREFIX}…（SSH 专用 key）"]
    return []


def _run(args: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    p = subprocess.run(args, cwd=str(cwd) if cwd else None, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", check=False, timeout=60)
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def collect(agent_dir: Path, owner_dir: Path, gh_config_dir: Path,
            expect_login: str = EXPECTED_AGENT_LOGIN, offline: bool = False) -> list[str]:
    bad: list[str] = []
    bad += check_token_env(dict(os.environ))
    bad += check_owner_remote(_run(["git", "remote", "get-url", "origin"], cwd=owner_dir))

    env = dict(os.environ, GH_CONFIG_DIR=str(gh_config_dir))
    for key in TOKEN_ENV:
        env.pop(key, None)
    if not offline:
        login = _run(["gh", "api", "user", "--jq", ".login"], env=env)
        bad += check_agent_login(login, expect_login)
        perms_raw = _run(["gh", "api", f"repos/{UPSTREAM}", "--jq", ".permissions"], env=env)
        if perms_raw:
            import json
            try:
                bad += check_upstream_permissions(json.loads(perms_raw))
            except ValueError:
                bad.append(f"读不到小号对上游的权限（gh 返回 {perms_raw[:60]!r}）")
        else:
            bad.append("gh-agent 查不到身份/权限——先确认它登录的是小号")

    if agent_dir.exists():
        urls = [u for u in _run(["git", "remote", "-v"], cwd=agent_dir).splitlines() if u]
        # `git remote -v` 每行形如 "origin\thttps://… (fetch)"，取 URL 段并去重
        parsed = []
        for line in urls:
            parts = line.split()
            if len(parts) >= 2:
                parsed.append(parts[1])
        bad += check_agent_remotes(sorted(set(parsed)), expect_login)
        bad += check_agent_commit_email(
            _run(["git", "config", "--local", "--get", "user.email"], cwd=agent_dir), expect_login)
    else:
        bad.append(f"小号工作目录 {agent_dir} 还不存在——先单独 clone 它自己的 fork，"
                   f"不要复用 {owner_dir}（worktree 会继承大号的 SSH remote）")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="大小号身份不变量检查")
    ap.add_argument("--agent-dir", default=str(Path.home() / "agentbox" / "football-engine"))
    ap.add_argument("--owner-dir", default=str(ROOT))
    ap.add_argument("--gh-config-dir", default=str(Path.home() / ".config" / "gh-agent"))
    ap.add_argument("--expect-login", default=EXPECTED_AGENT_LOGIN)
    ap.add_argument("--offline", action="store_true", help="只查本地，不访问 GitHub")
    args = ap.parse_args(argv)

    bad = collect(Path(args.agent_dir), Path(args.owner_dir), Path(args.gh_config_dir),
                  args.expect_login, args.offline)
    if not bad:
        print(f"身份不变量：通过（小号={args.expect_login}，大号走 SSH 别名，上游只读）")
        return 0
    print("身份不变量：不通过")
    for item in bad:
        print(f"  · {item}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
