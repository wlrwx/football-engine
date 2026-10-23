"""仓库体积守卫（2026-10-23，评审 P2#20）。

`git ls-files` 里任何超过阈值的文件都算违规——除历史遗留白名单。
防止大体量数据（parsed.jsonl 26MB 这类）再次悄悄入 git。

用法：python scripts/check_repo_size.py [--limit-mb 5]
退出码：0 = 通过；1 = 发现新增超限文件。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# 历史遗留（已在 git 历史里，清理需 force-push 重写，属用户裁决）
LEGACY_ALLOW: set[str] = {
    "data/state/fdc/parsed.jsonl",
    "data/history_parsed.jsonl",
    "data/models/lgbm_model.txt",
    "data/state/review_ledger.jsonl",   # append-only 账本，允许增长
    "data/history.log",
}


def tracked_files(root: Path) -> list[str]:
    r = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, timeout=120
    )
    if r.returncode != 0:
        print(f"git ls-files 失败: {r.stderr.strip()}", file=sys.stderr)
        return []
    return [f for f in r.stdout.split("\0") if f]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-mb", type=float, default=5.0)
    ap.add_argument("--root", default=".")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    limit = args.limit_mb * 1024 * 1024
    offenders = []
    legacy = []
    for rel in tracked_files(root):
        p = root / rel
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size <= limit:
            continue
        (legacy if rel in LEGACY_ALLOW else offenders).append((size, rel))

    for size, rel in sorted(legacy, reverse=True):
        print(f"  豁免（历史遗留）: {size / 1048576:.1f}MB  {rel}")

    if offenders:
        print("\n✗ 发现新增超大文件（阈值 %.1fMB）：" % args.limit_mb, file=sys.stderr)
        for size, rel in sorted(offenders, reverse=True):
            print(f"  {size / 1048576:.1f}MB  {rel}", file=sys.stderr)
        print(
            "\n请改用 Git LFS 或对象存储，并在 .gitignore 里排除运行时产物。\n"
            "见 docs/REPO_SIZE_POLICY_20261023.md",
            file=sys.stderr,
        )
        return 1

    print(f"✓ 仓库体积守卫通过（阈值 {args.limit_mb:.1f}MB，跟踪文件 {len(tracked_files(root))} 个）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
