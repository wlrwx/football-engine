"""挑战子系统防火墙（CI 强制，docs/CHALLENGE_SUBSYSTEM.md 反馈合同的可执行版）

1. engine/ 任何文件不得引用挑战产物（challenge_state/review/policy）
2. scripts/njstats_* 与 challenge_review 不得 import engine.*（反向零依赖）
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(r"challenge_state|challenge_review|challenge_policy")

def test_engine_never_reads_challenge_outputs():
    for f in (ROOT / "engine").rglob("*.py"):
        if "__pycache__" in str(f):
            continue
        src = f.read_text(encoding="utf-8", errors="ignore")
        assert not FORBIDDEN.search(src), (
            f"模型层读取挑战产物，违反反馈合同: {f}（见 docs/CHALLENGE_SUBSYSTEM.md）")

def test_challenge_scripts_dont_import_engine():
    for name in ("njstats_api_submit.py", "njstats_capture.py", "challenge_review.py"):
        f = ROOT / "scripts" / name
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8", errors="ignore")
        bad = [l for l in re.findall(r"^\s*(?:from|import)\s+(engine[.\w]*)", src, re.M)
               if l != "engine.beijing_time"]
        assert not bad, f"{name} 只许 import 时区工具，发现: {bad}（拔除零依赖原则）"
