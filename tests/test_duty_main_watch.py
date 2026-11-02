"""main 写入者监督（2026-11-02，与 dsh-superagent 接入同批）。

小号拿到 Write 之后，「谁在直推 main」不再是常量。用真 git 跑，不做文本断言：
这段逻辑的价值全在 git 范围与祖先判断上，读代码看不出错。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_main_watch", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_main_watch"] = duty
SPEC.loader.exec_module(duty)


def _sh(cwd, cmd):
    return subprocess.run(["bash", "-c", cmd], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120, check=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    _sh(tmp_path, "git init -q --bare remote.git && "
                  "git -C remote.git symbolic-ref HEAD refs/heads/main")
    r = tmp_path / "r"
    r.mkdir()
    _sh(r, "git init -q -b main && git config user.email owner@localhost && git config user.name owner")
    (r / "a.txt").write_text("1\n", encoding="utf-8")
    _sh(r, "git add -A && git commit -qm 'duty: 正常提交'")
    _sh(r, f"git remote add origin {remote} && git push -q origin main")
    monkeypatch.setattr(duty, "sh", lambda *a: subprocess.run(
        list(a), cwd=str(r), capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace"))
    seen = []
    monkeypatch.setattr(duty, "log", lambda m: seen.append(m))
    return r, tmp_path / "state.json", seen


def test_unexpected_authors_keeps_order_and_dedupes():
    rows = [{"sha": "1", "email": "a@x", "subject": "s"},
            {"sha": "2", "email": "a@x", "subject": "s"},
            {"sha": "3", "email": "b@x", "subject": "s"}]
    assert duty.unexpected_authors(rows, {"b@x"}) == ["a@x"]


def test_first_run_only_seeds_baseline(repo):
    r, state, seen = repo
    assert duty.watch_main_writes(path=state) == []
    data = json.loads(state.read_text(encoding="utf-8"))
    assert "owner@localhost" in data["known_authors"]
    assert data["last_sha"] == _sh(r, "git rev-parse origin/main").stdout.strip()
    assert any("基线" in m for m in seen)


def test_known_author_does_not_alert(repo):
    r, state, _ = repo
    duty.watch_main_writes(path=state)
    (r / "a.txt").write_text("2\n", encoding="utf-8")
    _sh(r, "git add -A && git commit -qm 'duty: 又一次' && git push -q origin main")
    assert duty.watch_main_writes(path=state) == [], "owner 的正常提交被当成偷推"


def test_stranger_push_is_detected(repo):
    r, state, _ = repo
    duty.watch_main_writes(path=state)
    _sh(r, "echo x > b.txt && git add -A && "
           "git -c user.email=thief@evil -c user.name=thief commit -qm 'policy: 悄悄降门槛' && "
           "git push -q origin main")
    alerts = duty.watch_main_writes(path=state)
    assert len(alerts) == 1 and "thief@evil" in alerts[0], alerts
    # 陌生作者不并入基线：进了就等于「看过一次永久合法」
    assert "thief@evil" not in json.loads(state.read_text(encoding="utf-8"))["known_authors"]


def test_force_push_reports_non_fast_forward(repo):
    r, state, _ = repo
    duty.watch_main_writes(path=state)
    _sh(r, "echo y > c.txt && git add -A && git commit -q --amend -m '改写过的历史' && "
           "git push -q -f origin main")
    alerts = duty.watch_main_writes(path=state)
    assert any("非快进" in a for a in alerts), alerts
    assert json.loads(state.read_text(encoding="utf-8"))["last_sha"] == \
        _sh(r, "git rev-parse origin/main").stdout.strip()
