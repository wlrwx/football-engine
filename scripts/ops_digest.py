#!/usr/bin/env python3
"""运维摘要：把两条模型自动化里的确定性部分整段脚本化（2026-11-03）。

为什么：实测 Qoder 1.1.79 之后单笔模型请求的 credits 大幅上升，而巡检（4 次/天）
与对账（2 次/天）做的事几乎全是确定性的——跑脚本、数轮次、拆池、算命中率、对账、
按预登记日期判延期。连「零合格归因」原 prompt 自己都写着「直接引用日志里的
匹配/候选/最高，不要自己复算」。所以判定与计数搬到这里，由 duty 每轮调用
（48 次/天、零模型成本，覆盖比原来 4 次/天更密）；模型只在 red_flags 非空时
被叫起来做排障，一天一次足够。

复用而不重写：两池 n/命中率/ROI/打平线、速度巡检、近 7 天快照缺口、账目对账
全部来自 challenge_reconcile（它又复用 experiment_pace 与 challenge_review 的
冻结判据）。这里只补它明确拿不到的两块：duty.log 的轮次/异常/归因，以及配额与
新注字段完整性。

只读仓库与 ~/.football-engine/duty.log；只写 ~/.football-engine/ops_digest.{txt,json}。
不碰 data/state/、不碰 engine/、不 commit/push、不打印任何凭据。
退出码：0=无红线，1=有红线（要人或模型看一眼），2=脚本自身出错。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import challenge_reconcile as cr  # noqa: E402

BJT = timezone(timedelta(hours=8))
HOME = Path.home()
DUTY_LOG = HOME / ".football-engine" / "duty.log"
OUT_TXT = HOME / ".football-engine" / "ops_digest.txt"
OUT_JSON = HOME / ".football-engine" / "ops_digest.json"
EXPECT_ROUNDS_PER_DAY = 48          # launchd :05/:35
SNAPSHOT_STALE_H = 26.0             # 超过一天没入库就是断链
GATE_EV_SITE = 2.0                  # 冻结门槛，只用于归因文案，不参与判定

LINE_RE = re.compile(r"^(\d{2}-\d{2}) (\d{2}):\d{2}:\d{2} (.*)$")
# 「匹配0」那种零合格行**没有**「最高」字段（2026-11-03 实测），所以它必须可选：
# 要求它存在会让数据洞那一类归因直接返回 None，正好漏掉最该报的症状。
ZERO_RE = re.compile(r"零合格.*?池(\d+)/匹配(\d+)/候选(\d+)(?:/最高([+-]?\d+(?:\.\d+)?)%)?")
# 一轮 duty 恰好留一条这个标记：成功轮是 mint(新)/mint(无变化)，失败轮是登录/网络提示。
# 不能数日志行数——一轮会写 5~7 行（实测 24h 有 276 行，其实只有 46 轮）。
ROUND_MARK_RE = re.compile(r"mint\(|需人工登录|下轮自动重试")
SECRET_RE = re.compile(r"eyJ[A-Za-z0-9_.\-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")

# 症状 → 人的动作。**顺序即优先级**：每行只取第一个匹配的规则，
# 因为 5xx 那条消息里也含「mint 失败」字样，排在 mint 后面就会被误分类成登录问题。
ERROR_RULES = (
    ("upstream", r"站点 API 服务端故障|站点 API 恢复|HTTP 5\d\d",
     "厂商 API 网关 5xx，与登录态无关（实测带/不带 cookie 都 502）；等厂商恢复，**不要人工播种**"),
    ("branch", r"duty 不在 main",
     "有进程/agent 切走了 duty 仓库的分支；确认已切回 main，并查是谁切的（2026-11-04 是执行器在大号仓库里 checkout）"),
    ("mint", r"需人工登录|mint 失败|\b401\b",
     "需用户跑 ./fe-agent seed（绝不尝试自动过验证码）"),
    ("credentials", r"Bad credentials|push 失败",
     "需更新 ~/.football-engine/github_token（./fe-agent ghtoken）"),
    ("network", r"ReadTimeout|SSLError|CERTIFICATE_VERIFY_FAILED|网络恢复",
     "站点/网络抖动，下轮自愈；持续不恢复才需人看"),
    ("djyy", r"blocked_by_edge|DJYY.*403",
     "已知 Cloudflare 拦截；快照补位生效即不算故障，不要改 cf-proxy/workflow"),
)
SUCCESS_RE = re.compile(r"已推:|1x2:|odds-snapshot:")


def mask(text: str) -> str:
    return SECRET_RE.sub("<凭据已掩码>", text)


def hour_keys(now: datetime, hours: int = 24) -> list[str]:
    return [(now - timedelta(hours=i)).strftime("%m-%d %H") for i in range(hours)][::-1]


def parse_log(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        m = LINE_RE.match(line.strip())
        if m:
            out.append((f"{m.group(1)} {m.group(2)}", m.group(3)))
    return out


def recent(parsed: list[tuple[str, str]], now: datetime) -> list[tuple[str, str]]:
    keys = set(hour_keys(now))
    return [(k, body) for k, body in parsed if k in keys]


def round_summary(rows: list[tuple[str, str]], now: datetime) -> dict:
    keys = hour_keys(now)
    marks = [(k, b) for k, b in rows if ROUND_MARK_RE.search(b)]
    seen = {k for k, _ in marks}
    missing = [k for k in keys if k not in seen]
    return {"rounds": len(marks), "expect": EXPECT_ROUNDS_PER_DAY,
            "missing_hours": missing, "missing_count": len(missing)}


def error_summary(rows: list[tuple[str, str]]) -> dict:
    """每类症状的条数，以及「最后一次出错之后是否已有成功轮次」（=已自愈）。"""
    last_ok = max((i for i, (_, b) in enumerate(rows) if SUCCESS_RE.search(b)), default=-1)
    out = {}
    hits: dict[str, list[int]] = {}
    for i, (_, body) in enumerate(rows):
        for code, pattern, _action in ERROR_RULES:
            if re.search(pattern, body):
                hits.setdefault(code, []).append(i)
                break          # 每行只归一类，按 ERROR_RULES 顺序取优先级最高的
    for code, _pattern, action in ERROR_RULES:
        idx = hits.get(code)
        if not idx:
            continue
        out[code] = {"count": len(idx), "resolved": max(idx) < last_ok, "action": action}
    return out


def attribution(rows: list[tuple[str, str]]) -> dict | None:
    """最后一轮「零合格」的归因。只引用日志字段，不复算。"""
    for _, body in reversed(rows):
        m = ZERO_RE.search(body)
        if not m:
            continue
        pool_n, matched, cand = int(m[1]), int(m[2]), int(m[3])
        top = float(m[4]) if m[4] else None
        if matched == 0:
            kind, detail = "coverage_gap", "匹配0：当天预测没落库或站点无对应赛事（数据洞，不是正常零合格）"
        elif top is None:
            kind, detail = "unknown", f"匹配{matched}/候选{cand}，日志未给最高 ev_site，无法归因"
        elif top < GATE_EV_SITE:
            kind, detail = "price_gap", f"价差不足：最高 {top}% < {GATE_EV_SITE}% 门槛，属正常零合格"
        else:
            kind, detail = "other", f"匹配{matched}/候选{cand}/最高{top}% 却未提交，需人看门槛与上界"
        return {"pool": pool_n, "matched": matched, "candidates": cand, "max_ev": top,
                "kind": kind, "detail": detail, "line": mask(body)}
    return None


def state_summary(state: dict) -> dict:
    records = state.get("records") or []
    pools = {"E8": 0, "E10": 0}
    missing_fields = 0
    for r in records:
        if r.get("gate") != "ev_site":
            continue
        exp = r.get("exp") or "E8"
        pools[exp if exp in pools else "E8"] += 1
        if not all(k in r for k in ("gate", "ev_site", "site_odds")):
            missing_fields += 1
    return {"week": state.get("week"), "used": state.get("used"), "quota": state.get("quota"),
            "records": len(records), "pools": pools, "missing_fields": missing_fields}


def snapshot_commit_age_h() -> float | None:
    p = subprocess.run(["git", "log", "-1", "--format=%ct", "--", "data/state/njstats/"],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", check=False, timeout=60)
    if p.returncode != 0 or not p.stdout.strip().isdigit():
        return None
    return (datetime.now(BJT).timestamp() - int(p.stdout.strip())) / 3600


def build_red_flags(rounds: dict, errors: dict, attr: dict | None, st: dict,
                    snap_age: float | None, account_line: str, today_site_odds: bool) -> list[dict]:
    flags: list[dict] = []

    def add(code, detail, action):
        flags.append({"code": code, "detail": detail, "action": action})

    if rounds["missing_count"] >= 2:
        add("duty_missing_hours",
            f"过去24h 缺 {rounds['missing_count']} 个整小时（{'、'.join(rounds['missing_hours'][:6])}）",
            "机器合盖/未运行；漏掉的逐时样本不可补，检查电源与唤醒设置")
    for code, info in errors.items():
        if not info["resolved"]:
            add(f"error_{code}", f"{code} 出现 {info['count']} 次且之后无成功轮次", info["action"])
    if attr and attr["kind"] == "coverage_gap":
        add("prediction_coverage_gap", attr["detail"],
            "查 data/daily/<今天>/predictions.json 是否落库；CI 回仓被拦时会出现这个症状")
    if attr and attr["kind"] in ("other", "unknown"):
        add("zero_qualify_unexplained", attr["detail"], "只读排查门槛/上界，绝不为了有注可投而改门槛")
    if snap_age is None or snap_age > SNAPSHOT_STALE_H:
        add("snapshot_not_committed",
            "快照从未入库" if snap_age is None else f"快照已 {snap_age:.1f}h 未入库",
            "样本只活在本机等于没攒；查 duty 的 commit_push 与远端权限")
    if not today_site_odds:
        add("today_site_odds_missing", "今天的站点赔率快照不在仓库里",
            "E8/E10 的配对回放前提；查 duty 是否跑过、是否推成功")
    m = re.search(r"仓库未记 (\d+) 条", account_line)
    if m and int(m[1]) > 0:
        add("account_gap", account_line, "历史上出过让球注漏记；逐场核对后再决定要不要补")
    if st.get("missing_fields"):
        add("record_fields_missing", f"{st['missing_fields']} 条 ev_site 注缺 gate/ev_site/site_odds 字段",
            "实验采集前提受损，查提交脚本写盘字段")
    return flags


def render(ctx: dict) -> list[str]:
    r, e, st = ctx["rounds"], ctx["errors"], ctx["state"]
    lines = [
        f"巡检: 过去24h {r['rounds']} 轮（基准 {r['expect']}），缺 {r['missing_count']} 个整小时"
        + (f"（{'、'.join(r['missing_hours'][:6])}）" if r["missing_hours"] else ""),
        "异常: " + ("；".join(f"{k}×{v['count']}{'(已自愈)' if v['resolved'] else ''}"
                              for k, v in e.items()) or "无"),
        "快照入库: " + (f"{ctx['snap_age']:.1f}h 前" if ctx["snap_age"] is not None else "从未"),
        f"配额: {st['week']} used={st['used']}/{st['quota']}，records={st['records']}；"
        f"ev_site 池 E8={st['pools']['E8']} / E10={st['pools']['E10']}；缺字段 {st['missing_fields']} 条",
    ]
    a = ctx["attribution"]
    if a:
        top = f"/最高{a['max_ev']}%" if a["max_ev"] is not None else ""
        lines.append(f"归因: {a['detail']}（池{a['pool']}/匹配{a['matched']}/候选{a['candidates']}{top}）")
    else:
        lines.append("归因: 近24h 无零合格轮")
    lines += ctx["reconcile"]
    flags = ctx["red_flags"]
    lines.append(f"红线: {len(flags)} 条" + ("——" + "；".join(f["code"] for f in flags) if flags else "（无需模型介入）"))
    for f in flags:
        lines.append(f"  · {f['code']}: {f['detail']} → {f['action']}")
    return [mask(x) for x in lines]


def collect(now: datetime | None = None) -> dict:
    now = now or datetime.now(BJT)
    text = DUTY_LOG.read_text(encoding="utf-8", errors="replace") if DUTY_LOG.exists() else ""
    rows = recent(parse_log(text), now)
    state = cr._load(cr.STATE / "challenge_state.json", {})
    records = cr._records()
    today = now.date()
    reconcile = [cr._fmt_pool(exp, cr.pool_stats(records, exp), today, records) for exp in ("E8", "E10")]
    reconcile += [cr.snapshot_gap_line(), cr.snapshot_line()]
    account_line = reconcile[-1]
    ctx = {
        "generated_at": now.isoformat(),
        "rounds": round_summary(rows, now),
        "errors": error_summary(rows),
        "attribution": attribution(rows),
        "state": state_summary(state),
        "snap_age": snapshot_commit_age_h(),
        "reconcile": reconcile,
    }
    ctx["red_flags"] = build_red_flags(
        ctx["rounds"], ctx["errors"], ctx["attribution"], ctx["state"], ctx["snap_age"],
        account_line, (cr.STATE / "njstats" / "site_odds" / f"{today.isoformat()}.json").exists())
    ctx["report"] = render(ctx)
    return ctx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="确定性运维摘要（巡检 + 对账）")
    ap.add_argument("--write", action="store_true", help="写 ~/.football-engine/ops_digest.{txt,json}")
    ap.add_argument("--quiet", action="store_true", help="不打印报告，只给退出码")
    args = ap.parse_args(argv)
    try:
        ctx = collect()
    except Exception as exc:
        print(f"ops_digest 自身出错: {type(exc).__name__}: {exc}")
        return 2
    if args.write:
        try:
            OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
            OUT_TXT.write_text("\n".join(ctx["report"]) + "\n", encoding="utf-8")
            OUT_JSON.write_text(json.dumps(ctx, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            print(f"ops_digest 写盘失败: {exc}")
            return 2
    if not args.quiet:
        print("\n".join(ctx["report"]))
    return 1 if ctx["red_flags"] else 0


if __name__ == "__main__":
    sys.exit(main())
