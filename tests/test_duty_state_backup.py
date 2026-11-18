"""本地状态日备（2026-11-17）：~/.football-engine 每天一份 tar.gz，同盘、留 7 份。

背景：Time Machine 从未配置，登录态/凭据/健康/marker 全部单副本。备份的失败
路径必须只写一行日志（绝不拖垮采集链），当天已成功不许重跑，超额旧档要滚掉。
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_backup", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_backup"] = duty
SPEC.loader.exec_module(duty)


@pytest.fixture
def env(tmp_path, monkeypatch):
    fe = tmp_path / ".football-engine"
    fe.mkdir()
    (fe / "njstats_session.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(duty, "HOME", tmp_path)
    monkeypatch.setattr(duty, "STATE_BACKUP_DIR", tmp_path / "Backups" / "football-engine")
    monkeypatch.setattr(duty, "STATE_BACKUP_MARKER", fe / ".state_backup_date")
    logs = []
    monkeypatch.setattr(duty, "log", lambda m: logs.append(m))
    monkeypatch.delenv("DUTY_SKIP_STATE_BACKUP", raising=False)
    return tmp_path, fe, logs


def _real_tar(tmp_path, fe, logs):
    duty._maybe_state_backup()
    return tmp_path, fe, logs


def test_first_backup_writes_archive_and_marker(env):
    tmp_path, fe, logs = env
    _real_tar(tmp_path, fe, logs)
    archives = list((tmp_path / "Backups" / "football-engine").glob("fe-state-*.tar.gz"))
    assert len(archives) == 1
    if os.name == "posix":
        # Windows 不映射 Unix 权限位（实测 st_mode=0o666）——#95 同款坑，断言仅限 POSIX
        assert oct(archives[0].stat().st_mode & 0o777) == "0o600", "凭据备份必须收紧权限"
    # tar 里能看到源目录（POSIX 归档名 = 相对 -C 的 .football-engine）
    listing = subprocess.run(["tar", "-tzf", str(archives[0])], capture_output=True,
                             text=True, encoding="utf-8", check=True).stdout
    assert ".football-engine/njstats_session.json" in listing
    assert "state-backup" in logs[-1]


def test_second_call_same_day_is_noop(env):
    tmp_path, fe, logs = _real_tar(*env)
    n_before = len(list((tmp_path / "Backups" / "football-engine").iterdir()))
    duty._maybe_state_backup()
    assert len(list((tmp_path / "Backups" / "football-engine").iterdir())) == n_before


def test_retention_prunes_to_seven(env):
    tmp_path, fe, logs = env
    bdir = tmp_path / "Backups" / "football-engine"
    bdir.mkdir(parents=True)
    for d in range(9):
        (bdir / f"fe-state-2026-10-{20 + d:02d}.tar.gz").write_bytes(b"x")
    duty._maybe_state_backup()
    kept = sorted(p.name for p in bdir.glob("fe-state-*.tar.gz"))
    assert len(kept) == duty.STATE_BACKUP_KEEP, kept


def test_tar_failure_does_not_raise_or_mark(env, monkeypatch):
    tmp_path, fe, logs = env

    def boom(args, **kw):
        raise OSError("disk gone")
    monkeypatch.setattr(duty.subprocess, "run", boom)
    duty._maybe_state_backup()  # 不许抛
    assert any("state-backup 异常" in x for x in logs)
    assert not (fe / ".state_backup_date").exists(), "失败不留当日标记：下一轮要能重试"


def test_skip_env(env, monkeypatch):
    tmp_path, fe, logs = env
    monkeypatch.setenv("DUTY_SKIP_STATE_BACKUP", "1")
    duty._maybe_state_backup()
    assert logs == [] and not (tmp_path / "Backups").exists()
