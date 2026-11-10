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
    # 厂商 API 5xx：人也修不了，只能等；派给 agent 更没用
    "error_upstream",
    # 服务端真值 0 条/断更 = token 半失效或私有端点降级，修法是用户重新登录或等厂商；
    # agent 既不能登录也不该碰账本
    "truth_snapshot_empty",
    "truth_snapshot_stale",
    # 当天预测还没生成（daily 已跑、体彩未开售）：等下一次 daily，派 agent 查不出故障
    "prediction_pending",
    # 登录态无法自动续期：修法是用户择机播种，agent 不能登录也不该碰凭据
    "njstats_session_expiring",
    # 有预测但站点池零命中：多半是译名/覆盖问题，改匹配要人判断，别让 agent 动
    "pool_no_overlap",
}
CI_KIND = "ci_failure"
SCOUT_KIND = "scout"
PR_FIX_KIND = "pr_fix"
# 人工根因：出现这些时，采集/快照类症状不派给 agent（它修不了登录态）
ROOT_CAUSES = {"error_mint", "error_credentials", "error_upstream"}

# 每日探索工单：主动挖掘「能提升预测能力」的东西，而不是只等故障。
# 这个原型没有自调度能力（worker preset 明确剔除了 scheduling），它的「主动」=
# 对观察到的事件自动评估并接受。所以由这里每天产生一条可观察事件，主题按天轮换，
# 人只需要在最后对提案做决策。全部是只读量化 + 提案，不碰冻结的判据与门槛。
SCOUT_FOCUS = (
    ("matching-loss", "匹配缺口归因",
     "池子里有上百场，但 duty 日志常年只有 `匹配5~7`。量化丢失都发生在哪：队名别名/译名不一致、"
     "日期窗口错位、联赛不在竞彩范围、还是 predictions 当天没落库。给出每一类的场数与占比，"
     "并提出可验证的最小修复（补别名/修窗口）或采集改进草案。"),
    ("calibration-strata", "分层校准偏差",
     "按联赛、赔率档（<1.60 / 1.60-2.50 / >2.50）、主客场分层，算 Brier 与命中率 vs 打平线，"
     "找出系统性偏差最大的层。只读分析、给样本量，小样本明说不可结论。"
     "**不要改 calibration 参数或文件**，把发现写成提案交人裁决。"),
    ("clv-coverage", "CLV 覆盖与匹配失败",
     "统计 CLV 面板里 league_uncovered / no_en / no_match 各自的占比与成因，量化「本可回收却丢了」"
     "的部分，提出提高覆盖的可验证办法（联赛映射、英文名来源、缓存新鲜度）。"),
    ("funnel-efficiency", "候选漏斗最卡的一环",
     "把 池→匹配→候选→合格 四级漏斗按天量化，指出最卡的一环并给出证据。"
     "**禁止建议降低 2% 门槛或放宽 25% 上界**；只找不改判据就能提升通过率的办法。"),
    ("leakage-alignment", "泄漏与训练/服务对齐",
     "审计模型特征是否混入目标市场（竞彩/站点）赔率或赛后信息，训练与服务的特征是否一致，"
     "同一口径是否在两处各写一份。给出文件行号证据与可复现的检查脚本/测试。"),
    ("sample-integrity", "样本完整性",
     "找出白丢或污染样本的地方：逐时快照缺口、结算滞后、重复计入、让球走盘因信息不足被当成失败、"
     "服务端真值与本地 records 的差异。量化影响（多少注/多少天）并提出可验证的修复。"),
)


def scout_candidate(today: str) -> dict:
    """按天轮换主题，同一天内稳定（duty 每 30 分钟跑一次，不能每次换题）。"""
    from datetime import date
    idx = date.fromisoformat(today).toordinal() % len(SCOUT_FOCUS)
    fid, title, brief = SCOUT_FOCUS[idx]
    return {"key": f"scout:{today}:{fid}", "kind": SCOUT_KIND,
            "title": title, "evidence": brief, "focus": fid}


def scout_due(state: dict, today: str) -> bool:
    """探索工单独立预算：每天一条，不占用故障派单的 DAILY_LIMIT。"""
    return state.get("scout_date") != today


def record_scout(state: dict, today: str, issue: str) -> dict:
    st = dict(state)
    st["scout_date"] = today
    st["scout_issue"] = issue
    return st


def candidates_from_prs(prs: list[dict]) -> list[dict]:
    """agent 自己的上游 PR 检查红了 → 派修复单。

    为什么需要：它的 Source 是自己的 fork，**看不见上游 PR 的 CI 结果与评论**，
    所以 PR 红了它自己不知道，只能等人转达。这里把反馈闭上：key 含 head sha，
    它推了新提交就是新 key，不会把旧失败反复派。
    """
    out = []
    for p in prs or []:
        failed = p.get("failed_checks") or []
        if not failed:
            continue
        sha = str(p.get("head_sha") or "")
        out.append({"key": f"prfix:{p.get('number')}:{sha[:8]}", "kind": PR_FIX_KIND,
                    "title": f"你的 PR #{p.get('number')} 检查未过",
                    "evidence": (f"{p.get('html_url')} | 标题：{p.get('title')} | "
                                 f"head={sha[:8]} | 红的检查：{'、'.join(failed)}"),
                    "pr_number": p.get("number")})
    return out


def fetch_agent_prs(token: str) -> list[dict]:
    """取小号在上游的开放 PR，并查每个 head 的检查结论。"""
    def get(path):
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "30", "-H", f"Authorization: Bearer {token}",
             "-H", "Accept: application/vnd.github+json", "https://api.github.com" + path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None
    prs = get(f"/repos/{UPSTREAM}/pulls?state=open&per_page=20") or []
    out = []
    for p in prs:
        if ((p.get("user") or {}).get("login") or "").lower() != AGENT.lower():
            continue
        sha = (p.get("head") or {}).get("sha") or ""
        runs = get(f"/repos/{UPSTREAM}/commits/{sha}/check-runs") or {}
        failed, pending = [], False
        for c in runs.get("check_runs") or []:
            if c.get("status") != "completed":
                pending = True
            elif c.get("conclusion") in ("failure", "timed_out", "action_required"):
                failed.append(c.get("name") or "?")
        # 还有检查在跑就先不派，避免把「尚未完成」当成失败
        if pending:
            continue
        out.append({"number": p.get("number"), "title": p.get("title"),
                    "html_url": p.get("html_url"), "head_sha": sha,
                    "failed_checks": sorted(set(failed))})
    return out


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
    scout = c.get("kind") == SCOUT_KIND
    prfix = c.get("kind") == PR_FIX_KIND
    prefix = "每日探索" if scout else ("PR 修复" if prfix else "自动派单")
    title = f"[{prefix}] {c['title']}"[:120]
    if scout:
        task = f"""## 今天的探索主题
{c['evidence']}

## 交付要求（这是挖掘与提案，不是直接改判据）
- 先**只读量化**：给出数字、样本量、口径与证据（文件行号 / 日志原文 / 可复现命令）。
- 产出 PR，内容可以是：分析脚本及其测试、诊断文档、或**预登记草案**。
- 草案写在 **PR 描述**里或 `docs/proposals/` 的**新文件**；
  **不要改 `docs/EXPERIMENTS.md`**（人审领域，CI 会直接拦）。
- 分清「已证实」与「推测」；样本不足就明说不足，不要把噪声当信号。
- 若量化后没有可行动发现，就如实报告「本轮无可行动发现」并给出数字，**不要硬凑改动**。
- E8/E10 正在按冻结判据跑样本：**不得改模型特征、门槛、上界、校准参数**，否则整段样本作废。
"""
    elif prfix:
        task = """## 要做的事
你提的上游 PR 检查没过（见上面「红的检查」）。请到**原来那个 fork 分支**上修，不要新开 PR。

- 先取失败详情：`gh run view --log-failed`（或读 check 的 annotations），定位真实报错，不要猜。
- **区分两类红**：
  1. 你自己的问题（lint/测试/逻辑）→ 修掉；
  2. 闸门误报或基础设施问题（例如报你改了你根本没碰的文件）→ **不要绕过闸门**，
     在 PR 里说明证据并停下来交给人。
- 推之前必须在本地跑通：`ruff check --select F engine scripts tests evolution` 与相关测试，
  并把输出摘要写进 PR 描述。**上一次就是因为没跑 lint 就推，被 F841 拦下。**
- 若人工已在 issue/PR 里给了复核结论（例如判定某条发现是误报），按结论改正，不要坚持原判。
"""
    else:
        task = f"""## 期望范围
先只读定位根因（可读上游 {UPSTREAM} 的代码与 CI 日志），再提**最小修复**。
允许改：`scripts/`、`.github/workflows/`、`tests/`、`engine/` 中与该根因直接相关的部分、文档。

## 验收
- 附根因说明与失败证据原文片段；
- 改动必须带**行为测试**（真跑，不做文本断言），并贴出验证输出摘要；
- `ruff check --select F engine scripts tests evolution` 与相关测试全绿。
"""
    body = f"""@{AGENT} 这是脚本自动派单（信号判定与选题由确定性脚本完成，不是人随手开的，也不需要人来 @）。

## 信号
- 类别：`{c['kind']}`
- 主题/证据：{c['evidence']}

{task}
## 交付路径
推 `agent/` 分支到 origin（你的 fork），提 PR 到 `{UPSTREAM}`，描述含改了什么/为什么/验证/回退。

## 红线（无条件）
- **不要合并任何 PR**（你对上游只有读权限，本来也合不了）。
- 不碰 `data/state/challenge_policy.json`、`data/state/challenge_state.json`、
  `data/state/review_ledger.jsonl`、`docs/EXPERIMENTS.md`、`engine/backtest/`、
  `engine/prediction/calibration.py`；不改任何门槛或判定口径。
- **不得给 main 加分支保护**：2026-11-03 实测它会拦死 `github-actions[bot]` 直推，
  导致 daily-forecast 连红四轮、当天预测缺失。
- **禁止在 `/Users/dykily/football-engine` 里做任何 git 写操作或改任何文件**（那是大号的生产仓库，
  duty 每 30 分钟在里面提交推送 main）；只允许用它的解释器，且 cwd 必须是你的工单目录。
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
    ap.add_argument("--no-scout", action="store_true", help="不派每日探索工单（只派故障/CI）")
    ap.add_argument("--no-pr-feedback", action="store_true",
                    help="不查小号自己的上游 PR 检查（关闭 CI 反馈回路）")
    args = ap.parse_args(argv)

    today = datetime.now(BJT).date().isoformat()
    state = _load(STATE_FILE, {})
    digest = _load(DIGEST_FILE, {})
    cands = candidates_from_flags(digest.get("red_flags") or [])
    skipped = [f.get("code") for f in (digest.get("red_flags") or [])
               if f.get("code") not in DISPATCHABLE]
    # 登录/凭据类根因存在时，「快照未入库」「预测没落库」只是它的症状，
    # 修法是人工播种/换 token，派给 agent 只会让它去改不该改的地方。
    if any(f.get("code") in ROOT_CAUSES for f in (digest.get("red_flags") or [])):
        if cands:
            print(f"派单: 存在登录/凭据根因（需人工），症状类不派给 agent: {[c['key'] for c in cands]}")
        cands = []

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

    # 闭环：小号看不见上游 PR 的 CI（它的 Source 是 fork），红了必须由这里转达
    if not args.no_pr_feedback and token:
        try:
            cands += candidates_from_prs(fetch_agent_prs(token))
        except Exception as exc:
            print(f"派单: 查小号 PR 检查失败（{type(exc).__name__}）")

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

    # 每日探索：独立预算，不占故障派单额度；这是「主动找活」的来源——
    # 原型自身没有调度能力，由这里每天产生一条可观察事件，主题按天轮换。
    if not args.no_scout and scout_due(state, today):
        c = scout_candidate(today)
        title, body = render_issue(c)
        if args.dry_run:
            print(f"派单[dry-run]: 将建探索 issue「{title}」 key={c['key']}")
            state = record_scout(state, today, "(dry-run)")
        elif not token:
            print("派单: 无 token，探索工单跳过")
        else:
            url = create_issue(token, title, body)
            if url:
                state = record_scout(state, today, url)
                print(f"派单: 已建每日探索 {url} 主题={c['focus']}")
            else:
                print("派单: 探索 issue 建失败（不记状态，下轮重试）")
    elif not args.no_scout:
        print("派单: 今日探索工单已派过，跳过")

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
