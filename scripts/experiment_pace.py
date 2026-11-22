#!/usr/bin/env python3
"""E8/E10 攒注速度与截止风险巡检（只读）。

背景：11-22 前 E8 预计只有 ~65 注（历史 10-13~10-20 回测约 2.1 注/天），
方差足以掉到 60 以下；而原判据的失败分支是「回退 edge」，edge 已实测
不可达。2026-10-22 已预登记失败分支：11-22 若 n<60，同一门槛延期到
12-31，不回退 edge。本脚本只报数，不改 policy/state/门槛。

用法: ./.venv/bin/python scripts/experiment_pace.py [--today YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state" / "challenge_state.json"

START = date(2026, 10, 22)       # ev_site 闸门启用日（10:35）
DEADLINE = date(2026, 11, 22)    # 原定截止
EXTENSION = date(2026, 12, 31)   # 预登记的延期截止
TARGET_N = 60
MIN_OBS_DAYS = 7                 # 少于此天数不外推


def _day(rec: dict) -> date | None:
    raw = rec.get("submitted_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).date()
    except (TypeError, ValueError):
        return None


def pool(recs: list[dict], exp: str) -> list[dict]:
    """按实验分池：只认 ev_site 闸门、实验开始后的记录。"""
    out = []
    for r in recs:
        if r.get("gate") != "ev_site":
            continue
        d = _day(r)
        if d is None or d < START:
            continue
        is_e10 = r.get("exp") == "E10"
        if (exp == "E10") != is_e10:
            continue
        out.append(r)
    return out


def summarize(recs: list[dict], exp: str, today: date) -> dict:
    n = len(pool(recs, exp))
    elapsed = max(1, (today - START).days + 1)
    days_left = max(0, (DEADLINE - today).days)
    ext_left = max(0, (EXTENSION - today).days)
    pace = n / elapsed
    projected = n + pace * days_left
    ext_projected = n + pace * ext_left
    required = (TARGET_N - n) / days_left if days_left > 0 else None

    # 已终判的实验不再喊「可收尾」——E10 于 2026-11-16 满 60 注按冻结判据终判失败
    # （ROI +6.07% 但单侧 p=0.289≥0.10），旧筛已恢复；n≥60 对它只意味着历史记录。
    if exp == "E10":
        status = "已终判关闭（11-16 失败：p=0.289≥0.10），仅历史口径"
    elif n >= TARGET_N:
        status = "已达 n=60，可按判据收尾"
    elif today > EXTENSION:
        status = "延期届满仍 n<60 → 证据不足/无结论（不自动回退 edge）"
    elif today > DEADLINE:
        status = "已过 11-22，按预登记延期至 12-31"
    elif today >= DEADLINE:
        status = "已到 11-22 且 n<60 → 执行预登记延期至 12-31"
    elif elapsed < MIN_OBS_DAYS:
        status = "观察期（不足 7 天，暂不外推）"
    elif projected < TARGET_N:
        status = "预计 11-22 不足 60 → 触发预登记延期至 12-31"
    else:
        status = "在轨"

    return {
        "exp": exp, "n": n, "pace": pace, "elapsed": elapsed,
        "days_left": days_left, "required": required,
        "projected": projected, "ext_projected": ext_projected,
        "status": status,
    }


def format_line(s: dict) -> str:
    line = (f"{s['exp']}: n={s['n']}/{TARGET_N} "
            f"速度{s['pace']:.2f}/天（已观察{s['elapsed']}天）")
    if s["days_left"] > 0:
        line += f" 距11-22 {s['days_left']}天"
        if s["required"] is not None:
            line += f" 需{s['required']:.2f}/天"
    if s["status"].startswith("在轨") or "触发" in s["status"]:
        line += f" 预计11-22 n≈{s['projected']:.0f}"
    if "延期" in s["status"]:
        line += f"；延期至12-31预计 n≈{s['ext_projected']:.0f}"
    line += f"｜{s['status']}"
    return line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", help="覆盖当天日期（测试用）")
    args = ap.parse_args(argv)
    today = (datetime.fromisoformat(args.today).date() if args.today
             else datetime.now().date())
    state = json.loads(STATE.read_text(encoding="utf-8"))
    recs = state.get("records", [])
    for exp in ("E8", "E10"):
        print(format_line(summarize(recs, exp, today)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
