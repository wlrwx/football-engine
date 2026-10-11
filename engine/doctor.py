#!/usr/bin/env python3
"""系统医生：统一的自愈/体检框架（2026-10-11）

背景：本项目的事故史反复证明"静默失效 + 无人修复"是主要故障模式——
DJYY 断供 3.5 周无人知、djyy_prob 断链 6 周、rebase 卡死 2 小时推送全灭、
账本亏损低估 45 倍。单点报警（selfcheck）与零散自愈（heal_proxy、git 清场）
不足以覆盖。本框架把检查与修复注册成表，一条检查对应一类真实事故。

设计原则：
1. 检查与修复分离：每条 check 返回 finding；heal 只做幂等、可验证的动作
2. "远端为准，数据每小时重生成"：git 类修复永远安全；补结算/补抓取幂等
3. 非致命默认：返回 0；仅 critical（数据流断裂级）返回 2 供 workflow 阻断
4. 报告统一落 data/state/system_health.json，供周检/Pages 消费

用法：
    python -m engine.doctor            # 全量检查 + 安全自愈（auto_run 末尾调用）
    python -m engine.doctor --check    # 只检查不修复
    python -m engine.doctor --no-net   # 跳过需要外网 API 的检查（CI/离线用）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.beijing_time import beijing_today  # noqa: E402

DAILY = ROOT / "data" / "daily"
STATE = ROOT / "data" / "state"
LEDGER = STATE / "review_ledger.jsonl"
REPO = ROOT


@dataclass
class Finding:
    """单条体检结论。healed=True 表示本次运行已自动修复。"""
    id: str
    layer: str          # git / data / source / model / env
    severity: str       # ok / info / warn / critical
    detail: str
    healable: bool = False
    healed: bool = False


@dataclass
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    @property
    def n_critical(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    @property
    def n_healed(self) -> int:
        return sum(1 for f in self.findings if f.healed)

    def to_json(self) -> dict:
        return {
            "generated_at": beijing_today() + "T" + time.strftime("%H:%M:%S"),
            "summary": {
                "total": len(self.findings),
                "critical": self.n_critical,
                "healed": self.n_healed,
            },
            "findings": [asdict(f) for f in self.findings],
        }


# ---------------------------------------------------------------- git 工具

def _git(args: list[str], cwd: Path = REPO) -> tuple[int, str]:
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def _run(args: list[str], timeout: int = 300) -> tuple[int, str]:
    r = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def _beijing_date(days_back: int = 0) -> str:
    d = date.fromisoformat(beijing_today()) - timedelta(days=days_back)
    return d.isoformat()


# ---------------------------------------------------------------- git 层

def check_git_state(report: DoctorReport, heal: bool) -> None:
    """事故史：rebase 卡死 → detached HEAD → 推送全灭（2026-10-11 两次复发）。"""
    stuck = ((REPO / ".git" / "rebase-merge").exists()
             or (REPO / ".git" / "rebase-apply").exists())
    _, head_branch = _git(["symbolic-ref", "-q", "--short", "HEAD"])
    detached = head_branch == ""

    if stuck or detached:
        if heal:
            _git(["rebase", "--quit"]) if stuck else None
            _git(["checkout", "-B", "main"])
            _git(["fetch", "origin", "main"])
            _git(["reset", "--hard", "origin/main"])
            report.add(Finding(
                "git/state", "git", "warn",
                f"残留 rebase 状态={stuck}, detached HEAD={detached} → 已清场对齐远端",
                healable=True, healed=True))
        else:
            report.add(Finding(
                "git/state", "git", "critical",
                f"残留 rebase 状态={stuck}, detached HEAD={detached}（推送将全灭）"))
    else:
        report.add(Finding("git/state", "git", "ok", "分支状态正常"))

    # 本地积压未推送（事故史：推送失败静默积压 6 个提交 5 小时）
    _git(["fetch", "origin", "main"])
    _, ahead = _git(["rev-list", "--count", "origin/main..main"])
    n_ahead = int(ahead) if ahead.isdigit() else 0
    if n_ahead > 0:
        healed = False
        if heal:
            rc, _ = _git(["push", "origin", "main"])
            healed = rc == 0
        report.add(Finding(
            "git/backlog", "git", "info" if healed else "warn",
            f"本地领先 origin/main {n_ahead} 个提交" + ("→ 已补推" if healed else "（推送失败，下轮重试）"),
            healable=True, healed=healed))
    else:
        report.add(Finding("git/backlog", "git", "ok", "无未推送积压"))

    # fetch refspec 被收窄（事故史：本地永远看不见远端其他分支，8 个分支无感知）
    _, refspec = _git(["config", "remote.origin.fetch"])
    if refspec and "refs/heads/*" not in refspec:
        if heal:
            _git(["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"])
            report.add(Finding(
                "git/refspec", "git", "info",
                f"fetch refspec 收窄（{refspec}）→ 已恢复全量", healable=True, healed=True))
        else:
            report.add(Finding("git/refspec", "git", "info",
                               f"fetch refspec 收窄（{refspec}），本地看不见远端其他分支",
                               healable=True))
    else:
        report.add(Finding("git/refspec", "git", "ok", "fetch refspec 全量"))


# ---------------------------------------------------------------- 数据流层

def _load_preds(day: str) -> list:
    pf = DAILY / day / "predictions.json"
    if not pf.exists():
        return []
    try:
        d = json.loads(pf.read_text(encoding="utf-8"))
    except Exception:
        return []
    return d.get("predictions", d) if isinstance(d, dict) else d


def check_settlement_flow(report: DoctorReport, heal: bool) -> None:
    """事故史：CI schedule 延迟 2~4h → 昨日比赛悬空未结算，账本断流。"""
    yesterday = _beijing_date(1)
    preds = _load_preds(yesterday)
    has_results = (DAILY / yesterday / "results_sina.json").exists() or \
                  (DAILY / yesterday / "results.json").exists()
    settled_dates: set = set()
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                settled_dates.add(json.loads(line).get("date", ""))
            except Exception:
                continue

    if not preds or not has_results:
        report.add(Finding("data/yesterday", "data", "info",
                           f"昨日({yesterday})无预测或无赛果，跳过结算检查"))
        return

    n_matches = len(preds)
    if yesterday in settled_dates:
        report.add(Finding("data/yesterday", "data", "ok",
                           f"昨日({yesterday})已结算"))
        return

    # 缺赛果先补抓（幂等合并），再结算（引擎侧幂等，已结算场次自动跳过）
    healed = False
    detail = f"昨日({yesterday}) {n_matches} 场有赛果但账本未结算"
    if heal:
        _run([sys.executable, "scripts/fetch_sina_results.py", "--date", yesterday], timeout=180)
        rc, _ = _run([sys.executable, "-m", "engine.main", "--settle", "--date", yesterday],
                     timeout=600)
        healed = rc == 0
        # 结算可能仍因赛果不全而部分完成，重读账本确认
        if LEDGER.exists():
            for line in LEDGER.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("date") == yesterday:
                        healed = True
                        break
                except Exception:
                    continue
    report.add(Finding("data/yesterday", "data",
                       "warn" if healed else "critical",
                       detail + ("→ 已触发补结算" if healed else "（补结算失败，需人工检查）"),
                       healable=True, healed=healed))


def check_missing_results(report: DoctorReport, heal: bool) -> None:
    """事故史：新浪赛果偶发缺失 → 结算无从下手。补抓幂等。"""
    yesterday = _beijing_date(1)
    if not (DAILY / yesterday / "predictions.json").exists():
        return
    if (DAILY / yesterday / "results_sina.json").exists():
        report.add(Finding("data/results", "data", "ok", "昨日赛果文件在"))
        return
    healed = False
    if heal:
        rc, _ = _run([sys.executable, "scripts/fetch_sina_results.py", "--date", yesterday],
                     timeout=180)
        healed = rc == 0
    report.add(Finding("data/results", "data", "warn" if healed else "warn",
                       f"昨日({yesterday})缺 results_sina.json" + ("→ 已补抓" if healed else "（补抓失败）"),
                       healable=True, healed=healed))


# ---------------------------------------------------------------- 数据源活性层

def check_djyy_liveness(report: DoctorReport) -> None:
    """事故史：djyy_prob 静默死亡 6 周（CI 屏蔽期预测无 DJYY→账本全 null）。"""
    rates: list[tuple[str, float]] = []
    for i in range(3):
        day = _beijing_date(i)
        preds = _load_preds(day)
        if not preds or len(preds) < 5:
            continue
        cnt = sum(1 for p in preds if p.get("djyy_enriched"))
        rates.append((day, cnt / len(preds)))
    low = [d for d, r in rates if r < 0.5]
    if len(low) >= 2:
        report.add(Finding(
            "source/djyy", "source", "critical",
            "DJYY 存活率 " + "、".join(f"{d}:{r:.0%}" for d, r in rates)
            + f" ——{len(low)} 天 <50%，第三通道将再次断链"))
    elif rates:
        report.add(Finding("source/djyy", "source", "ok",
                           "DJYY 存活率 " + "、".join(f"{d}:{r:.0%}" for d, r in rates)))


def check_ci_alive(report: DoctorReport, net: bool) -> None:
    """事故史：CI schedule 实际延迟 2~4h 属正常，但彻底停摆（>8h）必须知道。"""
    if not net:
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.github.com/repos/wlrwx/football-engine/actions/workflows/"
            "daily-forecast.yml/runs?per_page=1",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "engine-doctor"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            runs = json.load(resp).get("workflow_runs", [])
        if not runs:
            report.add(Finding("source/ci", "source", "warn", "CI 无运行记录"))
            return
        created = runs[0]["created_at"]  # ISO UTC
        import datetime as _dt
        age_h = (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 3600
        if age_h > 8:
            report.add(Finding("source/ci", "source", "warn",
                               f"最近一次 Daily Forecast 距今 {age_h:.1f}h（>8h，CI 可能停摆）"))
        else:
            report.add(Finding("source/ci", "source", "ok", f"最近 CI 运行 {age_h:.1f}h 前"))
    except Exception as e:
        report.add(Finding("source/ci", "source", "info", f"CI 状态检查跳过（{type(e).__name__}）"))


# ---------------------------------------------------------------- 概率/状态层

def check_probability_health(report: DoctorReport) -> None:
    """事故史：Dixon-Coles 量纲/符号、平局基线语义（PR#3）——概率分布畸变先在这里现形。"""
    today = _beijing_date(0)
    preds = _load_preds(today)
    if not preds or len(preds) < 5:
        return
    bad_sum = [p for p in preds
               if None in (p.get("home_win_prob"), p.get("draw_prob"), p.get("away_win_prob"))
               or abs(p["home_win_prob"] + p["draw_prob"] + p["away_win_prob"] - 1) > 0.02]
    if bad_sum:
        report.add(Finding("model/probsum", "model", "critical",
                           f"今日 {len(bad_sum)}/{len(preds)} 场三路概率和偏离 1（融合层畸变）"))
        return
    draws = [p["draw_prob"] for p in preds
             if p.get("draw_prob") is not None]
    if draws:
        mean_draw = sum(draws) / len(draws)
        # 足球先验 22~30%；出带 = 平局基线/量纲出了问题（PR#3 那类事故的形状）
        if not (0.15 <= mean_draw <= 0.35):
            report.add(Finding("model/draw", "model", "critical",
                               f"今日平局均值 {mean_draw:.1%} 出先验带 [15%,35%]——查 Dixon-Coles/平局基线"))
        else:
            report.add(Finding("model/draw", "model", "ok",
                               f"平局均值 {mean_draw:.1%} 在先验带内"))


def check_state_files(report: DoctorReport) -> None:
    """事故史：配置旋钮是假的（#10）、评估报告从未刷新（#14）——状态文件烂掉先现形。"""
    fw = STATE / "fusion_weights.json"
    if fw.exists():
        try:
            w = json.loads(fw.read_text(encoding="utf-8"))
            weights = w.get("weights", w) if isinstance(w, dict) else {}
            vals = [v for v in weights.values() if isinstance(v, (int, float))]
            if vals and abs(sum(vals) - 1.0) > 0.05:
                report.add(Finding("model/weights", "model", "warn",
                                   f"融合名义权重和 {sum(vals):.3f} ≠ 1（优化器输出畸变）"))
            elif any(v != v for v in vals):  # NaN
                report.add(Finding("model/weights", "model", "warn", "融合权重含 NaN"))
            else:
                report.add(Finding("model/weights", "model", "ok", "融合权重归一"))
        except Exception as e:
            report.add(Finding("model/weights", "model", "warn", f"fusion_weights.json 解析失败: {e}"))


# ---------------------------------------------------------------- 环境/调度层

CRON_JOBS_REQUIRED = ("run_realtime_monitor.sh", "auto_run.sh")


def check_env(report: DoctorReport, heal: bool) -> None:
    """事故史：cron 静默丢失、代理挂掉整夜推送全灭。"""
    try:
        _, crontab = _run(["crontab", "-l"])
        missing = [j for j in CRON_JOBS_REQUIRED if j not in crontab]
        if missing:
            report.add(Finding("env/cron", "env", "critical",
                               f"crontab 缺少关键任务: {missing}（调度层断裂）"))
        else:
            report.add(Finding("env/cron", "env", "ok", "crontab 关键任务在"))
    except Exception:
        report.add(Finding("env/cron", "env", "info", "crontab 读取失败（无 cron?）"))

    # 代理（收编自 auto_run.heal_proxy；推送与 DJYY 都依赖它）
    rc, _ = _run(["nc", "-z", "-w", "2", "127.0.0.1", "7897"])
    if rc != 0:
        healed = False
        if heal:
            _run(["open", "-a", "Clash Verge"])
            for _ in range(12):
                time.sleep(5)
                rc2, _ = _run(["nc", "-z", "-w", "2", "127.0.0.1", "7897"])
                if rc2 == 0:
                    healed = True
                    break
        report.add(Finding("env/proxy", "env", "warn" if healed else "warn",
                           "代理 7897 不通" + ("→ 已拉起 Clash Verge" if healed else "（拉起失败）"),
                           healable=True, healed=healed))
    else:
        report.add(Finding("env/proxy", "env", "ok", "代理 7897 通"))


def check_repo_hygiene(report: DoctorReport) -> None:
    """事故史：仓库 165MB pack 持续膨胀（30 分钟一次数据提交）。"""
    git_dir = REPO / ".git"
    size_mb = sum(f.stat().st_size for f in git_dir.rglob("*") if f.is_file()) / 1e6
    if size_mb > 400:
        report.add(Finding("env/repo", "env", "warn",
                           f".git 体积 {size_mb:.0f}MB（>400MB），需要瘦身（gc/LFS）"))
    else:
        report.add(Finding("env/repo", "env", "ok", f".git 体积 {size_mb:.0f}MB"))


# ---------------------------------------------------------------- 主流程

def run(heal: bool = True, net: bool = True) -> DoctorReport:
    report = DoctorReport()
    check_git_state(report, heal)
    check_missing_results(report, heal)
    check_settlement_flow(report, heal)
    check_djyy_liveness(report)
    check_ci_alive(report, net)
    check_probability_health(report)
    check_state_files(report)
    check_env(report, heal)
    check_repo_hygiene(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="系统医生：统一自愈/体检")
    parser.add_argument("--check", action="store_true", help="只检查不修复")
    parser.add_argument("--no-net", action="store_true", help="跳过外网 API 检查")
    parser.add_argument("--quiet", action="store_true", help="只写报告不打印")
    args = parser.parse_args(argv)

    report = run(heal=not args.check, net=not args.no_net)

    out = STATE / "system_health.json"
    out.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2),
                   encoding="utf-8")

    if not args.quiet:
        for f in report.findings:
            mark = {"ok": "✓", "info": "·", "warn": "⚠", "critical": "✗"}[f.severity]
            suffix = "（已自愈）" if f.healed else ""
            print(f"  {mark} [{f.layer}] {f.detail}{suffix}")
        print(f"系统医生: {len(report.findings)} 项，"
              f"critical {report.n_critical}，自愈 {report.n_healed}")

    return 2 if report.n_critical else 0


if __name__ == "__main__":
    sys.exit(main())
