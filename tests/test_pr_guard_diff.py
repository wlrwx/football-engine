"""PR 闸门取改动清单必须用三点 diff（2026-11-04 误报事故）。

事故：PR #80 只新增 1 个文件，闸门却报它改了 data/state/review_ledger.jsonl 与 duty
快照而拦红。根因是 workflow 用了两点 `git diff base head`：当 fork 落后于 main 时，
上游比 fork 多出来的改动会被**反向**算成 PR 的改动。GitHub 的 Files changed 用的是
三点（merge-base）diff。这里用真 git 复现两种拓扑，直接跑 workflow 里那条命令。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "pr-guard.yml"


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


def diff_cmd():
    m = re.search(r"git diff --name-only [^\n]+", WF.read_text(encoding="utf-8"))
    assert m, "找不到取改动清单的命令"
    # 去掉 `> /tmp/changed.txt` 重定向，只留 diff 表达式本身（含三点语法）
    return m.group(0).split(">")[0].strip()


def run_diff(repo, base, head, cmd):
    # 必须先 export：`VAR=x cmd "$VAR"` 的参数展开用的是旧值（workflow 里是 env: 块，没这问题）
    script = f"export BASE_SHA={base}\nexport HEAD_SHA={head}\n{cmd}"
    r = subprocess.run([BASH, "-c", script],
                       cwd=str(repo), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", check=False, timeout=120)
    assert r.returncode == 0, r.stderr
    return {x for x in r.stdout.splitlines() if x.strip()}


@pytest.fixture
def topology(tmp_path):
    """main 先前进（改了账本），feature 从更早的点分叉（只加一个无害文件）。"""
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "README.md").write_text("base\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    fork_point = git(r, "rev-parse", "HEAD").stdout.strip()

    git(r, "checkout", "-q", "-b", "feature")
    (r / "scripts").mkdir()
    (r / "scripts" / "leak_audit_check.py").write_text("# 新文件\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "feat: 只加一个审计脚本")
    head = git(r, "rev-parse", "HEAD").stdout.strip()

    git(r, "checkout", "-q", "main")
    (r / "data" / "state").mkdir(parents=True)
    (r / "data" / "state" / "review_ledger.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "duty: 上游账本前进")
    base = git(r, "rev-parse", "HEAD").stdout.strip()
    return r, base, head, fork_point


def test_fork_behind_main_does_not_inherit_upstream_changes(topology):
    r, base, head, _ = topology
    got = run_diff(r, base, head, diff_cmd())
    assert got == {"scripts/leak_audit_check.py"}, f"把上游改动算成 PR 的了: {got}"
    assert "data/state/review_ledger.jsonl" not in got


def test_two_dot_diff_would_have_false_positived(topology):
    """证明这个测试抓得住原 bug：两点 diff 确实会冤报账本。"""
    r, base, head, _ = topology
    got = run_diff(r, base, head, 'git diff --name-only "$BASE_SHA" "$HEAD_SHA"')
    assert "data/state/review_ledger.jsonl" in got, "拓扑没复现出原事故"


def test_real_protected_change_is_still_caught(tmp_path):
    r = tmp_path / "r2"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    base = git(r, "rev-parse", "HEAD").stdout.strip()
    git(r, "checkout", "-q", "-b", "feature")
    (r / "data" / "state").mkdir(parents=True)
    (r / "data" / "state" / "review_ledger.jsonl").write_text("{}\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "改账本")
    head = git(r, "rev-parse", "HEAD").stdout.strip()
    got = run_diff(r, base, head, diff_cmd())
    assert "data/state/review_ledger.jsonl" in got, "三点 diff 漏掉了真实违规"


def test_checkout_has_full_history_for_merge_base():
    text = WF.read_text(encoding="utf-8")
    assert re.search(r"fetch-depth:\s*0", text), "浅克隆取不到 merge-base，三点 diff 会失败"
