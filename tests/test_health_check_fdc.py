"""health-check 的 football-data 新鲜度判定（2026-11-04）。

真跑那段 shell，不做文本断言。历史 bug：jq 程序被拆成两行，第一行末尾就闭合了
单引号，第二行被 shell 当成新命令 → jq compile error → 整步 exit 3，**下面两条
真正的判定从来没执行过**。连续 5 轮 health-check 红，红的是检查自己；而数据其实
是好的（43/45、最新到 11-01）。文本里看着有 `::error::`，所以只有真跑才抓得到。
"""
import datetime
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "health-check.yml"
STEP = "football-data cache freshness (read-only)"


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
pytestmark = pytest.mark.skipif(BASH is None or shutil.which("jq") is None,
                                reason="需要 bash + jq")


def _block():
    doc = yaml.safe_load(WF.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == STEP:
                run = step["run"]
                # 只取判定部分，把 gh api 取数换成夹具
                return run[run.index('if [ -z "$ST" ]; then'):]
    pytest.fail(f"找不到步骤 {STEP}")


def run_with(tmp_path, status):
    if status is None:
        script = 'ST=""\n' + _block()
    else:
        f = tmp_path / "status.json"
        f.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        script = f'ST=$(cat "{f}")\n' + _block()
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, timeout=90)


def healthy(**over):
    d = {"files_ok": 43, "files_total": 45, "rows": 61885, "proxy_configured": False,
         "latest_date": (datetime.date.today() - datetime.timedelta(days=3)).isoformat()}
    d.update(over)
    return d


def test_healthy_cache_passes_and_prints_one_line(tmp_path):
    r = run_with(tmp_path, healthy())
    assert r.returncode == 0, f"jq 程序又被拆行了？rc={r.returncode}\n{r.stdout}{r.stderr}"
    assert "43/45" in r.stdout and "直连" in r.stdout, r.stdout
    assert r.stdout.count("→ football-data:") == 1, r.stdout


def test_all_files_failed_fails_the_step(tmp_path):
    r = run_with(tmp_path, healthy(files_ok=0))
    assert r.returncode == 1, "全部刷新失败却仍然是绿的（判定没长牙）"
    assert "::error::" in r.stdout and "全部文件刷新失败" in r.stdout


def test_stale_cache_fails_the_step(tmp_path):
    old = (datetime.date.today() - datetime.timedelta(days=20)).isoformat()
    r = run_with(tmp_path, healthy(latest_date=old))
    assert r.returncode == 1, "数据停更 20 天却仍然是绿的"
    assert "超过 8 天" in r.stdout, r.stdout


def test_missing_status_is_warning_not_failure(tmp_path):
    r = run_with(tmp_path, None)
    assert r.returncode == 0
    assert "::warning::" in r.stdout


def test_proxy_configured_is_reported(tmp_path):
    r = run_with(tmp_path, healthy(proxy_configured=True))
    assert r.returncode == 0 and "代理已配置" in r.stdout
