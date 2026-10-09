"""git 工作副本操作：让闭环能自己建分支、应用补丁、提交。

【安全边界 —— 为什么永不在主分支上动手】
本模块只在**一次性工作副本**（worktree / clone）上操作，绝不碰用户的主
仓库工作区。理由：
  1. 用户可能有未提交的改动，在其工作区上开分支会污染
  2. 闭环出错的代价必须是「一个临时目录被删掉」，而不是「项目被改坏」
  3. Actions runner 上每次都是干净 checkout，行为一致

【永不 push】
本模块只 commit，不 push、不建 PR。推送与建 PR 由独立的人工/主线步骤
执行 —— 「合入是决策」这条边界不能由脚本越过。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class GitError(Exception):
    pass


def _run(args: list[str], cwd: Path, check: bool = True) -> str:
    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise GitError(f"{' '.join(args)} 失败:\n{p.stdout}\n{p.stderr}")
    return p.stdout


def make_worktree(repo: Path, ref: str = "main") -> Path:
    """创建一次性工作副本，返回其路径。调用方负责清理。"""
    tmp = Path(tempfile.mkdtemp(prefix="evo_wt_"))
    dest = tmp / "wt"
    # 用 archive 导出目标 ref 的干净快照：不需要 git worktree 的元数据管理，
    # 也不会碰用户工作区
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=str(repo), capture_output=True)
    if archive.returncode != 0:
        raise GitError(f"git archive {ref} 失败:\n"
                       f"{archive.stderr.decode('utf-8', 'replace')}")
    dest.mkdir(parents=True, exist_ok=True)
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
        tf.extractall(dest)
    if not (dest / ".git").exists():
        _run(["git", "init", "-q"], dest)
        _run(["git", "add", "-A"], dest)
        _run(["git", "-c", "user.name=evolution",
              "-c", "user.email=evolution@local", "commit", "-q",
              "-m", "baseline snapshot"], dest)
    return dest


def apply_to_file(root: Path, rel: str, new_text: str) -> Path:
    p = root / rel
    if not p.exists():
        raise GitError(f"文件不存在: {rel}")
    p.write_text(new_text, encoding="utf-8")
    return p


def commit_branch(root: Path, branch: str, message: str,
                  files: list[str]) -> dict:
    """在工作副本里建分支并提交。返回 commit 信息。"""
    _run(["git", "checkout", "-q", "-b", branch], root)
    _run(["git", "add", "--"] + files, root)
    # 确认真的有变更 —— 空提交会让 PR 无法审阅
    status = _run(["git", "status", "--porcelain"], root).strip()
    if not status:
        raise GitError("补丁应用后无文件变更，拒绝空提交")
    _run(["git", "-c", "user.name=evolution",
          "-c", "user.email=evolution@local",
          "commit", "-q", "-m", message], root)
    sha = _run(["git", "rev-parse", "HEAD"], root).strip()
    return {"branch": branch, "commit": sha, "files": files}


def diff_summary(root: Path, base: str = "HEAD") -> str:
    try:
        return _run(["git", "diff", base, "--stat"], root).strip()
    except GitError:
        return ""


def cleanup(root: Path) -> None:
    shutil.rmtree(root.parent, ignore_errors=True)
