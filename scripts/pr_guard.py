#!/usr/bin/env python3
"""外部 PR 闸门（2026-11-02，接 dsh-superagent 的前置件）。

为什么必须有这个文件：dsh-superagent 的授权文档 002 自己写明——「对 Executor 能否
处置自己结果的条件，不决定谁有权授权工作，它属于 agent 指令，不属于 Policy」。
也就是「只提 PR / 不许碰裁判层」这句话在它的配置里是拦不住的。而部署文档 006 的
setup 又会校验 agent 的 **push 权限**（小号不给 Write 服务起不来），Write 就能合并
PR、就能改任何文件。所以规矩必须换成 GitHub 侧过不去的东西：

1. AGENT_AUTHORS（自动化身份）**永远不能**碰 PROTECTED：裁判层、策略状态、实验
   台账。这条不看标签——标签是 Write 账号自己能加的，拿它当闸门等于没闸门。
2. 人的 PR 碰到 PROTECTED 时要带 `human-approved` 标签：这是给我们自己设的
   减速带（改门槛/洗样本前必须先有一份写明裁决口径的记录）。
3. 碰 CHALLENGE 子系统的 PR，描述里必须有「拔除影响评估」——本来就有这条规矩
   （PROJECT.md 的 PR 约定），进了 CI 就不再依赖「我记得」。

只做判定、不动仓库：退出码 0 放行、1 拒绝。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 自动化身份。它们的 PR 由 ruleset 强制「必须有人批准」，这里再堵死路径。
AGENT_AUTHORS = {"dykily"}

HUMAN_LABEL = "human-approved"
SECTION = "拔除影响评估"

# 人审领域：改了这些，agent 就是在自己改自己的裁判。
PROTECTED = (
    "data/state/challenge_policy.json",
    "data/state/challenge_state.json",
    "data/state/review_ledger.jsonl",
    "docs/EXPERIMENTS.md",
    "engine/backtest/",
    "engine/prediction/calibration.py",
)
# 挑战子系统：拔除影响评估的适用范围（含 duty/CI 提交链本身）。
CHALLENGE = (
    "scripts/challenge_",
    "scripts/njstats_",
    "data/state/challenge_",
    "data/state/njstats/",
    "engine/strategy/",
    ".github/workflows/challenge.yml",
    ".github/workflows/pr-guard.yml",
    "docs/EXPERIMENTS.md",
)


def hits(files: list[str], prefixes: tuple[str, ...]) -> list[str]:
    return [f for f in files if any(f == p or f.startswith(p) for p in prefixes)]


def decide(files: list[str], labels: list[str], body: str, author: str = "") -> list[str]:
    """返回违规说明；空列表 = 放行。"""
    bad: list[str] = []
    protected = hits(files, PROTECTED)
    if protected and (author or "").lower() in AGENT_AUTHORS:
        bad.append(
            f"自动化身份 {author} 不得改动裁判层/策略状态/实验台账："
            + "、".join(sorted(protected)[:6])
            + "（这类改动必须由人提 PR）"
        )
    elif protected and HUMAN_LABEL not in labels:
        bad.append(
            f"改动涉及人审领域（{'、'.join(sorted(protected)[:6])}），"
            f"PR 必须带 `{HUMAN_LABEL}` 标签后再合并"
        )
    challenge = hits(files, CHALLENGE)
    if challenge and SECTION not in (body or ""):
        bad.append(
            f"改动涉及挑战子系统（{'、'.join(sorted(challenge)[:4])}），"
            f"PR 描述必须含「{SECTION}」段落"
        )
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--files-from", required=True, help="每行一个改动文件路径")
    ap.add_argument("--labels-json", default="[]", help="标签名数组的 JSON")
    ap.add_argument("--body-file", default="", help="PR 描述所在文件")
    ap.add_argument("--author", default="", help="PR 发起者 login")
    args = ap.parse_args(argv)

    files = [ln.strip() for ln in Path(args.files_from).read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    labels = [str(x) for x in json.loads(args.labels_json or "[]")]
    body = ""
    if args.body_file:
        p = Path(args.body_file)
        if p.exists():
            body = p.read_text(encoding="utf-8", errors="replace")

    bad = decide(files, labels, body, args.author)
    if not bad:
        print(f"PR 闸门：放行（改动 {len(files)} 个文件，作者 {args.author or '未知'}）")
        return 0
    print("PR 闸门：拒绝")
    for item in bad:
        print(f"  · {item}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
