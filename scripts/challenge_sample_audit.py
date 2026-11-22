#!/usr/bin/env python3
"""E8/E10 样本完整性只读审计（2026-11-17 每日探索 · scout）。

五个轴，全部对着已入库的数据量化，不改判据：

  1. 逐时快照缺口   data/state/njstats/{site_odds,my_predictions,community}/<date>.json
  2. 结算滞后       records → review_ledger / results_sina 的可判性与落盘时刻
  3. 重复计入       sid / 队名对 / 别名子串 三个粒度的重复注单
  4. 让球走盘       parse_pick/judge_hit 在亚盘整数线上把走盘判成失败
  5. 服务端真值差异 my_predictions（msgId=11）vs challenge_state.records

只读承诺（tests/test_challenge_sample_audit.py 逐条钉住）：
  · 不 import engine.*——与 challenge_review 同侧，见 docs/CHALLENGE_SUBSYSTEM.md
  · 不写 data/state/**：`--json` 明确拒绝落在 data/state 下
  · 不改门槛、闸门、判据、policy；本脚本只读数、报数、亮红旗

用法：
  python3 scripts/challenge_sample_audit.py                # 文件层面的轴（秒级）
  python3 scripts/challenge_sample_audit.py --history      # 加算 git 里的落盘时刻（~1-2 分钟）
  python3 scripts/challenge_sample_audit.py --json /tmp/audit.json
  python3 scripts/challenge_sample_audit.py --ci           # 有红旗时退出码 1（尚未接进任何 workflow）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_PATH = ROOT / "data" / "state" / "challenge_state.json"
NJ_DIR = ROOT / "data" / "state" / "njstats"
BJT = timezone(timedelta(hours=8))

# njstats_api_submit.my_predictions() 写死 page=1, size=60 且不翻页。
# 这个常量是「服务端真值被截断」的判据，不是站点上限的断言。
TRUTH_PAGE_SIZE = 60
# 「收盘价」窗口取开赛前 ≤6h。注意实测连日内序列完整的日子，最后一次上板
# 也只到开赛前 median ~16h（诊断文档 §2.3）——站点提前把场次移出可提交板，
# 所以这个窗口内命中的注很少，那本身就是结论，不是脚本没找对。
CLOSE_WINDOW_H = 6
E8_START = date(2026, 10, 22)          # experiment_pace.START，ev_site 闸门启用日


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_records() -> list[dict]:
    return _load(STATE_PATH, {}).get("records") or []


def _match_pair(match: str) -> tuple[str, str]:
    parts = str(match or "").split(" vs ")
    if len(parts) != 2:
        return "", ""
    return parts[0].strip(), parts[1].strip()


def _ts(text: str | None):
    """'YYYY-MM-DD HH:MM:SS' → 北京时 aware datetime。"""
    try:
        return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 注单判定：口径全部借用生产实现，不另写一套
# --------------------------------------------------------------------------

def judgments(records: list[dict]) -> list[dict]:
    """每条 record 一行：命中/未判/无精确比分/让球线。

    actual_idx 走 review_ledger（权威），比分走 results_sina（与
    challenge_review/challenge_reconcile 同一份 join），让球判定用
    challenge_review.judge_hit——包括它把整数线走盘判成 False 的那条规则。
    """
    import challenge_reconcile as cr
    from challenge_review import judge_hit, parse_pick

    ledger, pred_rows = cr._load_ctx()
    out = []
    for rec in records:
        row = {"match": rec.get("match"), "sid": rec.get("sid"),
               "exp": rec.get("exp"), "gate": rec.get("gate"),
               "submitted_at": rec.get("submitted_at"),
               "site_odds": rec.get("site_odds"),
               "resolved": False, "match_id": None, "score": False, "actual": None, "hit": None,
               "play": parse_pick(str(rec.get("pick") or "")).get("play"),
               "line": parse_pick(str(rec.get("pick") or "")).get("line")}
        pred = cr.resolve_record(rec, pred_rows)
        if not pred:
            out.append(row)
            continue
        mid = pred.get("match_id")
        row["resolved"] = bool(mid)
        row["match_id"] = mid
        lr = ledger.get(mid) if mid else None
        sc = cr._lookup_score(cr._scores(cr._dates_for(rec) or ([str(mid)[:10]] if mid else [])),
                              pred.get("home_team", ""), pred.get("away_team", ""))
        if sc:
            row["score"] = True
            row["score_line"] = sc
        actual = lr.get("actual_idx") if lr else None
        if actual is None and sc:
            actual = 0 if sc[0] > sc[1] else (1 if sc[0] == sc[1] else 2)
        row["actual"] = actual
        if actual is not None:
            row["hit"] = judge_hit(parse_pick(str(rec.get("pick") or "")), actual,
                                   sc[0] if sc else None, sc[1] if sc else None)
        out.append(row)
    return out


def pool_rows(records: list[dict], judged: list[dict], exp: str) -> list[dict]:
    """取某实验池对应的判定行（按对象身份对齐，不重新实现分池口径）。"""
    import experiment_pace as ep

    poolset = {id(r) for r in ep.pool(records, exp)}
    return [j for j, r in zip(judged, records) if id(r) in poolset]


# --------------------------------------------------------------------------
# 轴 3：重复计入
# --------------------------------------------------------------------------

def duplicate_bets(records: list[dict]) -> dict:
    """三个粒度：sid 重复、同场原文重复、别名/子串同场（一次两注的那种）。"""
    from challenge_review import match_pair_review

    sids = [r["sid"] for r in records if r.get("sid")]
    sid_dups = {s: n for s, n in Counter(sids).items() if n > 1}
    named = []
    for r in records:
        h, a = _match_pair(r.get("match"))
        if h and a:
            named.append((r.get("sid"), h, a))
    exact_dups: list[list] = []
    seen_exact: dict[tuple, list] = defaultdict(list)
    for sid, h, a in named:
        seen_exact[(h, a)].append(sid)
    for key, vals in seen_exact.items():
        if len(vals) > 1:
            exact_dups.append([*key, *vals])
    fuzzy: list[list] = []
    for i in range(len(named)):
        for j in range(i + 1, len(named)):
            _, h1, a1 = named[i]
            _, h2, a2 = named[j]
            if (h1, a1) != (h2, a2) and match_pair_review(h1, a1, h2, a2):
                fuzzy.append([named[i][0], f"{h1} vs {a1}", named[j][0], f"{h2} vs {a2}"])
    return {"n_records": len(records), "n_sid": len(sids),
            "sid_dups": sid_dups, "exact_fixture_dups": exact_dups,
            "alias_fixture_dups": fuzzy}


# --------------------------------------------------------------------------
# 轴 5：服务端真值 vs 本地 records
# --------------------------------------------------------------------------

def load_truth_snapshots() -> list[dict]:
    out = []
    for f in sorted((NJ_DIR / "my_predictions").glob("*.json")):
        d = _load(f, {})
        if not isinstance(d, dict):
            continue
        out.append({"day": f.stem, "snapshot_at": d.get("snapshot_at"),
                    "count": d.get("count"), "local_records": d.get("local_records"),
                    "records": d.get("records") or []})
    return out


def server_truth_vs_records(records: list[dict], snaps: list[dict]) -> dict:
    """可见性 + 双向差额。方向很重要：生产对账只查「服务端有、仓库没有」。"""
    local_sid = {r["sid"] for r in records if r.get("sid")}
    local_names = {" ".join(str(r.get("match") or "").split()) for r in records}
    union: dict[str, dict] = {}
    counts = []
    for s in snaps:
        c = 0
        for r in s["records"]:
            union[r["sid"]] = r
            c += 1
        counts.append((s["day"], c))
    newest = max((s["day"] for s in snaps), default=None)
    newest_set = {r["sid"] for s in snaps if s["day"] == newest for r in s["records"]}
    pinned = [d for d, c in counts if c == TRUTH_PAGE_SIZE]
    server_only = [r for sid, r in union.items() if sid not in local_sid]
    server_only_named = [r for r in server_only
                         if " ".join(f"{r.get('home')} vs {r.get('away')}".split()) in local_names]
    local_only = sorted(sid for sid in local_sid if sid not in union)
    # 「是不是只返回最近 60 条」的证据：可见/隐身两组注单的提交时间区间应当严格相邻
    sub_of = {r["sid"]: str(r.get("submitted_at") or "") for r in records if r.get("sid")}
    vis_t = sorted(t for sid, t in sub_of.items() if sid in newest_set and t)
    inv_t = sorted(t for sid, t in sub_of.items() if sid not in newest_set and t)
    odds_agree = odds_differ = odds_unknown = 0
    for sid, s in union.items():
        loc = next((r for r in records if r.get("sid") == sid), None)
        if not loc or not isinstance(loc.get("site_odds"), (int, float)) or not s.get("odds"):
            odds_unknown += 1
            continue
        try:
            so = float(s["odds"])
        except (TypeError, ValueError):
            odds_unknown += 1
            continue
        if abs(so - float(loc["site_odds"])) < 1e-9:
            odds_agree += 1
        else:
            odds_differ += 1
    return {
        "n_records": len(records),
        "n_sid_local": len(local_sid),
        "n_snapshots": len(snaps),
        "count_per_snapshot": counts,
        "max_count": max([c for _, c in counts], default=0),
        "pinned_days_at_page_size": len(pinned),
        "truncated_suspected": bool(len(pinned) >= 3 and len(local_sid) > TRUTH_PAGE_SIZE),
        "union_server_sids": len(union),
        "server_only": [{"sid": r.get("sid"), "match": f"{r.get('home')} vs {r.get('away')}",
                         "market": r.get("market"), "dir": r.get("dir"), "odds": r.get("odds")}
                        for r in server_only],
        "server_only_matched_by_name": len(server_only_named),
        "server_only_unmatched": [{"sid": r.get("sid"),
                                   "match": f"{r.get('home')} vs {r.get('away')}",
                                   "market": r.get("market")}
                                  for r in server_only if r not in server_only_named],
        "local_only_sids": local_only,
        "newest_snapshot_visible": len(newest_set),
        "newest_snapshot_covers_pct": (len(newest_set & local_sid) / len(local_sid)) if local_sid else None,
        "invisible_to_live_check": len(local_sid - newest_set),
        "visible_submitted_range": [vis_t[0], vis_t[-1]] if vis_t else None,
        "invisible_submitted_range": [inv_t[0], inv_t[-1]] if inv_t else None,
        "bet_odds_agree_with_server": odds_agree,
        "bet_odds_differ_from_server": odds_differ,
        "bet_odds_uncomparable": odds_unknown,
        "server_market_codes": dict(Counter(r.get("market") for r in union.values())),
    }


def market_code_conflicts(records: list[dict], snaps: list[dict]) -> dict:
    """本地按 1x2 判、服务端却记在非 1x2 市场码上的注——这是「让球被当成胜平负」的真实入口。

    码 1 = 胜平负（CI 走的就是它：`api.submit(jwt, sid, 1, dir+1)`）。
    码 2/4/5 的语义仓库里**没有**权威解释（README 只逆向到 msgId/字段形状），
    所以本函数只报「有 N 注的判定市场与服务端市场码不一致」，不替站点猜语义。
    """
    from challenge_review import parse_pick

    union: dict[str, dict] = {}
    for s in snaps:
        for r in s["records"]:
            union[r["sid"]] = r
    by_sid = {r.get("sid"): r for r in records if r.get("sid")}
    by_name = {" ".join(str(r.get("match") or "").split()): r for r in records}
    conflicts, labels, unregistered = [], [], []
    for sid, srv in union.items():
        code = srv.get("market")
        if code in (None, 1):
            continue
        loc = by_sid.get(sid)
        nm = " ".join(f"{srv.get('home')} vs {srv.get('away')}".split())
        loc = loc or by_name.get(nm)
        item = {"sid": sid, "server_market": code, "server_dir": srv.get("dir"),
                "server_odds": srv.get("odds"), "match": nm}
        if loc is None:
            unregistered.append(item)
            continue
        item.update({"local_pick": loc.get("pick"), "local_market_field": loc.get("market"),
                     "local_play": parse_pick(str(loc.get("pick") or "")).get("play"),
                     "local_sid": loc.get("sid"), "local_gate": loc.get("gate"),
                     "local_exp": loc.get("exp")})
        if item["local_play"] == "1x2":
            conflicts.append(item)          # 判定用的市场与结算方不一致（真风险）
        elif loc.get("market") == "1x2":
            labels.append(item)             # 只是字段标错，判定走的是 pick（让球按比分判）
    in_pool = [c for c in conflicts if c.get("local_gate") == "ev_site"]
    return {"non_1x2_server_bets": len([r for r in union.values()
                                        if r.get("market") not in (None, 1)]),
            "union_size": len(union),
            "judged_as_1x2_but_server_other_market": conflicts,
            "n_conflicts": len(conflicts), "n_conflicts_in_ev_site_pool": len(in_pool),
            "label_only_mismatch": labels,
            "server_only_unregistered": unregistered}


# --------------------------------------------------------------------------
# 轴 1：逐时快照缺口
# --------------------------------------------------------------------------

SNAPSHOT_KINDS = ("site_odds", "my_predictions", "community", "strategies")


def day_range(since: date, until: date) -> list[str]:
    return [(since + timedelta(days=i)).isoformat() for i in range((until - since).days + 1)]


def snapshot_gaps(since: date, until: date) -> dict:
    """缺天要按**每个快照自己的启用日**起算，否则 11-08 才上线的 strategies 会把
    整段历史报成缺口（那就是噪音，会把真缺口淹掉）。"""
    out = {}
    for kind in SNAPSHOT_KINDS:
        d = NJ_DIR / kind
        have = {p.stem for p in d.glob("*.json")} if d.exists() else set()
        start = since
        if have:
            start = max(since, date.fromisoformat(min(have)))
        want = day_range(start, until)
        present = [x for x in want if x in have]
        missing = [x for x in want if x not in have]
        hours = []
        for x in present:
            sa = _ts(_load(d / f"{x}.json", {}).get("snapshot_at"))
            hours.append(sa.hour * 60 + sa.minute if sa else None)
        kept = [h for h in hours if h is not None]
        out[kind] = {
            "first_file": min(have) if have else None,
            "want_days": len(want), "present_days": len(present),
            "missing_days": missing,
            "coverage_pct": (len(present) / len(want)) if want else None,
            "kept_snapshot_hour_bjt_median": (sorted(kept)[len(kept) // 2] / 60) if kept else None,
            # 覆盖式落盘：一天一份，留的是当天最后一次，日内序列不在工作树里
            "overwrite_keeps_last_run_of_day": bool(kept) and (sorted(kept)[len(kept) // 2] > 20 * 60),
        }
    return out


# --------------------------------------------------------------------------
# 轴 2：结算滞后 / 可判性
# --------------------------------------------------------------------------

def settlement_coverage(records: list[dict], judged: list[dict], today: date) -> dict:
    """pace 的 n=已提交，判据要的是 n=已结算。两者不等就是白丢/虚高。"""
    import experiment_pace as ep

    res = {}
    for exp in ("E8", "E10"):
        pool = ep.pool(records, exp)
        rows = pool_rows(records, judged, exp)
        unjudged = [j for j in rows if j.get("hit") is None]
        no_score = [j for j in rows if j.get("hit") is not None and not j.get("score")]
        res[exp] = {"n_pool": len(pool), "n_judged": len(rows) - len(unjudged),
                    "n_unsettled_in_n": len(unjudged),
                    "unsettled_matches": [j["match"] for j in unjudged],
                    "judged_without_exact_score": len(no_score),
                    "settled_no_score_matches": [j["match"] for j in no_score],
                    "reaches_target_60": len(pool) >= ep.TARGET_N,
                    "target_reached_while_unsettled": len(pool) >= ep.TARGET_N and bool(unjudged),
                    "pace_line": ep.format_line(ep.summarize(records, exp, today))}
    return res


def pool_deadline_drift(records: list[dict]) -> dict:
    """池子越过登记的到期条件（满 60 注）之后还在往里收注的窗口。

    口径来自 `docs/EXPERIMENTS.md` E8/E10 两行的「判定日：2026-11-22 或满 60 注
    （先到为准）」——所以「第 60 注的提交时刻」就是先到时点，之后进的注都属于
    判据已到期后新增，登记行数与复算池数会对不上。本函数只数，不动任何东西。
    """
    import experiment_pace as ep

    out = {}
    for exp in ("E8", "E10"):
        pool = sorted(ep.pool(records, exp), key=lambda r: str(r.get("submitted_at") or ""))
        n = len(pool)
        if n < ep.TARGET_N:
            out[exp] = {"n_pool": n, "target_n": ep.TARGET_N, "crossed_at": None,
                        "bets_after_target": 0, "after_target_examples": []}
            continue
        crossed_at = str(pool[ep.TARGET_N - 1].get("submitted_at") or "")
        # 一轮最多提交 K=6 注，所以「满 60 先到」天然会一次越线好几注——
        # 这个数解释了登记行的 63 与阈值 60 为什么差 3
        same_ts = sum(1 for r in pool if str(r.get("submitted_at") or "") == crossed_at)
        after = [(r.get("submitted_at"), r.get("match"), r.get("pick"))
                 for r in pool[ep.TARGET_N:] if str(r.get("submitted_at") or "") > crossed_at]
        out[exp] = {"n_pool": n, "target_n": ep.TARGET_N, "crossed_at": crossed_at,
                    "bets_at_crossing_timestamp": same_ts,
                    "bets_after_target": len(after), "after_target_examples": after[:8]}
    return out


# --------------------------------------------------------------------------
# 轴 4：让球走盘
# --------------------------------------------------------------------------

def handicap_exposure(records: list[dict], judged: list[dict]) -> dict:
    """让球注的量、整数线（可能走盘）的量，以及走盘在现有规则下算赢还是算输。"""
    from challenge_review import judge_hit, parse_pick

    hc = [(r, j) for r, j in zip(records, judged) if j.get("play") == "handicap"]
    integer_line = [(r, j) for r, j in hc if (j.get("line") or 0) % 1 == 0]
    # 净胜恰好等于线 = 走盘：主让 1 球且 2-1、客受让 1 球且 1-0
    push = judge_hit(parse_pick("主队-1@0.95"), 0, 2, 1)
    push_away = judge_hit(parse_pick("客+1@0.95"), 0, 1, 0)
    pool_hc = [j for j in judged if j.get("play") == "handicap" and j.get("gate") == "ev_site"]
    missing = [j for j in judged if j.get("hit") is None and j.get("play") == "handicap"]
    pushes = [j for j in judged
              if j.get("play") == "handicap" and j.get("score_line")
              and abs(abs(j["score_line"][0] - j["score_line"][1]) - (j.get("line") or -1)) < 1e-9]
    return {"n_handicap_bets": len(hc),
            "handicap_matches": [r.get("match") for r, _ in hc],
            "n_integer_line": len(integer_line),
            "push_counted_as_hit": push,
            "push_counted_as_hit_away": push_away,
            "n_handicap_in_ev_site_pool": len(pool_hc),
            "n_handicap_unjudgeable": len(missing),
            "n_actual_push_bets": len(pushes),
            "push_bets": [j.get("match") for j in pushes],
            "n_settled_without_exact_score": sum(1 for j in judged
                                                 if j.get("hit") is not None and not j.get("score"))}


# --------------------------------------------------------------------------
# 记账口径（不改判据，只把两个分母摊开）
# --------------------------------------------------------------------------

def payout_math(records: list[dict], judged: list[dict]) -> dict:
    import experiment_pace as ep

    out = {}
    for exp in ("E8", "E10"):
        rows = pool_rows(records, judged, exp)
        pool = ep.pool(records, exp)
        odds = [r.get("site_odds") for r in pool if isinstance(r.get("site_odds"), (int, float))]
        n_judged = sum(1 for j in rows if j.get("hit") is not None)
        priced_judged = sum(1 for j, r in zip(rows, pool)
                            if j.get("hit") is not None
                            and isinstance(r.get("site_odds"), (int, float)))
        # 三个分母要分开看：n_pool（已提交）、judged（已结算）、
        # 而生产面板的「打平线」是在 n_pool 上算的，命中率/ROI 在 judged 上算——
        # 未结算注一多，同一行里的两个率就不是同一个池子。
        odds_judged = [r.get("site_odds") for j, r in zip(rows, pool)
                       if j.get("hit") is not None and isinstance(r.get("site_odds"), (int, float))]
        mean_imp = sum(1 / o for o in odds) / len(odds) if odds else None
        mean_imp_judged = (sum(1 / o for o in odds_judged) / len(odds_judged)
                           if odds_judged else None)
        inv_mean = 1 / (sum(odds) / len(odds)) if odds else None
        drift_pp = ((mean_imp - mean_imp_judged) * 100
                    if mean_imp is not None and mean_imp_judged is not None else None)
        out[exp] = {"n_pool": len(pool), "n_priced": len(odds),
                    "break_even_printed_on_pool": mean_imp,
                    "break_even_on_settled": mean_imp_judged,
                    "break_even_denominator_drift_pp": drift_pp,
                    "judged": n_judged, "priced_judged": priced_judged,
                    "roi_denominator_gap": n_judged - priced_judged,
                    "break_even_flat_stake": inv_mean,
                    "break_even_overstated_pp": (mean_imp - inv_mean) * 100
                    if mean_imp is not None and inv_mean is not None else None}
    return out


# --------------------------------------------------------------------------
# --history：把「落盘时刻」这个维度补上（只用 git，不碰网络）
# --------------------------------------------------------------------------

def _git_log_epochs(rel: str, since: str | None = None) -> list[tuple[int, str]]:
    cmd = ["git", "-C", str(ROOT), "log", "--format=%ct|%H", "--reverse", "--", rel]
    if since:
        cmd[4:4] = [f"--since={since}"]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        return []
    return [(int(a), b) for a, b in (ln.split("|") for ln in res.stdout.split())]


def _git_show_json(sha: str, rel: str):
    res = subprocess.run(["git", "-C", str(ROOT), "show", f"{sha}:{rel}"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except ValueError:
        return None


def score_arrival_lag(records: list[dict], since: str = "2026-11-03") -> dict:
    """开赛 → 比分第一次出现在 git 里的小时数。

    口径是**上界**：day-file 只有 N 次提交时，比分可能在两次提交之间就已在
    本机落盘，git 看不见。所以 max 能拿来证「不迟于 X」，min 没有意义。
    """
    import challenge_reconcile as cr
    import experiment_pace as ep
    from challenge_review import match_pair_review

    _, pred_rows = cr._load_ctx()
    series: dict[str, list] = {}
    per_day: dict[str, list[float]] = defaultdict(list)
    depth: dict[str, int] = {}
    pool = ep.pool(records, "E8") + ep.pool(records, "E10")
    n_ok = n_skip = n_before = 0
    for rec in pool:
        pred = cr.resolve_record(rec, pred_rows)
        mid = (pred or {}).get("match_id")
        if not mid:
            n_skip += 1
            continue
        day = str(mid)[:10]
        if day < since:
            n_before += 1
            continue
        rel = f"data/daily/{day}/results_sina.json"
        if rel not in series:
            cs = _git_log_epochs(rel)
            series[rel] = [(ct, _git_show_json(sha, rel)) for ct, sha in cs]
            depth[day] = len(cs)
        ht, at = pred.get("home_team", ""), pred.get("away_team", "")
        for ct, payload in series[rel]:
            hit = next((x for x in (payload if isinstance(payload, list) else [])
                        if x.get("home_score") is not None and x.get("away_score") is not None
                        and match_pair_review(x.get("home_team", ""), x.get("away_team", ""), ht, at)),
                       None)
            if hit:
                kt = _ts(hit.get("match_time"))
                if kt:
                    per_day[day].append((datetime.fromtimestamp(ct, BJT) - kt).total_seconds() / 3600)
                    n_ok += 1
                break
        else:
            n_skip += 1
    la = sorted(v for vals in per_day.values() for v in vals)
    med = {d: round(sorted(v)[len(v) // 2], 1) for d, v in per_day.items()}
    return {"bets_in_pool": len(pool), "bets_measured": n_ok, "bets_unresolved": n_skip,
            "since": since,
            "day_files_measured": len(per_day),
            "day_file_commit_depth": dict(sorted(depth.items())),
            "lag_h_median": la[len(la) // 2] if la else None,
            "lag_h_p90": la[int(0.9 * (len(la) - 1))] if la else None,
            "lag_h_max": la[-1] if la else None,
            "per_day_median_lag": dict(sorted(med.items())),
            # 赛日早于 since（日文件在 git 里只有 11-04 那一次批次提交）→ 结算时点测不到
            "bets_unauditable_before_since": n_before}


def close_window_stats(vals: list[float], window_h: float | None = None) -> dict:
    """最后一次上板距开赛的分布 + 落在收盘窗口内的比例（纯函数，可测）。

    median 取排序后 v[n//2]——偶数样本偏上，与 lag 分位同一口径。
    """
    win = CLOSE_WINDOW_H if window_h is None else window_h
    v = sorted(vals)
    hit = sum(1 for x in v if x <= win)
    return {"n": len(v), "median_h": v[len(v) // 2] if v else None,
            "within_close_window": hit,
            "within_close_window_pct": (hit / len(v)) if v else None}


def odds_close_coverage(records: list[dict]) -> dict:
    """站点赔率的可见窗口：sid 第一次/最后一次出现在板上距开赛多久。

    回答的是「快照能不能当收盘价用」。逐时序列只在 git 里有——工作树每天
    一份、被当次运行覆盖，所以这里必须走 --history。
    """
    import experiment_pace as ep

    rel = "data/state/njstats/site_odds/"
    hist: dict[str, dict] = defaultdict(dict)
    days: dict[str, set] = defaultdict(set)
    for sha in [h for _, h in _git_log_epochs(rel)]:
        files = subprocess.run(["git", "-C", str(ROOT), "show", "--name-only", "--format=", sha, "--", rel],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace").stdout.split()
        for f in files:
            d = _git_show_json(sha, f)
            sa = (d or {}).get("snapshot_at")
            if not sa:
                continue
            days[f[-15:-5]].add(sa)
            for m in d.get("matches") or []:
                if m.get("odds"):
                    hist[m["sid"]][sa] = m.get("kick_ts")
    pool = ep.pool(records, "E8") + ep.pool(records, "E10")
    # 日内序列可用的起点：哪几天真的存了 ≥24 轮。11-04 之前每份日文件只有 1 次
    # 提交（当天最后一次），拿它算「收盘距离」会把「没存」误报成「价很旧」。
    run_days = {d: len(v) for d, v in days.items()}
    multirun_days = {d for d, n in run_days.items() if n >= 24}
    hourly_since = min(multirun_days) if multirun_days else None
    leads, lasts = [], []
    by_era: dict[str, list[float]] = {"日内序列完整": [], "日内序列有缺口": [],
                                      "只有当天最后一轮": []}
    n_novis = 0
    for r in pool:
        ver = hist.get(r.get("sid")) or {}
        kts = [v for v in ver.values() if v]
        if not kts:
            n_novis += 1
            continue
        k = max(kts)
        pre = [t.timestamp() for t in map(_ts, ver) if t and t.timestamp() < k]
        if not pre:
            continue
        leads.append((k - min(pre)) / 3600)
        lasts.append((k - max(pre)) / 3600)
        kd = datetime.fromtimestamp(k, BJT).date()
        need = [(kd - timedelta(days=i)).isoformat() for i in range(3)]
        if not hourly_since or kd.isoformat() < hourly_since:
            era = "只有当天最后一轮"
        elif all(run_days.get(x, 0) >= 24 for x in need):
            era = "日内序列完整"
        else:
            era = "日内序列有缺口"
        by_era[era].append((k - max(pre)) / 3600)
    la = sorted(lasts)

    return {"bets_in_pool": len(pool), "bets_never_on_board": n_novis,
            "hourly_archive_since": hourly_since,
            "era_stats": {k: close_window_stats(v) for k, v in by_era.items()},
            "bets_measured": len(la),
            "last_look_median_h": la[len(la) // 2] if la else None,
            "last_look_max_h": la[-1] if la else None,
            "within_close_window_h": sum(1 for v in la if v <= CLOSE_WINDOW_H),
            "close_window_h": CLOSE_WINDOW_H,
            "first_lead_median_h": sorted(leads)[len(leads) // 2] if leads else None,
            "hourly_snapshots_per_day": {d: len(v) for d, v in sorted(days.items())},
            "expected_runs_per_day": 48}


# --------------------------------------------------------------------------
# 红旗 + 渲染
# --------------------------------------------------------------------------

def red_flags(report: dict) -> list[dict]:
    """level=red：现在就在丢样本、或现在就在报假平安（--ci 对它返非 0）。
    level=note：量化过的敞口与口径注记，当前样本里还没造成损失。"""
    flags: list[dict] = []

    def add(level: str, text: str) -> None:
        flags.append({"level": level, "text": text})

    st = report["server_truth"]
    if st["truncated_suspected"]:
        visible = st["n_sid_local"] - st["invisible_to_live_check"]
        add("red", f"服务端真值疑似分页截断：{st['pinned_days_at_page_size']} 份快照的条数恒为 "
                   f"{TRUTH_PAGE_SIZE}，而本地 sid 注单已有 {st['n_sid_local']} 条；最新一份只看得见 "
                   f"{visible}/{st['n_sid_local']} 条，其余 {st['invisible_to_live_check']} 条对"
                   "「实时对账 + 跨机去重」是隐身的")
    for kind, g in report["snapshot_gaps"].items():
        if g["missing_days"]:
            add("red", f"逐时快照缺天：{kind} 自 {g['first_file'] or '（无文件）'} 起缺 "
                       f"{len(g['missing_days'])}/"
                       f"{g['want_days']} 天（{'、'.join(g['missing_days'])}）；站点板是 current-only，"
                       "这几天补不回来")
    for exp, s in report["settlement"].items():
        if s["target_reached_while_unsettled"]:
            # 登记判据写的到期条件是「满 60 注」=已提交，所以这不是判据问题，是读数歧义：
            # 同一行里 n 与命中率/ROI 的分母不同，收尾那一句容易被读成「60 注已结算」。
            add("note", f"{exp} 的 pace 已报「n={s['n_pool']} ≥ 60 可收尾」，但其中 "
                        f"{s['n_unsettled_in_n']} 注还没结算：n（已提交）≠ judged（已结算）")
    for exp, m in report["payout_math"].items():
        if m["roi_denominator_gap"]:
            add("red", f"{exp} 的 ROI 分母是 judged={m['judged']}，分子只加有站点价的 "
                       f"{m['priced_judged']} 注 → 无价注被按 0 计入，ROI 系统性低估")
    if st["server_only_unmatched"]:
        add("red", f"服务端有 {len(st['server_only_unmatched'])} 注在本地 records 里连队名都对不上"
                   f"（{'、'.join(x['match'] for x in st['server_only_unmatched'])}）"
                   "——投了、结算了，但没有任何预登记")
    mc = report.get("market_codes") or {}
    if mc.get("n_conflicts_in_ev_site_pool"):
        add("red", f"{mc['n_conflicts_in_ev_site_pool']} 注在冻结池内被按 1x2 判定，"
                   "但服务端记的是非 1x2 市场码")
    if mc.get("n_conflicts"):
        add("note", f"{mc['n_conflicts']} 注本地按 1x2 口径判（pick 写成 主胜/平/负），服务端却记在"
                    f"非 1x2 市场码上（{sorted({c['server_market'] for c in mc['judged_as_1x2_but_server_other_market']})}）"
                    "——这些注的命中/未命中只有在「该码等价于胜平负」时才成立；码语义仓库里没有权威解释。"
                    f"全部在 ev_site 池外（{mc['n_conflicts_in_ev_site_pool']} 注在池内），"
                    "受影响注单：" + "、".join(f"{c['match']}({c['local_pick']}→mkt{c['server_market']})"
                                              for c in mc["judged_as_1x2_but_server_other_market"][:4]) + " 等")
    hc = report["handicap"]
    if hc["n_actual_push_bets"]:
        add("red", f"{hc['n_actual_push_bets']} 注让球的净胜==让球线（走盘）被算成未命中："
                   f"{'、'.join(hc['push_bets'])}")
    if hc["push_counted_as_hit"] is False and hc["n_handicap_bets"]:
        add("note", f"规则性敞口：亚盘整数线走盘 = 未命中（`challenge_review.judge_hit` 的 "
                    f"`margin - line > 0`）。样本内让球注 {hc['n_handicap_bets']} 注"
                    f"（整数线 {hc['n_integer_line']} 注、实际走盘 {hc['n_actual_push_bets']} 注），"
                    f"ev_site 池内 {hc['n_handicap_in_ev_site_pool']} 注 → 当前零污染，"
                    "恢复让球提交才会吃到")
    for exp, d in (report.get("deadline_drift") or {}).items():
        if d["bets_after_target"]:
            add("note", f"{exp} 池在登记判据的先到时点（第 {d['target_n']} 注 {d['crossed_at']}）之后"
                        f"又收了 {d['bets_after_target']} 注 → 现在复算得到 n={d['n_pool']}，"
                        "与登记表写的注数不是同一个数；回退靠改 policy 文件 + PR 合并传播，"
                        "中间那段闸门照旧在投")
    no_score = sum(s["judged_without_exact_score"] for s in report["settlement"].values())
    if no_score:
        add("note", f"{no_score} 注已结算，但全部 results_sina.json 里查不到精确比分"
                    "（1x2 走账本 actual_idx，不受影响）；让球注非用比分不可，落在这类场次上永久不可判，"
                    "而 pace 的 n 照样把它算进去")
    for exp, m in report["payout_math"].items():
        if m.get("break_even_denominator_drift_pp"):
            add("note", f"{exp} 的「打平线」算在已提交池（{m['break_even_printed_on_pool']:.1%}），"
                        f"命中率/ROI 算在已结算池（打平线若同口径是 {m['break_even_on_settled']:.1%}）"
                        f"→ 同一行两个率差 {abs(m['break_even_denominator_drift_pp']):.2f}pp；"
                        "收尾之后每多进一注，这行读数还会自己漂")
    for exp, m in report["payout_math"].items():
        if m["break_even_overstated_pp"]:
            add("note", f"{exp} 的「打平线」打印的是平均隐含概率 mean(1/赔率)，平注真值是 "
                        f"1/mean(赔率)，高估 {m['break_even_overstated_pp']:.2f}pp（看板读数偏保守，"
                        "不参与判据）")
    oc = report.get("odds_close")
    if oc and oc.get("hourly_archive_since") and oc["era_stats"].get("日内序列完整", {}).get("n"):
        e = oc["era_stats"].get("日内序列完整") or {}
        if e.get("n") and e.get("within_close_window_pct") is not None \
                and e["within_close_window_pct"] < 0.5:
            add("note", f"收盘价本来就取不到：日内序列完整（{oc['hourly_archive_since']} 起）的 "
                        f"{e['n']} 注里只有 {e['within_close_window']} 注（"
                        f"{e['within_close_window_pct']:.0%}）在开赛前 ≤{oc['close_window_h']}h "
                        f"还留在板上，最后一次上板距开赛中位 {e['median_h']:.0f}h——"
                        f"站点提前把场次移出可提交板，`site_odds/` 最多给到 "
                        f"T-{e['median_h']:.0f}h 的参考价，不是收盘价")
    return flags


def render(report: dict) -> str:
    L = [f"=== E8/E10 样本完整性审计 @ {report['generated_at']} (baseline {report['baseline']}) ===",
         f"records={report['records']}  sid 注单={report['server_truth']['n_sid_local']}  "
         f"ev_site 池={report['pool_sizes']}"]
    L.append("\n[1] 逐时快照")
    for kind, g in report["snapshot_gaps"].items():
        cov = f"{g['coverage_pct']:.1%}" if g["coverage_pct"] is not None else "—"
        keep = (f"，留当天 {g['kept_snapshot_hour_bjt_median']:.1f}h 那一轮"
                if g["kept_snapshot_hour_bjt_median"] is not None else "")
        L.append(f"  {kind:15s} {g['present_days']}/{g['want_days']} 天（{cov}）"
                 f" 缺 {'、'.join(g['missing_days']) or '无'}{keep}")
    if "odds_close" in report:
        oc = report["odds_close"]
        L.append(f"  日内轮次（git distinct snapshot_at）："
                 f"{oc['hourly_snapshots_per_day']}｜期望 {oc['expected_runs_per_day']}/天")
        L.append(f"  注单 sid 的收盘可见性：measured={oc['bets_measured']}/{oc['bets_in_pool']} "
                 f"从未上板 {oc['bets_never_on_board']}；最后一次上板距开赛 "
                 f"median={oc['last_look_median_h']:.1f}h max={oc['last_look_max_h']:.1f}h，"
                 f"≤{oc['close_window_h']}h 的只有 {oc['within_close_window_h']} 注"
                 f"（首次上板距开赛 median {oc['first_lead_median_h']:.1f}h）")
        L.append(f"  日内序列从 {oc['hourly_archive_since']} 起才真的在 git 里；"
                 "按「开赛前三天是否都有 ≥24 轮」分时代看收盘距离：")
        for era, e in oc["era_stats"].items():
            if not e["n"]:
                continue
            L.append(f"    {era:10s} n={e['n']:3d} median={e['median_h']:.1f}h "
                     f"≤{oc['close_window_h']}h {e['within_close_window']} 注"
                     f"（{e['within_close_window_pct']:.0%}）")
    L.append("\n[2] 结算滞后 / 可判性")
    for exp, s in report["settlement"].items():
        L.append(f"  {exp}: {s.get('pace_line', '')}")
        L.append(f"       n={s['n_pool']} 已判={s['n_judged']} 未结算仍计入 n={s['n_unsettled_in_n']}"
                 f" 已结算无精确比分={s['judged_without_exact_score']}"
                 + (f" ← {'、'.join(s['unsettled_matches'])}" if s["n_unsettled_in_n"] else ""))
    if "lag" in report:
        lg = report["lag"]
        L.append(f"  开赛→比分入库（git 可见，上界）since {lg['since']}: n={lg['bets_measured']} "
                 f"median={lg['lag_h_median']:.1f}h p90={lg['lag_h_p90']:.1f}h max={lg['lag_h_max']:.1f}h")
    L.append("\n[3] 重复计入")
    d = report["duplicates"]
    L.append(f"  sid 重复 {len(d['sid_dups'])}  同场原文重复 {len(d['exact_fixture_dups'])}  "
             f"别名/子串同场 {len(d['alias_fixture_dups'])}（注单 {d['n_records']} 条 / sid {d['n_sid']} 个）")
    L.append("\n[4] 让球走盘")
    dd = report.get("deadline_drift") or {}
    for exp, d in dd.items():
        if d["bets_after_target"]:
            L.append(f"  {exp}: 第 {d['target_n']} 注于 {d['crossed_at']} 提交（先到时点，"
                     f"同一时刻共 {d['bets_at_crossing_timestamp']} 注越线），之后又进池 "
                     f"{d['bets_after_target']} 注 → 现在复算 n={d['n_pool']}")
    mc = report.get("market_codes") or {}
    L.append(f"  市场码：服务端非 1x2 的注 {mc.get('non_1x2_server_bets', 0)} 条 / 并集 {mc.get('union_size', 0)} 条"
             f"；本地按 1x2 判而服务端记非 1x2 码的 {mc.get('n_conflicts', 0)} 条"
             f"（ev_site 池内 {mc.get('n_conflicts_in_ev_site_pool', 0)} 条）；"
             f"仅字段标错（pick 已是让球、按比分判）{len(mc.get('label_only_mismatch', []))} 条；"
             f"服务端有、本地连名字都没有 {len(mc.get('server_only_unregistered', []))} 条")
    hc = report["handicap"]
    L.append(f"  让球注 {hc['n_handicap_bets']} 注（整数线 {hc['n_integer_line']}）"
             f" ev_site 池内 {hc['n_handicap_in_ev_site_pool']} 注"
             f" 净胜==线 现判为 {'命中' if hc['push_counted_as_hit'] else '未命中'}")
    L.append("\n[5] 服务端真值 vs 本地 records")
    st = report["server_truth"]
    L.append(f"  快照条数序列 {st['count_per_snapshot'][-6:]} → 恒 {TRUTH_PAGE_SIZE} 的天数 {st['pinned_days_at_page_size']}")
    L.append(f"  并集 sid {st['union_server_sids']}；服务端有而本地无 sid {len(st['server_only'])}"
             f"（其中按队名能对回 {st['server_only_matched_by_name']}、对不回 {len(st['server_only_unmatched'])}）")
    L.append(f"  本地有而服务端并集没有 {len(st['local_only_sids'])}（幽灵注）；"
             f"下单价与服务端一致 {st['bet_odds_agree_with_server']} / 不一致 {st['bet_odds_differ_from_server']}"
             f" / 无法比 {st['bet_odds_uncomparable']}")
    cov = st["newest_snapshot_covers_pct"]
    L.append((f"  最新快照只覆盖本地 sid 的 {cov:.1%}" if cov is not None
              else "  最新快照覆盖本地 sid：—")
             + f"（{st['invisible_to_live_check']} 条注单对实时对账不可见）")
    L.append("\n[6] 记账口径")
    for exp, m in report["payout_math"].items():
        drift = m.get("break_even_denominator_drift_pp")
        pct = lambda x: "—" if x is None else f"{x:.1%}"          # noqa: E731
        L.append(f"  {exp}: 打平线（算在已提交池）{pct(m['break_even_printed_on_pool'])} "
                 f"｜同口径算在已结算池 {pct(m['break_even_on_settled'])}"
                 + (f"（漂 {drift:+.2f}pp）" if drift is not None else "")
                 + f"｜平注真值 {pct(m['break_even_flat_stake'])}"
                 + (f"（比打印值低 {m['break_even_overstated_pp']:.2f}pp）"
                    if m["break_even_overstated_pp"] is not None else "")
                 + f"｜ROI 分母 judged={m['judged']} 有价={m['priced_judged']}")
    L.append("\n红旗（现在就在丢样本 / 报假平安）")
    reds = [f for f in report["red_flags"] if f["level"] == "red"]
    for f in reds or [dict(text="（无）")]:
        L.append("  ⚠ " + f["text"])
    L.append("量化敞口 / 口径注记")
    notes = [f for f in report["red_flags"] if f["level"] == "note"]
    for f in notes or [dict(text="（无）")]:
        L.append("  · " + f["text"])
    return "\n".join(L)


def build_report(with_history: bool = False, since: str = "2026-11-03",
                 window_start: date = E8_START) -> dict:
    records = load_records()
    today = datetime.now(BJT).date()
    snaps = load_truth_snapshots()
    judged = judgments(records)
    report = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S"),
        "baseline": _baseline(),
        "records": len(records),
        "pool_sizes": {e: len(pool_rows(records, judged, e)) for e in ("E8", "E10")},
        "duplicates": duplicate_bets(records),
        "server_truth": server_truth_vs_records(records, snaps),
        "market_codes": market_code_conflicts(records, snaps),
        "snapshot_gaps": snapshot_gaps(window_start, today),
        "settlement": settlement_coverage(records, judged, today),
        "deadline_drift": pool_deadline_drift(records),
        "handicap": handicap_exposure(records, judged),
        "payout_math": payout_math(records, judged),
    }
    if with_history:
        report["lag"] = score_arrival_lag(records, since)
        report["odds_close"] = odds_close_coverage(records)
    report["red_flags"] = red_flags(report)
    return report


def _baseline() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def guard_out_path(path: str) -> Path:
    """--json 不许写进 data/state/：那是裁判层/策略层的领地，也是本工单的红线。"""
    p = Path(path).resolve()
    banned = (ROOT / "data" / "state").resolve()
    if p == banned or banned in p.parents:
        raise SystemExit(f"audit: 拒绝写入 {banned} 下（只读审计，不产状态文件）")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", dest="json_path", help="把报告另存到这个路径（不得在 data/state/ 下）")
    ap.add_argument("--history", action="store_true",
                    help="附加 git 历史维度（比分落盘时刻 / 站点赔率收盘可见性），慢")
    ap.add_argument("--ci", action="store_true", help="有红旗时退出码 1")
    ap.add_argument("--since", default="2026-11-03", help="--history 里 day-file 已入 git 的最早日期")
    ap.add_argument("--window-start", default=E8_START.isoformat(), help="快照缺口的回溯起点")
    args = ap.parse_args(argv)
    report = build_report(with_history=args.history, since=args.since,
                          window_start=date.fromisoformat(args.window_start))
    print(render(report))
    if args.json_path:
        out = guard_out_path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n→ {out}")
    return 1 if (args.ci and any(f["level"] == "red" for f in report["red_flags"])) else 0


if __name__ == "__main__":
    sys.exit(main())
