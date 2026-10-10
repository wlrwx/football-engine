"""git 工作副本操作：让闭环能自己建分支、应用补丁、提交。

【架构演进 2026-10-09 —— 修复「分支消失」断链】

旧设计用 `git archive` 把代码导出到独立 tmp 目录，再 `git init` 成新仓库
commit。问题是 cleanup 把 tmp 删掉后，**那个分支和 commit 就从世界上消失**
——它们从没存在于 origin 上，push_and_pr 推的是主仓库里不存在的分支。

新设计分两种模式，由调用方选择：

  模式 A（本地复用，默认）：仍在一次性 tmp 工作副本上操作，永不碰用户
  工作区。适用于本地试跑/审计。产出的 commit sha 只对 tmp 有意义。

  模式 B（Actions/真实仓库，`direct=True`）：直接在传入的 repo 上
  checkout 新分支、应用补丁、commit。适用于 GitHub Actions runner ——
  那里本来就是一次性干净 checkout，没有「污染用户工作区」的顾虑。
  这样分支真实存在于仓库的 git 对象里，后续 `git push origin <branch>`
  才能推上去。

【安全边界（两种模式都不变）】
  1. 永不碰 main / master —— 只 checkout -b 新分支
  2. 只 commit，不 push、不建 PR、绝不自动合入
  3. 失败就回滚当前分支状态，不留残留分支
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


# ------------------------------------------------------------------ 模式 A

def make_worktree(repo: Path, ref: str = "main") -> Path:
    """创建一次性工作副本（独立 tmp 目录），返回其路径。调用方负责清理。

    仅用于本地试跑；其 commit 不进入 origin。
    """
    tmp = Path(tempfile.mkdtemp(prefix="evo_wt_"))
    dest = tmp / "wt"
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
    status = _run(["git", "status", "--porcelain"], root).strip()
    if not status:
        raise GitError("补丁应用后无文件变更，拒绝空提交")
    _run(["git", "-c", "user.name=evolution",
          "-c", "user.email=evolution@local",
          "commit", "-q", "-m", message], root)
    sha = _run(["git", "rev-parse", "HEAD"], root).strip()
    return {"branch": branch, "commit": sha, "files": files}


def cleanup(root: Path) -> None:
    shutil.rmtree(root.parent, ignore_errors=True)


# ------------------------------------------------------------------ 模式 B

def checkout_branch(direct_repo: Path, branch: str, base_ref: str) -> str:
    """在真实仓库上从 base_ref 切出新分支，返回之前的 HEAD（用于回滚）。

    前置：直接仓库必须是干净状态（Actions checkout 保证）。
    """
    prev = _run(["git", "rev-parse", "HEAD"], direct_repo).strip()
    _run(["git", "checkout", "-q", "-B", branch, base_ref], direct_repo)
    return prev


def commit_in_repo(direct_repo: Path, branch: str, message: str,
                   files: list[str]) -> dict:
    """在真实仓库当前分支上提交，返回 commit 信息。"""
    _run(["git", "add", "--"] + files, direct_repo)
    status = _run(["git", "status", "--porcelain"], direct_repo).strip()
    if not status:
        # 无变更，回滚到之前的 base
        return {"branch": branch, "commit": "", "files": files, "empty": True}
    _run(["git", "-c", "user.name=evolution",
          "-c", "user.email=evolution@local",
          "commit", "-q", "-m", message], direct_repo)
    sha = _run(["git", "rev-parse", "HEAD"], direct_repo).strip()
    return {"branch": branch, "commit": sha, "files": files, "empty": False}


def checkout_back(direct_repo: Path, ref: str) -> None:
    """回滚到 ref（通常切回 detached 或之前的 base）。"""
    try:
        _run(["git", "checkout", "-q", ref], direct_repo)
    except GitError:
        _run(["git", "checkout", "-q", "main"], direct_repo)