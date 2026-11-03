"""ci-sync 回仓必须真提交、失败必须出声（2026-11-04）。

原实现：`git push origin HEAD:main || echo "推送跳过（无新提交或竞态，下轮重试）"`。
11-02 给 main 加分支保护后，github-actions[bot] 的直推被 GH006 拦掉：daily-forecast
红了四轮才被发现，而 ci-sync 同样被拦却**一直是绿的**——它是那次事故的隐形放大器。

用真 git + 本地裸库跑，并用 pre-receive 钩子模拟「服务端拒推」（等价于 GH006），
不做文本断言：旧代码文本里也有 git push，看着一切正常。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "ci-sync.yml"
STEP = "提交回仓库"


def _bash():
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    candidates = []
    if git:
        candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
    return next((c for c in candidates if Path(c).exists()), None)


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="需要 Git Bash / POSIX bash")


def sh(cmd, cwd):
    return subprocess.run([BASH, "-c", cmd], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, timeout=180)


def block():
    lines = WF.read_text(encoding="utf-8").splitlines()
    start = next(k for k, l in enumerate(lines) if l.strip() == f"- name: {STEP}")
    run_at = next(k for k in range(start, len(lines)) if lines[k].strip() == "run: |")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip())
    out = []
    for line in lines[run_at + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        body = line[indent + 2:] if line.strip() else ""
        if "remote set-url" in body:      # 测试用本地裸库，不改 remote
            continue
        out.append(body)
    text = "\n".join(out)
    assert "git push" in text and "exit 1" in text, "抠错了段落"
    return text


@pytest.fixture
def env(tmp_path):
    remote = tmp_path / "remote.git"
    sh('git init -q --bare remote.git && git -C remote.git symbolic-ref HEAD refs/heads/main', tmp_path)
    a = tmp_path / "a"
    sh("git clone -q remote.git a", tmp_path)
    sh("git config user.email t@t && git config user.name t", a)
    (a / "data").mkdir()
    (a / "web").mkdir()
    (a / "data" / "x.json").write_text('{"v":0}\n', encoding="utf-8")
    (a / "web" / "index.html").write_text("base\n", encoding="utf-8")
    sh("git add -A && git commit -qm base && git push -q origin HEAD:main", a)
    return remote, a, tmp_path


def test_no_change_exits_zero_without_commit(env):
    remote, a, _ = env
    r = sh(block(), a)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "无变更" in r.stdout
    log = sh(f'git --git-dir="{remote}" log --oneline', a).stdout
    assert log.count("\n") == 1, "无变更却产生了提交"


def test_change_lands_on_remote(env):
    remote, a, _ = env
    (a / "data" / "x.json").write_text('{"v":1}\n', encoding="utf-8")
    r = sh(block(), a)
    assert r.returncode == 0, r.stdout + r.stderr
    body = sh(f'git --git-dir="{remote}" show main:data/x.json', a).stdout
    assert '"v":1' in body, "生成物没有进仓"


def test_server_rejected_push_must_fail_loudly(env):
    """模拟 GH006：服务端拒推。旧代码在这里 exit 0 并打印「推送跳过」。"""
    remote, a, _ = env
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: error: GH006 protected branch' >&2\nexit 1\n",
                    encoding="utf-8")
    hook.chmod(0o755)
    (a / "data" / "x.json").write_text('{"v":2}\n', encoding="utf-8")
    r = sh(block(), a)
    out = r.stdout + r.stderr
    assert r.returncode != 0, "服务端拒推却返回 0 —— 生成物丢了而这一步是绿的"
    assert "::error::" in out, out[-400:]
    assert "推送跳过" not in out, "还在用那句无害提示掩盖失败"


def test_conflict_aborts_and_fails(env):
    remote, a, tmp = env
    b = tmp / "b"
    sh("git clone -q remote.git b", tmp)
    sh("git config user.email o@o && git config user.name o", b)
    (b / "data" / "x.json").write_text('{"v":99}\n', encoding="utf-8")
    sh("git commit -qam '上游先改' && git push -q origin HEAD:main", b)
    (a / "data" / "x.json").write_text('{"v":3}\n', encoding="utf-8")
    r = sh(block(), a)
    out = r.stdout + r.stderr
    assert r.returncode != 0, "两边都改了同一文件却返回 0"
    assert "::error::" in out
    assert "rebase" not in sh("git status --short", a).stdout.lower(), "退出时还停在 rebase 中间态"
    upstream = sh(f'git --git-dir="{remote}" show main:data/x.json', a).stdout
    assert '"v":99' in upstream, "冲突处理覆盖了先推的一方"


def test_missing_web_dir_still_commits(env):
    remote, a, _ = env
    shutil.rmtree(a / "web")
    (a / "data" / "x.json").write_text('{"v":4}\n', encoding="utf-8")
    r = sh(block(), a)
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"v":4' in sh(f'git --git-dir="{remote}" show main:data/x.json', a).stdout


def test_extraction_keeps_the_retry_and_loud_failure():
    b = block()
    assert re.search(r"for i in 1 2 3", b), "丢了重试"
    assert b.count("exit 1") >= 2, "冲突与推送失败都必须非零退出"
