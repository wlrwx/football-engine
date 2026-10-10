"""把闭环产出推到 GitHub 分支并开 PR。

这是「全自动」的最后一块：闭环在 GitHub Actions 上跑完，
把通过验证的自动修复推到 origin 分支，并开 PR 让主线 agent 裁决。

设计约束：
  1. 只推、只开 PR，**绝不自动合入** main —— 合入是决策，不是自动化。
  2. 每个补丁一个分支、一个 PR，失败不阻塞其他补丁。
  3. 用了 GITHUB_TOKEN（= GH_TOKEN 环境变量），gh CLI 在 Actions 预装。
  4. 本地无 gh 时优雅降级：只打印将要执行的命令，不报错。

用法（Actions 内）：
    python evolution/scripts/push_and_pr.py --out evolution/out --repo .
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, capture_output=True, text=True)


def _gh_available() -> bool:
    try:
        r = subprocess.run(["gh", "--version"], capture_output=True, text=True)
        return r.returncode == 0
    except FileNotFoundError:
        # 本地/无 gh 环境：闭环降级为 dry-run，不报错
        return False


def collect_autofix(out_dir: Path) -> list[dict]:
    """读取闭环落盘的 autofix_*.json，返回补丁元数据列表。"""
    metas = []
    for p in sorted(out_dir.glob("autofix_*.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if m.get("@type") == "autofix":
                metas.append(m)
        except Exception as ex:  # noqa: BLE001
            print(f"[skip] 无法解析 {p.name}: {ex}")
    return metas


def push_and_pr(repo_dir: Path, metas: list[dict], dry_run: bool) -> list[str]:
    """把每个补丁推到 origin 分支并开 PR，返回已创建的 PR URL 列表。"""
    created = []
    for m in metas:
        branch = m.get("branch", "")
        issue = m.get("issue", "?")
        patch = m.get("patch_file", "")
        verify = m.get("verify", "")
        if not branch:
            print(f"[skip] {issue}: 无分支信息")
            continue

        body = (
            f"## 自动进化闭环修复：{issue}\n\n"
            f"**补丁**: `{patch}`\n\n"
            f"**验证**: {verify}\n\n"
            f"**提交**: `{m.get('commit', '?')[:8]}`\n\n"
            f"> 本 PR 由 self-evolving loop 自动提出并验证，"
            f"**未自动合入**。请主线 agent 裁决。\n"
            f"> 回滚：关闭本 PR 并 `git branch -D {branch}` 即可。\n"
        )

        if dry_run:
            print(f"[dry-run] 将推送 {branch} 并开 PR（issue={issue}）")
            created.append(f"(dry-run) {branch}")
            continue

        # 1. 应用补丁到当前工作副本（若脚本从 autofix 复现）
        patch_path = Path(patch) if patch and Path(patch).exists() else None
        if patch_path:
            r = subprocess.run(["git", "apply", str(patch_path)],
                               cwd=repo_dir, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[fail] {issue}: git apply 失败: {r.stderr[:200]}")
                continue

        # 2. 推分支
        r = subprocess.run(["git", "push", "origin", branch],
                           cwd=repo_dir, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[fail] {issue}: push 失败: {r.stderr[:200]}")
            continue

        # 3. 开 PR
        r = _gh(["pr", "create", "--base", "main", "--head", branch,
                 "--title", f"fix({issue}): 自动进化闭环修复",
                 "--body", body])
        if r.returncode != 0:
            print(f"[fail] {issue}: gh pr create 失败: {r.stderr[:200]}")
            continue
        url = (r.stdout or "").strip()
        print(f"[pr] {issue}: {url}")
        created.append(url)

    return created


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="闭环输出目录")
    ap.add_argument("--repo", required=True, help="git 仓库路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    a = ap.parse_args()

    out_dir = Path(a.out)
    repo_dir = Path(a.repo)
    metas = collect_autofix(out_dir)
    print(f"[collect] 找到 {len(metas)} 个自动修复补丁")

    if not metas:
        print("[done] 无自动修复补丁，不推送。")
        return

    if not _gh_available() and not a.dry_run:
        print("[warn] 未检测到 gh CLI，进入 dry-run 模式（不真推）")
        a.dry_run = True

    urls = push_and_pr(repo_dir, metas, a.dry_run)
    print(f"[summary] 创建 {len(urls)} 个 PR")
    for u in urls:
        print(f"  {u}")


if __name__ == "__main__":
    main()