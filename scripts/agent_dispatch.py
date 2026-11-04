#!/usr/bin/env python3
"""把生产信号自动派成 dsh-superagent 工单（2026-11-04）。

为什么需要它：dsh-superagent 的 Source 仓库必须是 agent 能 push 的仓库——实测把
Source 指向只读上游会在**保存时**被拒（`@dykily cannot push to wlrwx/football-engine`），
所以它只能监听小号 fork 的动静，看不见上游 CI 红绿。给上游写权限就能直接监听，但那会
毁掉「小号结构上合不了 PR」这个保证（实测无写权限时合并端点返回 404），并把我们带回
「分支保护拦死 CI 回仓」的事故（2026-11-03 daily-forecast 连红四轮）。

折中：判定留在确定性脚本这边，本脚本把信号写成 fork 上的 issue（用 wlrwx 身份，它在
agent 的 allowed list 里 → 显式请求，任何 eval mode 都生效）。agent 干活时本来就能读
上游（public + pull），所以给证据链接就够。

安全边界（硬编码，不靠提示词）：
- 只派「可安全自动修」的类别；涉及账本/策略/门槛/凭据/机器电源的红线**只报给人**。
- 同一 key 一天只派一次；每天总量上限 DAILY_LIMIT。
- 不要求上游写权限、不合并、不改门槛。

退出码：0 正常（含无信号），2 自身出错。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
BJT = timezone(timedelta(hours=8))
STATE_FILE = HOME / ".football-engine" / "agent_dispatch.json"
DIGEST_FILE = HOME / ".football-engine" / "ops_digest.json"
TOKEN_FILE = HOME / ".football-engine" / "github_token"
FORK = "dykily/football-engine"
UPSTREAM = "wlrwx/football-engine"
AGENT = "dykily"
DAILY_LIMIT = 3
CI_WORKFLOWS = ("daily-forecast.yml", "challenge.yml", "ci-sync.yml", "test.yml",
                "health-check.yml", "weekly-deep-check.yml")

# 可安全交给 agent 自动修的：都是「代码/CI 层面可验证」的故障。
DISPATCHABLE = {
    "prediction_coverage_gap": "当天预测没落库（CI 回仓链路）",
    "snapshot_not_committed": "逐时快照长时间未入库（提交链路）",
    "today_site_odds_missing": "今天的站点赔率快照不在仓库里",
}
# 其余红线一律只报给人：账本/策略/门槛/凭据/电源都不是 agent 该自己动的。
HUMAN_ONLY = {
    "account_gap", "record_fields_missing", "zero_qualify_unexplained",
    "error_mint", "error_credentials", "error_network", "error_djyy",
    "duty_missing_hours",
    # 分支被切走这件事本身就是 agent 越界造成的，绝不能派给它自己「修」
    "error_branch",
}
CI_KIND = "ci_failure"


def candidates_from_flags(flags: list[dict]) -> list[dict]:
    out = []
    for f in flags or []:
        code = f.get("code")
        if code in DISPATCHABLE:
            out.append({"key": f"flag:{code}", "kind": code,
                        "title": DISPATCHABLE[code],
                        "evidence": f"{f.get('detail', '')} → {f.get('action', '')}"})
    return out


def candidates_from_ci(runs: list[dict]) -> list[dict]:
    """每个被监听 workflow 只看**最近一次**运行：它红才派。

    2026-11-04：原实现取「最新的失败」，于是把已经修好的历史失败也派出去
    （health-check #1244 的 jq bug、weekly-deep-check #2 的 exit128 都已修好），
    等于让 agent 去修一个不存在的问题。判据必须是「现在是不是红的」。
    """
    latest: dict[str, dict] = {}
    for r in runs or []:
        wf = Path(str(r.get("path", ""))).name or str(r.get("name") or "")
        if wf not in CI_WORKFLOWS:
            continue
        prev = latest.get(wf)
        if prev is None or str(r.get("created_at", "")) > str(prev.get("created_at", "")):
            latest[wf] = r
    out = []
    for wf, r in sorted(latest.items()):
        if r.get("conclusion") != "failure":
            continue
        out.append({"key": f"ci:{wf}:{r.get('run_number')}", "kind": CI_KIND,
                    "title": f"上游 CI 失败：{wf} #{r.get('run_number')}",
                    "evidence": (f"{r.get('html_url')} | event={r.get('event')} | "
                                 f"created={r.get('created_at')} | head={str(r.get('head_sha'))[:8]} | "
                                 f"commit={str(r.get('display_title') or '')[:80]}")})
    return out


def due(state: dict, key: str, today: str, limit: int = DAILY_LIMIT) -> bool:
    count = state.get("day_count", 0) if state.get("day") == today else 0
    if count >= limit:
        return False
    last = (state.get("keys") or {}).get(key, {}).get("last_date")
    return last != today


def record(state: dict, key: str, today: str, issue: str) -> dict:
    st = dict(state)
    st["keys"] = dict(st.get("keys") or {})
    count = st.get("day_count", 0) if st.get("day") == today else 0
    st["day"] = today
    st["day_count"] = count + 1
    st["keys"][key] = {"last_date": today, "issue": issue}
    return st


def render_issue(c: dict) -> tuple[str, str]:
    title = f"[自动派单] {c['title']}"[:120]
    body = f"""@{AGENT} 这是 ops_digest/CI 自动派单（信号判定由确定性脚本完成，不是人随手开的）。

## 信号
- 类别：`{c['kind']}`
- 证据：{c['evidence']}

## 期望范围
先只读定位根因（可读上游 {UPSTREAM} 的代码与 CI 日志），再提**最小修复**。
允许改：`scripts/`、`.github/workflows/`、`tests/`、`engine/` 中与该根因直接相关的部分、文档。

## 验收
- 附根因说明与失败证据原文片段；
- 改动必须带**行为测试**（真跑，不做文本断言），并贴出验证输出摘要；
- `ruff check --select F engine scripts tests evolution` 与相关测试全绿；
- 推 `agent/` 分支到 origin（你的 fork），提 PR 到 `{UPSTREAM}`，描述含改了什么/为什么/验证/回退。

## 红线（无条件）
- **不要合并任何 PR**（你对上游只有读权限，本来也合不了）。
- 不碰 `data/state/challenge_policy.json`、`data/state/challenge_state.json`、
  `data/state/review_ledger.jsonl`、`docs/EXPERIMENTS.md`、`engine/backtest/`、
  `engine/prediction/calibration.py`；不改任何门槛或判定口径。
- **不得给 main 加分支保护**：2026-11-03 实测它会拦死 `github-actions[bot]` 直推，
  导致 daily-forecast 连红四轮、当天预测缺失。
- 不靠删测试/跳过测试/放宽断言来变绿；不打印或提交任何 JWT/cookie/token。
- 若根因落在红线范围内，**停下来只报告**，不要改。
"""
    return title, body


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def fetch_ci_runs(token: str) -> list[dict]:
    runs: list[dict] = []
    for wf in CI_WORKFLOWS:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "30", "-H", f"Authorization: Bearer {token}",
             "-H", "Accept: application/vnd.github+json",
             f"https://api.github.com/repos/{UPSTREAM}/actions/workflows/{wf}/runs?per_page=5"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        try:
            data = json.loads(r.stdout)
        except ValueError:
            continue
        for x in data.get("workflow_runs") or []:
            x["path"] = f".github/workflows/{wf}"
            x["name"] = wf
            runs.append(x)
    return runs


def create_issue(token: str, title: str, body: str) -> str:
    r = subprocess.run(
        ["curl", "-sS", "--max-time", "40", "-X", "POST",
         "-H", f"Authorization: Bearer {token}", "-H", "Accept: application/vnd.github+json",
         "-H", "Content-Type: application/json", "--data-binary",
         json.dumps({"title": title, "body": body}, ensure_ascii=False),
         f"https://api.github.com/repos/{FORK}/issues"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return ""
    return d.get("html_url") or ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把生产信号派成 agent 工单")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要派什么，不建 issue、不写状态")
    ap.add_argument("--limit", type=int, default=DAILY_LIMIT)
    ap.add_argument("--no-ci", action="store_true", help="只看 ops_digest 红线，不查上游 CI")
    args = ap.parse_args(argv)

    today = datetime.now(BJT).date().isoformat()
    state = _load(STATE_FILE, {})
    digest = _load(DIGEST_FILE, {})
    cands = candidates_from_flags(digest.get("red_flags") or [])
    skipped = [f.get("code") for f in (digest.get("red_flags") or [])
               if f.get("code") not in DISPATCHABLE]

    token = ""
    if TOKEN_FILE.exists():
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if not args.no_ci:
        if not token:
            print("派单: 无 GitHub token，跳过上游 CI 检查")
        else:
            try:
                cands += candidates_from_ci(fetch_ci_runs(token))
            except Exception as exc:
                print(f"派单: 查上游 CI 失败（{type(exc).__name__}），本轮只用红线信号")

    sent = 0
    for c in cands:
        if not due(state, c["key"], today, args.limit):
            print(f"派单: 跳过（今日已派或达上限）{c['key']}")
            continue
        title, body = render_issue(c)
        if args.dry_run:
            print(f"派单[dry-run]: 将建 issue「{title}」 key={c['key']}")
            state = record(state, c["key"], today, "(dry-run)")
            sent += 1
            continue
        if not token:
            print("派单: 无 token，无法建 issue")
            break
        url = create_issue(token, title, body)
        if not url:
            print(f"派单: 建 issue 失败 {c['key']}（不记状态，下轮重试）")
            continue
        state = record(state, c["key"], today, url)
        sent += 1
        print(f"派单: 已建 {url} key={c['key']}")

    if skipped:
        print(f"派单: 只报给人的红线（不派给 agent）: {sorted(set(skipped))}")
    if not args.dry_run:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.chmod(STATE_FILE, 0o600)
        except OSError as exc:
            print(f"派单: 状态写盘失败 {exc}")
            return 2
    print(f"派单: 本轮派出 {sent} 条（每日上限 {args.limit}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
