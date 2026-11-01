#!/usr/bin/env python3
"""E8/E10 数据兜底对账（CI 可跑，无需 JWT/网络）。

2026-10-23：本机 22:40 的对账自动化在电脑关机时不会跑。这脚本只读仓库里
已入库的数据，给 daily-forecast 的 step summary 用：
  - 速度/预计 n：复用 experiment_pace 的冻结分池与截止规则
  - 打平所需命中率、已结算命中率/ROI：用 results_sina 的比分 + challenge_review
    的 parse_pick/judge_hit（不另写一套解析规则）
  - 账目：最后一次入库的「已预测」快照 vs 本地 records，并打印快照龄

拿不到的：零合格轮归因（池/匹配/候选/最高 ev_site 只在本机 duty.log）、
实时服务端真值（JWT 约 20h，只能由本机续期）。报告里明确写出来，不假装有。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state"
DAILY = ROOT / "data" / "daily"

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import experiment_pace as ep  # noqa: E402
from challenge_review import (  # noqa: E402
    judge_hit, match_pair_review, parse_pick, resolve_record)

BJT = timezone(timedelta(hours=8))


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _records() -> list[dict]:
    st = _load(STATE / "challenge_state.json", {})
    return st.get("records") or []


def _dates_for(rec: dict) -> list[str]:
    raw = str(rec.get("submitted_at") or "")[:10]
    try:
        d0 = date.fromisoformat(raw)
    except ValueError:
        return []
    return [(d0 + timedelta(days=i)).isoformat() for i in range(3)]


def _scores(dates: list[str]) -> list[tuple]:
    idx = []
    for d in dates:
        rows = _load(DAILY / d / "results_sina.json", [])
        for x in rows if isinstance(rows, list) else []:
            hs, as_ = x.get("home_score"), x.get("away_score")
            h, a = x.get("home_team"), x.get("away_team")
            if hs is None or as_ is None or not h or not a:
                continue
            idx.append((h, a, int(hs), int(as_)))
    return idx


def _lookup_score(idx: list[tuple], ph: str, pa: str):
    for h, a, hs, as_ in idx:
        if match_pair_review(h, a, ph, pa):
            return hs, as_
    return None


def _load_ctx():
    """账本（权威 actual_idx）+ 所有预测，用于把站点译名解析到 match_id。"""
    ledger = {}
    p = STATE / "review_ledger.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = r.get("match_id")
            if mid and (mid not in ledger or
                        (ledger[mid].get("actual_idx") is None
                         and r.get("actual_idx") is not None)):
                ledger[mid] = r
    pred_rows = []
    if DAILY.exists():
        for d in sorted(x.name for x in DAILY.iterdir() if x.is_dir()):
            raw = _load(DAILY / d / "predictions.json", [])
            for x in (raw if isinstance(raw, list) else raw.get("predictions", [])):
                pred_rows.append((d, x))
    return ledger, pred_rows


def _judge(rec: dict, ctx) -> bool | None:
    ledger, pred_rows = ctx
    pred = resolve_record(rec, pred_rows)
    if not pred:
        return None
    mid = pred.get("match_id")
    dates = _dates_for(rec) or ([str(mid)[:10]] if mid else [])
    sc = _lookup_score(_scores(dates), pred.get("home_team", ""), pred.get("away_team", ""))
    hs, as_ = sc if sc else (None, None)
    lr = ledger.get(mid) if mid else None
    actual = lr.get("actual_idx") if lr else None
    if actual is None:
        if hs is None:
            return None
        actual = 0 if hs > as_ else (1 if hs == as_ else 2)
    return judge_hit(parse_pick(str(rec.get("pick") or "")), actual, hs, as_)


def pool_stats(records: list[dict], exp: str) -> dict:
    ctx = _load_ctx()
    pool = ep.pool(records, exp)
    odds = [r.get("site_odds") for r in pool if isinstance(r.get("site_odds"), (int, float))]
    break_even = sum(1 / o for o in odds) / len(odds) if odds else None
    judged, hits, roi = 0, 0, 0.0
    for r in pool:
        hit = _judge(r, ctx)
        if hit is None:
            continue
        judged += 1
        hits += int(hit)
        if isinstance(r.get("site_odds"), (int, float)):
            roi += hit * r["site_odds"] - 1
    return {
        "n": len(pool), "break_even": break_even,
        "judged": judged, "hits": hits,
        "hit_rate": (hits / judged) if judged else None,
        "roi": (roi / judged) if judged else None,
    }


def snapshot_gap_line(days: int = 7) -> str:
    """逐时快照完整性：断网/关机最直观的证据，且 CI 也能看见（不依赖 duty.log）。"""
    base = STATE / "njstats"
    today = datetime.now(BJT).date()
    want = [(today - timedelta(days=i)).isoformat() for i in range(days)][::-1]
    parts = []
    for kind in ("site_odds", "my_predictions", "community"):
        d = base / kind
        have = {p.stem for p in d.glob("*.json")} if d.exists() else set()
        miss = [x for x in want if x not in have]
        parts.append(f"{kind} 缺{len(miss)}"
                     + (f"（{'、'.join(m[5:] for m in miss)}）" if miss else ""))
    return f"逐时快照(近{days}天): " + " | ".join(parts)


def snapshot_line() -> str:
    d = STATE / "njstats" / "my_predictions"
    files = sorted(d.glob("*.json")) if d.exists() else []
    if not files:
        return "账目: 无已入库的「已预测」快照"
    snap = _load(files[-1], {})
    recs = snap.get("records") or []
    age_h = None
    try:
        ts = datetime.strptime(snap.get("snapshot_at", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
        age_h = (datetime.now(BJT) - ts).total_seconds() / 3600
    except (ValueError, TypeError):
        pass
    repo = {str(r.get("match") or "") for r in _records()}
    missing = [f"{s.get('home')} vs {s.get('away')}" for s in recs
               if f"{s.get('home')} vs {s.get('away')}" not in repo]
    age = f"{age_h:.1f}h" if age_h is not None else "龄未知"
    stale = "（可能过期）" if age_h is not None and age_h > 12 else ""
    names = "、".join(missing[:3]) + (" 等" if len(missing) > 3 else "") if missing else "无"
    return (f"账目: 快照 {len(recs)} 条/{age}{stale} vs 仓库 {len(repo)}；"
            f"仓库未记 {len(missing)} 条：{names}")


def _fmt_pool(name: str, s: dict, today: date, records: list[dict]) -> str:
    pace = ep.format_line(ep.summarize(records, name, today))
    if s["judged"]:
        hit = f"已结算 {s['judged']}/{s['n']} 命中 {s['hits']} ({s['hit_rate']:.1%})"
        roi = f"ROI {s['roi']:+.1%}"
    else:
        hit, roi = f"已结算 0/{s['n']}（暂无命中率）", "ROI —"
    be = f"打平线 {s['break_even']:.1%}" if s["break_even"] is not None else "打平线 —"
    return f"{pace}｜{hit}｜{roi}｜{be}"


def main(argv=None) -> int:
    records = _records()
    today = datetime.now(BJT).date()
    print("=== E8/E10 对账（数据兜底，无 JWT/无实时服务端）===")
    for exp in ("E8", "E10"):
        print(_fmt_pool(exp, pool_stats(records, exp), today, records))
    print(snapshot_gap_line())
    print(snapshot_line())
    print("归因: 零合格轮需本机 duty.log（CI 不可得）；实时真值需本机续 JWT，均未包含。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
