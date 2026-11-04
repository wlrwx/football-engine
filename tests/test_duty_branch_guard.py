"""duty 必须在 main 上提交（2026-11-04 事故）。

事故：dsh-superagent 的执行器在大号仓库里 `git checkout` 到了它自己的 agent/ 分支，
duty 随后把 3 轮快照提交到那条分支、push origin main 推的却是没动的本地 main，
快照一直没进仓。用真 git 跑，覆盖「干净自愈」与「脏工作区绝不硬切」。
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_branch_guard", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_branch_guard"] = duty
SPEC.loader.exec_module(duty)


def _bash():
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    cands = []
    if git:
        cands.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            cands.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
    return next((c for c in cands if Path(c).exists()), None)


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="需要 Git Bash / POSIX bash")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, timeout=120)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    monkeypatch.setattr(duty, "sh", lambda *a: subprocess.run(
        list(a), cwd=str(r), capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace"))
    logs = []
    monkeypatch.setattr(duty, "log", lambda m: logs.append(m))
    return r, logs


def test_on_main_is_noop(repo):
    r, logs = repo
    assert duty.ensure_on_main() is True
    assert logs == []
    assert git(r, "branch", "--show-current").stdout.strip() == "main"


def test_clean_wrong_branch_self_heals(repo):
    r, logs = repo
    git(r, "checkout", "-q", "-b", "agent/some-ticket")
    assert duty.ensure_on_main() is True
    assert git(r, "branch", "--show-current").stdout.strip() == "main"
    assert any("已切回 main" in m for m in logs), logs


def test_dirty_wrong_branch_never_force_switches(repo):
    r, logs = repo
    git(r, "checkout", "-q", "-b", "agent/some-ticket")
    (r / "a.txt").write_text("别人正在改的东西\n", encoding="utf-8")
    assert duty.ensure_on_main() is False
    assert git(r, "branch", "--show-current").stdout.strip() == "agent/some-ticket", "脏工作区被硬切了"
    assert (r / "a.txt").read_text(encoding="utf-8") == "别人正在改的东西\n", "改动被丢掉了"
    assert any("不干净" in m for m in logs), logs


def test_detached_head_is_not_main(repo):
    r, logs = repo
    sha = git(r, "rev-parse", "HEAD").stdout.strip()
    git(r, "checkout", "-q", "--detach", sha)
    assert duty.ensure_on_main() is True
    assert git(r, "branch", "--show-current").stdout.strip() == "main"
    assert any("detached" in m for m in logs), logs
