#!/usr/bin/env python3
"""运维自检（2026-11-23）：厂商端点探测 + 备份可恢复校验。只读、不写业务文件。

背景：
- 厂商 `/api/user/*` 与网关的存活面这半月反复变化，duty 日志只有失败形状，
  恢复与否靠碰运气发现 → 固定探测清单，每轮/手动都能一眼看到四端点状态。
- 每日备份（#109）写了 5 天但从没验证过「能回滚」——没演练过的备份不算备份。
  本脚本把最新 tar.gz 解到临时目录做形状校验（**不打印任何凭据内容**），
  把「可恢复」变成每天可自动断言的事实。

用法：
  .venv/bin/python scripts/ops_selfcheck.py            # 两项全检
  .venv/bin/python scripts/ops_selfcheck.py --backup   # 只检备份
  .venv/bin/python scripts/ops_selfcheck.py --vendor   # 只探端点
退出码：0 全过；1 有失败项（打印原因）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HOME = Path.home()
FE = HOME / ".football-engine"
BACKUP_DIR = HOME / "Backups" / "football-engine"
JWT_FILE = FE / "njstats_jwt"
API = "https://www.njstats.cn/njserver/openApi/"


def vendor_probe() -> list[tuple[str, str]]:
    """返回 [(端点名, ok/dead/5xx/err:类型)]。判定只看状态码类，不碰响应内容。"""
    import re
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    results: list[tuple[str, str]] = []
    try:
        tok = JWT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        tok = ""
    # 1) 数据网关公开（msgId=1 不带 JWT 也应 200）：用 api 模块造 protobuf 体
    try:
        spec_dir = str(Path(__file__).resolve().parent)
        import importlib.util
        s = importlib.util.spec_from_file_location(
            "api_sc", Path(spec_dir) / "njstats_api_submit.py")
        api = importlib.util.module_from_spec(s)
        assert s and s.loader
        s.loader.exec_module(api)
        st, raw = api._post_jwt(tok or "x", "1012", "1", api._field(3, b""))
        results.append(("网关 msgId=1", "ok" if st == 200 and len(raw) > 1000 else f"dead:{st}"))
        st2, raw2 = api._post_jwt(tok, "1012", "11", api._field(2, api._field_v(1, 1) + api._field_v(2, 5)))
        n = len(api.parse_my_predictions(raw2)) if st2 == 200 else -1
        results.append(("私有真值 msgId=11", "ok" if n >= 0 and st2 == 200 else f"dead:{st2}"))
    except Exception as exc:  # noqa: BLE001
        results.append(("网关/私有", f"err:{type(exc).__name__}"))
    for name, path in (("user/current", "/api/user/current"), ("user/login", "/api/user/login")):
        try:
            code = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "10", "-X", "POST" if name.endswith("login") else "GET",
                 "https://www.njstats.cn" + path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, check=False).stdout.strip()
            state = "ok" if code in ("200", "401", "400") else ("5xx" if re.match(r"5\d\d", code) else f"dead:{code}")
            results.append((name, state))
        except Exception as exc:  # noqa: BLE001
            results.append((name, f"err:{type(exc).__name__}"))
    return results


def check_latest_backup(backup_dir: Path = BACKUP_DIR) -> tuple[bool, str]:
    """解包最新备份到临时目录并校验关键文件形状（绝不打印内容）。"""
    files = sorted(backup_dir.glob("fe-state-*.tar.gz")) if backup_dir.exists() else []
    if not files:
        return False, "无备份文件"
    latest = files[-1]
    tmp = Path(tempfile.mkdtemp(prefix="fe-restore-"))
    try:
        with tarfile.open(latest, "r:gz") as tf:
            names = tf.getnames()
            try:
                tf.extractall(tmp, filter="data")   # 3.12+：只解普通文件，挡符号链接戏法
            except TypeError:                        # 老解释器无 filter 参数
                tf.extractall(tmp)                   # noqa: S202 自有本地文件
        root = tmp / ".football-engine"
        need = {
            "njstats_jwt": lambda p: p.read_bytes().startswith(b"eyJ"),
            "njstats_session_health.json": lambda p: "result" in json.loads(p.read_text(encoding="utf-8")),
            "ops_digest.json": lambda p: isinstance(json.loads(p.read_text(encoding="utf-8")), dict),
        }
        for fname, shape in need.items():
            p = root / fname
            if not p.exists():
                return False, f"{latest.name} 缺 {fname}（名单 {len(names)} 项）"
            try:
                if not shape(p):
                    return False, f"{latest.name}/{fname} 形状不对"
            except (ValueError, OSError):
                return False, f"{latest.name}/{fname} 解析失败"
        return True, f"{latest.name} 可恢复（{latest.stat().st_size // 1024} KiB，校验 3 项形状）"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="运维自检：厂商端点 + 备份可恢复")
    ap.add_argument("--vendor", action="store_true")
    ap.add_argument("--backup", action="store_true")
    args = ap.parse_args(argv)
    if not (args.vendor or args.backup):
        args.vendor = args.backup = True
    bad = 0
    if args.backup:
        ok, msg = check_latest_backup()
        print(f"backup-check: {'✅' if ok else '❌'} {msg}")
        bad += 0 if ok else 1
    if args.vendor:
        for name, state in vendor_probe():
            flag = "✅" if state == "ok" else "⚠" if state == "5xx" else "❌"
            print(f"vendor: {flag} {name} = {state}")
            bad += 1 if state.startswith(("dead", "err")) else 0
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
