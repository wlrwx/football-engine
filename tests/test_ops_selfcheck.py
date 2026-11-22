"""运维自检的备份可恢复校验（2026-11-23）。

没演练过的备份不算备份——这里用真 tar 造最新档，验证「解出来且形状对」，
并钉住三种坏形状：无文件 / 缺关键件 / JWT 内容不是 eyJ 开头（半截写入）。
"""
import importlib.util
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("ops_selfcheck", ROOT / "scripts" / "ops_selfcheck.py")
sc = importlib.util.module_from_spec(SPEC)
sys.modules["ops_selfcheck"] = sc
SPEC.loader.exec_module(sc)

GOOD = {"njstats_jwt": "eyJhbGciOi.test.sig",
        "njstats_session_health.json": json.dumps({"result": "ok", "at": "x"}),
        "ops_digest.json": json.dumps({"red_flags": []})}


def _make_backup(tmp_path, files: dict, name="fe-state-2026-11-23.tar.gz", sub="src"):
    root = tmp_path / sub
    src = root / ".football-engine"
    src.mkdir(parents=True)
    for k, v in files.items():
        (src / k).write_text(v, encoding="utf-8")
    bdir = tmp_path / "backups"
    bdir.mkdir(exist_ok=True)
    with tarfile.open(bdir / name, "w:gz") as tf:
        tf.add(src, arcname=".football-engine")
    return bdir


def test_good_backup_passes(tmp_path):
    bdir = _make_backup(tmp_path, GOOD)
    ok, msg = sc.check_latest_backup(bdir)
    assert ok, msg
    assert "可恢复" in msg


def test_missing_dir_fails(tmp_path):
    ok, msg = sc.check_latest_backup(tmp_path / "nope")
    assert not ok and "无备份" in msg


def test_missing_key_file_fails(tmp_path):
    files = {k: v for k, v in GOOD.items() if k != "ops_digest.json"}
    bdir = _make_backup(tmp_path, files)
    ok, msg = sc.check_latest_backup(bdir)
    assert not ok and "ops_digest.json" in msg


def test_halfwritten_jwt_fails(tmp_path):
    bdir = _make_backup(tmp_path, {**GOOD, "njstats_jwt": "garbage-no-header"})
    ok, msg = sc.check_latest_backup(bdir)
    assert not ok and "njstats_jwt" in msg


def test_picks_newest_by_name(tmp_path):
    _make_backup(tmp_path, GOOD, name="fe-state-2026-11-22.tar.gz", sub="a")
    _make_backup(tmp_path, {**GOOD, "njstats_jwt": "broken"}, name="fe-state-2026-11-23.tar.gz", sub="b")
    ok, msg = sc.check_latest_backup(tmp_path / "backups")
    assert not ok and "11-23" in msg, "必须校验最新档，旧档合格不能掩盖"
