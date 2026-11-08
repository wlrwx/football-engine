"""两个「自己造成的假故障」（2026-11-08）。

1. 降级探测选了坏端点：`list_items`（msgId=11）在厂商故障期返回 None，而
   `board_items`（msgId=1）实测正常（233 场）。用前者探测会把可用 JWT 判成不可用，
   于是「降级继续」形同虚设、继续停摆。
2. main-watch 看 author 而不是 committer：PR 合并的 author 是 PR 作者（小号 dykily），
   committer 才是实际推进 main 的人。看 author 会把每个经人审核合并的 agent PR
   误报成「陌生作者直推」（实测误报 PR #87）。
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_probe_commit", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_probe_commit"] = duty
SPEC.loader.exec_module(duty)


def test_probe_uses_board_items_not_broken_list_items(tmp_path, monkeypatch):
    f = tmp_path / "jwt"
    f.write_text("cached-tok", encoding="utf-8")
    monkeypatch.setattr(duty, "JWT_FILE", f)
    monkeypatch.setattr(duty.api, "list_items", lambda *a, **k: None)
    monkeypatch.setattr(duty.api, "board_items", lambda *a, **k: [{"sid": "x"}])
    assert duty._cached_jwt_usable() == "cached-tok", "探测端点选错会让降级形同虚设"


def test_probe_still_rejects_when_board_also_fails(tmp_path, monkeypatch):
    f = tmp_path / "jwt"
    f.write_text("cached-tok", encoding="utf-8")
    monkeypatch.setattr(duty, "JWT_FILE", f)
    monkeypatch.setattr(duty.api, "board_items", lambda *a, **k: None)
    assert duty._cached_jwt_usable() is None


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
def test_pr_merge_with_foreign_author_is_not_a_stranger(tmp_path, monkeypatch):
    def sh(cmd, cwd):
        return subprocess.run([BASH, "-c", cmd], cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=120)
    sh("git init -q --bare remote.git && git -C remote.git symbolic-ref HEAD refs/heads/main", tmp_path)
    r = tmp_path / "r"
    r.mkdir()
    sh("git init -q -b main && git config user.email owner@localhost && git config user.name owner", r)
    (r / "a.txt").write_text("1\n", encoding="utf-8")
    sh('git add -A && git commit -qm base && git remote add origin ../remote.git && git push -q origin main', r)
    monkeypatch.setattr(duty, "sh", lambda *a: subprocess.run(
        list(a), cwd=str(r), capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace"))
    monkeypatch.setattr(duty, "log", lambda m: None)
    state = tmp_path / "state.json"
    assert duty.watch_main_writes(path=state) == []          # 建基线

    # 模拟 squash 合并：author 是小号，committer 仍是 owner
    (r / "b.txt").write_text("2\n", encoding="utf-8")
    sh("git add -A && GIT_AUTHOR_NAME=dykily GIT_AUTHOR_EMAIL=dykily@msn.com "
       "git commit -qm 'feat: agent PR' && git push -q origin main", r)
    assert duty.watch_main_writes(path=state) == [], "经人合并的 agent PR 被误报成陌生直推"

    # 真正的陌生直推（committer 也是陌生人）仍要被抓到
    (r / "c.txt").write_text("3\n", encoding="utf-8")
    sh("git add -A && GIT_AUTHOR_NAME=thief GIT_AUTHOR_EMAIL=thief@evil "
       "GIT_COMMITTER_NAME=thief GIT_COMMITTER_EMAIL=thief@evil "
       "git commit -qm '偷偷直推' && git push -q origin main", r)
    alerts = duty.watch_main_writes(path=state)
    assert any("thief@evil" in a for a in alerts), alerts
