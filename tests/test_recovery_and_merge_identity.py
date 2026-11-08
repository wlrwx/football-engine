"""降级成功不等于厂商恢复；GitHub 合并身份不是陌生直推（2026-11-08）。

两个实测误报：
1. 降级用缓存 JWT 那轮打印了「站点 API 恢复（中断 0.1h 后恢复）」，而 mint 仍 502。
   假恢复会让人以为厂商好了，还把中断计时清零、丢掉真实停摆时长。
2. 用 owner 身份在 GitHub 上 squash 合并 agent PR后，committer 是
   `GitHub <noreply@github.com>`，被 main-watch 当成陌生作者直推报警。
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_watch_merge", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_watch_merge"] = duty
SPEC.loader.exec_module(duty)


@pytest.mark.parametrize("minted", [None, duty.MINT_NET, duty.MINT_SERVER])
def test_fallback_success_does_not_claim_recovery(minted, monkeypatch):
    cleared, logs = [], []
    monkeypatch.setattr(duty, "_net_fail_clear", lambda ts, path=None: cleared.append(path) or "（中断 5h）")
    monkeypatch.setattr(duty, "log", lambda m: logs.append(m))
    duty._log_recovery(minted)
    assert cleared == [], "降级成功不该清掉厂商中断计时"
    assert logs == [], "降级成功不该报恢复"


def test_real_mint_success_clears_both_timers(monkeypatch):
    cleared, logs = [], []
    monkeypatch.setattr(duty, "_net_fail_clear", lambda ts, path=None: cleared.append(path) or "（中断 5h）")
    monkeypatch.setattr(duty, "log", lambda m: logs.append(m))
    duty._log_recovery("fresh-token")
    assert cleared == [None, duty.UPSTREAM_STATE]
    assert any("网络恢复" in m for m in logs) and any("站点 API 恢复" in m for m in logs)


def _bash():
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    c = []
    if git:
        c.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            c.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
    return next((x for x in c if Path(x).exists()), None)


BASH = _bash()


@pytest.mark.skipif(BASH is None, reason="需要 Git Bash / POSIX bash")
def test_github_merge_identity_is_not_a_stranger(tmp_path, monkeypatch):
    def sh(cmd, cwd):
        return subprocess.run([BASH, "-c", cmd], cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=120)
    sh("git init -q --bare remote.git && git -C remote.git symbolic-ref HEAD refs/heads/main", tmp_path)
    r = tmp_path / "r"
    r.mkdir()
    sh("git init -q -b main && git config user.email owner@localhost && git config user.name owner", r)
    (r / "a.txt").write_text("1\n", encoding="utf-8")
    sh("git add -A && git commit -qm base && git remote add origin ../remote.git && git push -q origin main", r)
    monkeypatch.setattr(duty, "sh", lambda *a: subprocess.run(
        list(a), cwd=str(r), capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace"))
    monkeypatch.setattr(duty, "log", lambda m: None)
    state = tmp_path / "state.json"
    assert duty.watch_main_writes(path=state) == []

    # GitHub 网页/API squash 合并：author 是小号、committer 是 GitHub
    (r / "b.txt").write_text("2\n", encoding="utf-8")
    sh("git add -A && GIT_AUTHOR_NAME=dykily GIT_AUTHOR_EMAIL=dykily@msn.com "
       "GIT_COMMITTER_NAME=GitHub GIT_COMMITTER_EMAIL=noreply@github.com "
       "git commit -qm 'feat: agent PR (#90)' && git push -q origin main", r)
    assert duty.watch_main_writes(path=state) == [], "GitHub 合并身份被误报成陌生直推"
