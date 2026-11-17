#!/usr/bin/env python3
"""匹配缺口归因（**只读量化**：不参与下单、不改判据、不写仓库任何状态文件）。

## 要回答的问题
issue #28（2026-11-17 派单）：站点池子里有上百场，日志常年只有
`… /池196/匹配5/候选5 …`。中间丢的场到底丢在哪一步——队名别名/译名不一致、
日期窗口错位、联赛不在竞彩范围，还是 predictions 当天没落库？给出每类场数与占比。

## 口径（写死在这里，免得「看着像」）
主口径全部用**仓库里已落盘的产物**，无网络、可复现：

| 输入 | 路径 | 含义 |
|---|---|---|
| 池子 | `data/state/njstats/site_odds/<D>.json` | duty 每轮抓的 msgId=1 全板；`total`=全板场数，`matches`=**带站点 1x2 赔率**的子集 |
| 预测 | `data/daily/<D>/predictions.json` | 竞彩销售日 D 的预测落库（`challenge_hourly` 读的就是它） |
| 竞彩开售名单 | `data/daily/<D>/results.json` ∪ 同日 `predictions.json` | 判定「这场竞彩当天到底开没开售」的证据 |
| 去重真值 | `data/state/njstats/my_predictions/<D>.json` | 服务端「我已预测」列表（与池子快照同一轮 duty 写的） |

两点必须说清的口径限制：
1. **匹配器直接 import 生产的 `challenge_hourly.match_prediction`**。归因的分母与
   分子都是生产口径；本脚本只在生产判「匹配不上」之后，才用更宽的诊断规则去问
   「为什么对不上」。复刻一份匹配逻辑来测 = 在测自己写的副本（issue #28 之前的教训）。
2. 池子每天只落**一张快照**（多数 23:35，少数白天），所以池侧漏斗是**那一刻**的板面
   结构，不是全天累计。快照里没赔率的条目连 `matches` 都没进，本脚本按
   `total − len(matches)` 单独计一类 `0_no_site_odds`（没有结算价，永远进不了候选）。

## 诊断规则（比生产宽，只用于归因，不进任何下单路径）
- 队名相似度按侧判：`_canon`（=normalize_team + `_POOL_ALIAS`）相等 = same；
  互含且 ≥3 字 = contain；`difflib` 相似度 ≥0.6（双方 ≥3 字）= similar；
  梯队/女队标记不同一律 None（10-21 板上「里尔 vs 加拉塔萨雷」和
  「里尔U19 vs 加拉塔萨雷U19」同时存在，只看队名会把青年队当成年队）。
- 「别名可挽回」= 两侧都相似、至少一侧写法不同、**且开赛时刻相差 ≤20 分钟**。
  时刻对不上只算「疑似」，不计入可挽回——这是与「同名不同场」的唯一可靠分界。
- 「竞彩开售」按开赛日 kd 及 kd-1 两个销售日查（实测 1538 条预测里，销售日 D 的比赛
  开球落在 D 的 577 场、落在 D+1 的 889 场，**没有落在 D-1 或 D+2 的**）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 生产匹配器与其配套：归因口径必须与下单一致
from challenge_hourly import _SQUAD_MARKERS, _canon, match_prediction  # noqa: E402

BJT = timezone(timedelta(hours=8))
SNAP_DIR = "data/state/njstats/site_odds"
TRUTH_DIR = "data/state/njstats/my_predictions"
KICKOFF_GUARD_MIN = 45      # 与 challenge_hourly 的 `kts < now + 45*60` 同
ALIAS_TIME_TOL_MIN = 20     # 别名归因允许的开赛时刻偏差
EXAMPLES_PER_BUCKET = 5

POLICY_PATH = "data/state/challenge_policy.json"
EV_CEILING = 0.25               # 生产上界护栏（脏盘口），照抄不改
# 冻结判据里「合格」= ev_floor ≤ ev_site ≤ 上界（challenge_hourly.py 的 ev_ok）。
# 本脚本只是**事后重放这条公式来给丢失场次估值**，不参与下单，也不改任何参数。


def load_policy_gates(root: Path) -> dict[str, Any]:
    """读策略层当前闸门（只读）。坏文件 → 回落到 challenge_hourly 的默认值。"""
    gates = {"gate_mode": "edge", "ev_floor": 0.02, "min_sporttery_odds": 1.60, "gate_delta": 0.0}
    f = root / POLICY_PATH
    if not f.exists():
        return gates
    try:
        pol = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return gates
    if not isinstance(pol, dict):
        return gates
    if isinstance(pol.get("ev_floor"), (int, float)):
        gates["ev_floor"] = float(pol["ev_floor"])
    if isinstance(pol.get("min_sporttery_odds"), (int, float)):
        gates["min_sporttery_odds"] = float(pol["min_sporttery_odds"])
    if pol.get("gate_mode") == "ev_site":
        gates["gate_mode"] = "ev_site"
    if pol.get("gate_hint") == "tighten" and (pol.get("n_settled") or 0) >= 80:
        gates["gate_delta"] = 0.005
    return gates


def replay_candidate(p: dict[str, Any], site_odds: list[float] | None,
                     gates: dict[str, Any]) -> dict[str, Any] | None:
    """把一条没匹配上的预测 + 池子里那场站点价，重放一遍生产的合格判据。

    口径完全照 `scripts/challenge_hourly.py` 的 `main()`：选概率最高的方向、
    `ev_site = P × 站点价 − 1`、合格 = `ev_floor ≤ ev_site ≤ 0.25`。
    缺概率/缺赔率算不出来 → None（不计入任何分子）。
    """
    fp = [p.get("home_win_prob"), p.get("draw_prob"), p.get("away_win_prob")]
    odds = [p.get("home_odds"), p.get("draw_odds"), p.get("away_odds")]
    if None in fp or not odds[0]:
        return None
    sel = max(range(3), key=lambda i: fp[i])
    o, P = odds[sel], fp[sel]
    o_site = site_odds[sel] if site_odds and len(site_odds) > sel else None
    ev_site = P * o_site - 1 if o_site else None
    out = {"dir": sel, "p": round(P, 4), "sporttery_odds": o, "site_odds": o_site,
           "ev_site": None if ev_site is None else round(ev_site, 4)}
    mf = p.get("market_fair")
    if mf:
        out["edge"] = round(P - mf[sel], 4)
    if gates["gate_mode"] == "ev_site":
        out["qualified"] = bool(ev_site is not None and gates["ev_floor"] <= ev_site <= EV_CEILING)
    else:
        # edge 档的 g 随星期与 mode 变（在 challenge_hourly 里算），离线重放拿不到那一刻的
        # 参数，宁可不判：qualified=None = 无法判定，不进分子也不进分母。
        out["qualified"] = None
    out["e10_low_price"] = bool(o < 1.60)
    return out


POOL_BUCKET_MEANING = {
    "0_no_site_odds": "板上有这场但站点没给 1x2 三档价（快照连 matches 都没进）",
    "1_pool_data_missing": "池条目缺 sid/kick_ts/队名，生产直接跳过",
    "2_kickoff_too_close": "日期/时间窗：距快照时刻不足 45 分钟或已开赛",
    "3_already_submitted": "去重：服务端「我已预测」里已有这场",
    "4_matched_production": "生产匹配上（进候选池的唯一入口）",
    "5_alias_recoverable": "队名别名/译名不一致（开赛时刻对得上，可验证）",
    "6_alias_unconfirmed": "疑似别名（队名相似但开赛时刻对不上，不计可挽回）",
    "7_beyond_forecast_window": "日期窗口错位：开球在 D+2 之后，预测还没生成",
    "8_sold_not_predicted_exact": "竞彩开售且我方没预测（队名与竞彩写法一致）",
    "9_sold_not_predicted_variant": "竞彩开售且我方没预测（池子写法与竞彩写法不同）",

    "10_no_sporttery_evidence": "该开赛日（及前一天）的竞彩开售名单里找不到这场 = 无证据显示竞彩开售",
}


# ---------------------------------------------------------------- 载入
def load_snapshot(root: Path, day: str) -> dict[str, Any] | None:
    """`site_odds/<day>.json` → 解析好时刻的池子快照。缺文件/坏 JSON 返回 None。"""
    f = root / SNAP_DIR / f"{day}.json"
    if not f.exists():
        return None
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        snap_at = datetime.strptime(raw["snapshot_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
    except (KeyError, ValueError, json.JSONDecodeError, OSError):
        return None
    return {"day": day, "snapshot_at": snap_at, "total": int(raw.get("total") or 0),
            "matches": raw.get("matches") or []}


def snapshot_days(root: Path) -> list[str]:
    d = root / SNAP_DIR
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def missing_snapshot_days(days: list[str]) -> list[str]:
    """快照日期序列里的空洞（含首尾之间的缺天）。缺天 = 那天没法归因。"""
    ds = [date.fromisoformat(d) for d in days]
    if len(ds) < 2:
        return []
    out, cur = [], ds[0]
    for d in ds[1:]:
        while cur < d - timedelta(days=1):
            cur += timedelta(days=1)
            out.append(cur.strftime("%Y-%m-%d"))
        cur = d
    return out


def load_preds(root: Path, day: str) -> list[dict[str, Any]] | None:
    """销售日 `day` 的预测落库。文件不存在 → None（=「当天没落库」，本身就是一类丢失）。"""
    f = root / "data" / "daily" / day / "predictions.json"
    if not f.exists():
        return None
    try:
        ps = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    ps = ps.get("predictions", ps) if isinstance(ps, dict) else ps
    if not isinstance(ps, list):
        return None
    return [p for p in ps if isinstance(p, dict) and p.get("home_team") and p.get("away_team")]


def load_results_pairs(root: Path, day: str) -> set[tuple[str, str]]:
    """`results.json` 里 match_id 前缀属于销售日 `day` 的赛果队名对（已结算 = 硬证据）。"""
    f = root / "data" / "daily" / day / "results.json"
    if not f.exists():
        return set()
    try:
        rs = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    rs = rs.get("results", rs) if isinstance(rs, dict) else rs
    out: set[tuple[str, str]] = set()
    for r in rs or []:
        if not isinstance(r, dict):
            continue
        mid = str(r.get("match_id") or "")
        if mid and not mid.startswith(f"{day}_"):
            continue
        h, a = r.get("home_team"), r.get("away_team")
        if h and a:
            out.add((_canon(h), _canon(a)))
    return out


def load_sold_pairs(root: Path, day: str) -> set[tuple[str, str]]:
    """销售日 `day` 的竞彩开售名单（`_canon` 后的 (主, 客) 对）。

    取两份的并集：当天落库的预测 + 当天赛果。赛果与赛程的译名可能不同
    （实测 2026-11-15 预测写「神户胜利」、赛果写「神户胜利船」），只读一份会把开售名单算小。
    需要「一场只算一次」的计数请用 `coverage_report`，它会把异写并成一簇。
    """
    out = {(_canon(p["home_team"]), _canon(p["away_team"])) for p in load_preds(root, day) or []}
    return out | load_results_pairs(root, day)


def load_bet_pairs(root: Path, day: str) -> set[tuple[str, str]]:
    """服务端「我已预测」快照的队名对（下单去重真值）。缺文件 → 空集（当作未知）。"""
    f = root / TRUTH_DIR / f"{day}.json"
    if not f.exists():
        return set()
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    out = set()
    for r in raw.get("records") or []:
        h, a = r.get("home"), r.get("away")
        if h and a:
            out.add((_canon(h), _canon(a)))
    return out


def _kickoff_dt(p: dict[str, Any]) -> datetime | None:
    """预测的 kickoff 字符串 → BJT datetime。缺字段返回 None（实测 72/1538 条没有）。"""
    k = p.get("kickoff")
    if not k:
        return None
    try:
        return datetime.strptime(str(k)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
    except ValueError:
        return None


# ---------------------------------------------------------------- 相似度（仅诊断用）
def _is_squad(name: str) -> bool:
    return any(m in name for m in _SQUAD_MARKERS)


def side_similarity(a: str, b: str) -> str | None:
    """单个队名的相似等级：same / contain / similar / None。梯队与成年队直接 None。"""
    ca, cb = _canon(a), _canon(b)
    if _is_squad(a) != _is_squad(b):
        return None
    if ca and ca == cb:
        return "same"
    if len(ca) >= 3 and len(cb) >= 3 and (ca in cb or cb in ca):
        return "contain"
    if len(ca) >= 3 and len(cb) >= 3 and SequenceMatcher(None, ca, cb).ratio() >= 0.6:
        return "similar"
    return None


def pair_similarity(ph: str, pa: str, oh: str, oa: str) -> tuple[bool, bool]:
    """(两侧都相似, 至少一侧写法不同)。ph/pa = 池子队名，oh/oa = 我方队名。"""
    sh, sa = side_similarity(ph, oh), side_similarity(pa, oa)
    if sh is None or sa is None:
        return False, False
    return True, (sh != "same" or sa != "same")


# ---------------------------------------------------------------- 单日归因
def classify_day(root: Path, day: str) -> dict[str, Any] | None:
    """一张池子快照 + 对应两天预测 → 池侧漏斗、预测侧漏斗、别名证据。无快照返回 None。"""
    snap = load_snapshot(root, day)
    if snap is None:
        return None
    snap_at: datetime = snap["snapshot_at"]
    d_next = (snap_at + timedelta(days=1)).strftime("%Y-%m-%d")

    # 与 challenge_hourly 完全一致：读销售日 D 与 D+1 两天的预测
    pred: dict[tuple[str, str], dict[str, Any]] = {}
    pred_src: dict[tuple[str, str], str] = {}
    preds_dirs: dict[str, list[dict[str, Any]] | None] = {}
    for dd in (day, d_next):
        ps = load_preds(root, dd)
        preds_dirs[dd] = ps
        for p in ps or []:
            k = (p["home_team"], p["away_team"])
            pred.setdefault(k, p)
            pred_src.setdefault(k, dd)
    missing_pred_days = [dd for dd, ps in preds_dirs.items() if ps is None]
    # 「当天没落库」单独计一类：challenge_hourly 读不到文件时是 `continue`，
    # 静默少一整天（11-16 快照因此匹配 0）。

    bet = load_bet_pairs(root, day)
    gates = load_policy_gates(root)
    matched_replay: list[dict[str, Any]] = []
    sold_cache: dict[str, set[tuple[str, str]]] = {}

    def sold_lookup(h: str, a: str, kd: date) -> tuple[str, tuple[str, str]] | None:
        """这池条目对应的竞彩开售记录：('exact'|'variant', 竞彩写法) 或 None。"""
        pair = (_canon(h), _canon(a))
        # 只看 kd 与 kd-1 两个销售日：实测销售日 D 的比赛开球落在 D 或 D+1，
        # 不会往前落到 D-1，所以 kd+1 那一侧不存在对应场次（留着会把「以后才预测的场」
        # 误算成「开售了没预测」）。
        cand = {}
        for d in (kd, kd - timedelta(days=1)):
            key = d.strftime("%Y-%m-%d")
            if key not in sold_cache:
                sold_cache[key] = load_sold_pairs(root, key)
            cand[key] = sold_cache[key]
        for sp in cand.values():
            if pair in sp:
                return "exact", pair
        for day_key, sp in cand.items():
            for (uh, ua) in sp:
                ok, differs = pair_similarity(h, a, uh, ua)
                if ok and differs:
                    return "variant", (uh, ua)
        return None

    matched_pred_keys: set[tuple[str, str]] = set()
    matched_from_dir: dict[str, int] = {}
    pool_buckets: dict[str, int] = {}
    pool_examples: dict[str, list[str]] = {}
    alias_evidence: list[dict[str, Any]] = []

    def bump(key: str, example: str = "", n: int = 1) -> None:
        pool_buckets[key] = pool_buckets.get(key, 0) + n
        if example:
            ex = pool_examples.setdefault(key, [])
            if len(ex) < EXAMPLES_PER_BUCKET:
                ex.append(example)

    n_no_odds = max(0, snap["total"] - len(snap["matches"]))
    if n_no_odds:
        # 快照只存带 1x2 三档价的条目，缺的那部分只能按 total − len(matches) 补计一类
        bump("0_no_site_odds", f"total={snap['total']} matches={len(snap['matches'])}", n=n_no_odds)

    for it in snap["matches"]:
        h, a, kts = it.get("home"), it.get("away"), it.get("kick_ts")
        if not it.get("sid") or not kts or not h or not a:
            bump("1_pool_data_missing", str(it)[:80])
            continue
        kt = datetime.fromtimestamp(kts, BJT)
        if kt < snap_at + timedelta(minutes=KICKOFF_GUARD_MIN):
            bump("2_kickoff_too_close", f"{h} vs {a} @ {kt:%m-%d %H:%M}")
            continue
        if (_canon(h), _canon(a)) in bet:
            bump("3_already_submitted", f"{h} vs {a}")
            continue
        p = match_prediction(pred, h, a)
        if p is not None:
            bump("4_matched_production", f"{h} vs {a}")
            matched_pred_keys.add((p["home_team"], p["away_team"]))
            src = pred_src.get((p["home_team"], p["away_team"]), day)
            # 标成 D / D+1：D+1 在快照那一刻还没落库，用来界定「回看上限」与单轮口径的差
            lbl = "D" if src == day else "D+1"
            matched_from_dir[lbl] = matched_from_dir.get(lbl, 0) + 1
            rp = replay_candidate(p, it.get("odds"), gates)
            if rp:
                matched_replay.append(rp)
            continue
        # 生产判「匹配不上」之后，才进诊断
        hit = _nearest_pred(pred, h, a, kt)
        if hit is not None:
            pp, level = hit
            key = "5_alias_recoverable" if level == "time" else "6_alias_unconfirmed"
            bump(key, f"{h} vs {a} ↔ {pp['home_team']} vs {pp['away_team']}")
            alias_evidence.append({
                "day": day, "pool": f"{h} vs {a}", "ours": f"{pp['home_team']} vs {pp['away_team']}",
                "pool_kickoff": kt.strftime("%Y-%m-%d %H:%M"),
                "ours_kickoff": (pp.get("kickoff") or "")[:16],
                "competition": pp.get("competition"), "level": level,
                "replay": replay_candidate(pp, it.get("odds"), gates),
                "diff": [f"{x}→{y}" for x, y in ((h, pp["home_team"]), (a, pp["away_team"]))
                         if _canon(x) != _canon(y)],
            })
            continue
        kd = kt.date()
        # 销售日 D 的比赛最晚 D+1 开球 ⇒ 开球在 D+2 之后的，那一刻根本不可能有预测
        if kd.isoformat() > d_next:
            bump("7_beyond_forecast_window", f"{h} vs {a} @ {kt:%m-%d %H:%M}")
            continue
        sold = sold_lookup(h, a, kd)
        if sold is not None:
            kind, spair = sold
            bump("8_sold_not_predicted_exact" if kind == "exact" else "9_sold_not_predicted_variant",
                 f"{h} vs {a} ↔ 竞彩 {spair[0]} vs {spair[1]}")
        else:
            bump("10_no_sporttery_evidence", f"{h} vs {a}")

    # 预测侧漏斗：分母 = 这两天落库的预测（challenge_hourly 的加载窗口）
    pred_buckets: dict[str, int] = {}
    for key, p in pred.items():
        kt = _kickoff_dt(p)
        if key in matched_pred_keys:
            k = "1_matched_production"
        elif (_canon(p["home_team"]), _canon(p["away_team"])) in bet:
            k = "2_already_submitted"
        elif kt is not None and kt < snap_at + timedelta(minutes=KICKOFF_GUARD_MIN):
            k = "3_kickoff_already_past"
        else:
            hit = _nearest_pool(snap["matches"], p)
            k = {"time": "4_alias_recoverable", "no_time": "5_alias_unconfirmed"}.get(hit or "", "6_not_on_pool_board")
        pred_buckets[k] = pred_buckets.get(k, 0) + 1

    return {
        "day": day,
        "snapshot_at": snap_at.strftime("%Y-%m-%d %H:%M:%S"),
        "pool_board_total": snap["total"],
        "pool_entries": len(snap["matches"]),
        "pred_days": {dd: (None if preds_dirs[dd] is None else len(preds_dirs[dd])) for dd in preds_dirs},
        "missing_pred_days": missing_pred_days,
        "pool_buckets": pool_buckets,
        "pool_examples": pool_examples,
        "pred_buckets": pred_buckets,
        "pred_total": len(pred),
        "matched_from_dir": matched_from_dir,
        "matched_replay": {"n": len(matched_replay),
                           "qualified": sum(1 for r in matched_replay if r.get("qualified")),
                           "low_price": sum(1 for r in matched_replay if r.get("e10_low_price"))},
        "alias_evidence": alias_evidence,
        "matched_pairs": sorted([h, a] for h, a in matched_pred_keys),
    }


def _nearest_pred(pred: dict[tuple[str, str], dict[str, Any]], h: str, a: str,
                  kt: datetime) -> tuple[dict[str, Any], str] | None:
    """生产没匹配上的池条目，在预测里找最相似的一条。时刻对得上的优先。"""
    fallback: tuple[dict[str, Any], str] | None = None
    for (oh, oa), p in pred.items():
        ok, differs = pair_similarity(h, a, oh, oa)
        if not ok or not differs:
            continue
        pkt = _kickoff_dt(p)
        if pkt is not None and abs((pkt - kt).total_seconds()) <= ALIAS_TIME_TOL_MIN * 60:
            return p, "time"
        if fallback is None:
            fallback = (p, "no_time")
    return fallback


def _nearest_pool(matches: list[dict[str, Any]], p: dict[str, Any]) -> str | None:
    """反向：一条没匹配上的预测，在池子里找最相似的条目 → time / no_time / None。"""
    kt = _kickoff_dt(p)
    out: str | None = None
    for it in matches:
        h, a = it.get("home"), it.get("away")
        if not h or not a:
            continue
        ok, differs = pair_similarity(h, a, p["home_team"], p["away_team"])
        if not ok or not differs:
            continue
        if kt is None or not it.get("kick_ts"):
            out = out or "no_time"
            continue
        if abs((datetime.fromtimestamp(it["kick_ts"], BJT) - kt).total_seconds()) <= ALIAS_TIME_TOL_MIN * 60:
            return "time"
        out = out or "no_time"
    return out


# ---------------------------------------------------------------- duty 日志对照（本机可选）
LOG_POOL_RE = re.compile(r"池(\d+)/匹配(\d+)/候选(\d+)")   # 零合格轮：池/匹配/候选 三段齐全
LOG_SUBMIT_RE = re.compile(r"challenge: 提交 (\d+) 注.*?[（(]匹配(\d+)/候选(\d+)")
LOG_LINE_RE = re.compile(r"^(\d{2}-\d{2}) (\d{2}):\d{2}:\d{2} ")
SECRET_RE = re.compile(r"eyJ[A-Za-z0-9_.\-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")


def parse_duty_log(path: Path) -> dict[str, Any]:
    """把 duty.log 里的 `池N/匹配N/候选N` 行按天汇总，用来对照快照口径。

    日志行只有 MM-DD（无年份），这里按「最后一行的年份 = 文件 mtime 年份」倒推跨年。
    只统计数字，绝不回显日志原文（里面有 mint/token 相关行）。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"available": False, "reason": f"读不到 {path}"}
    dated: list[tuple[str, int, int | None, int]] = []
    for ln in lines:
        if SECRET_RE.search(ln):
            continue
        m = LOG_LINE_RE.match(ln)
        if not m:
            continue
        md, hh = m.group(1), int(m.group(2))
        pool = matched = None
        if (g := LOG_POOL_RE.search(ln)):
            pool, matched = int(g.group(1)), int(g.group(2))
        elif (g := LOG_SUBMIT_RE.search(ln)):
            # 提交行不带「池」字段，只有 匹配/候选——不能拿注数冒充池容量
            matched = int(g.group(2))
        if matched is None:
            continue
        dated.append((md, hh, pool, matched))
    if not dated:
        return {"available": False, "reason": "日志里没有 池/匹配/候选 行"}
    year = datetime.fromtimestamp(path.stat().st_mtime, BJT).year
    prev_key, stamped = None, []
    for md, hh, pool, matched in reversed(dated):
        key = (int(md[:2]), int(md[3:5]))
        if prev_key is not None and key > prev_key:
            year -= 1          # 12-31 ← 01-01 逆序回溯时遇到「变大」= 跨年
        prev_key = key
        stamped.append((f"{year}-{md}", hh, pool, matched))
    stamped.reverse()
    by_day: dict[str, dict[str, Any]] = {}
    for d, _hh, pool, matched in stamped:
        r = by_day.setdefault(d, {"rounds": 0, "pool_max": 0, "matched_max": 0, "matched_zero_rounds": 0})
        r["rounds"] += 1
        if pool is not None:
            r["pool_max"] = max(r["pool_max"], pool)
        r["matched_max"] = max(r["matched_max"], matched)
        r["matched_zero_rounds"] += 1 if matched == 0 else 0
    vals = sorted(r["matched_max"] for r in by_day.values())
    return {
        "available": True, "window": f"{stamped[0][0]}…{stamped[-1][0]}", "lines": len(stamped),
        "days": len(by_day),
        "pool_max_median": sorted(r["pool_max"] for r in by_day.values())[len(by_day) // 2],
        "matched_max_median": vals[len(vals) // 2],
        "matched_max_min": vals[0], "matched_max_max": vals[-1],
        "rounds_matched_le7": sum(1 for _, _, _, m in stamped if m <= 7),
        "rounds_total": len(stamped),
    }


# ---------------------------------------------------------------- 汇总
def alias_candidates(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """别名证据 → 可落地的候选：`池子写法 → 我方写法`，按影响天数排序。

    只取 `level == "time"`（开赛时刻对得上）的证据：这类才是可验证的同场异写。
    """
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for d in days:
        for e in d["alias_evidence"]:
            if e["level"] != "time":
                continue
            for x, y in (t.split("→", 1) for t in e["diff"]):
                row = agg.setdefault((x, y), {"pool_name": x, "ours_name": y, "competitions": [],
                                             "n_days": 0, "n_matches": 0, "days": [], "examples": []})
                if d["day"] not in row["days"]:
                    row["n_days"] += 1
                    row["days"].append(d["day"])
                row["n_matches"] += 1
                if e.get("competition") and e["competition"] not in row["competitions"]:
                    row["competitions"].append(e["competition"])
                if len(row["examples"]) < 3:
                    row["examples"].append(f'{e["pool"]} @ {e["pool_kickoff"]}（我方 {e["ours"]}）')
    return sorted(agg.values(), key=lambda r: (-r["n_days"], -r["n_matches"], r["pool_name"]))


def _derive(per_day: list[dict[str, Any]]) -> dict[str, Any]:
    """把漏斗折算成几个能直接拿去说事的比率（分母全部写清）。"""
    pool_board = sum(r["pool_board_total"] for r in per_day)
    ev = [e for r in per_day for e in r["alias_evidence"]]
    tim = [e for e in ev if e["level"] == "time"]
    rp = [e.get("replay") for e in tim if e.get("replay")]
    matched = sum(r["pool_buckets"].get("4_matched_production", 0) for r in per_day)
    m_rp = sum(r["matched_replay"]["n"] for r in per_day)
    m_q = sum(r["matched_replay"]["qualified"] for r in per_day)
    pred_total = sum(r["pred_total"] for r in per_day)
    pb: dict[str, int] = {}
    for r in per_day:
        for k, v in r["pred_buckets"].items():
            pb[k] = pb.get(k, 0) + v
    bettable = pred_total - pb.get("2_already_submitted", 0) - pb.get("3_kickoff_already_past", 0)
    out_of_scope = sum(r["pool_buckets"].get(k, 0) for r in per_day for k in
                       ("7_beyond_forecast_window", "10_no_sporttery_evidence", "0_no_site_odds",
                        "1_pool_data_missing"))
    timing_loss = sum(r["pool_buckets"].get(k, 0) for r in per_day for k in
                      ("2_kickoff_too_close", "3_already_submitted"))
    return {
        "pool_out_of_scope": out_of_scope,
        "pool_out_of_scope_pct": round(out_of_scope / pool_board, 4) if pool_board else None,
        "pool_timing_loss": timing_loss,
        "pool_timing_loss_pct": round(timing_loss / pool_board, 4) if pool_board else None,
        "production_matches_per_day": round(matched / len(per_day), 2) if per_day else None,
        "alias_time_verified": len(tim),
        "alias_days_hit": len({e["day"] for e in tim}),
        "alias_replay_computable": len(rp),
        "alias_replay_qualified": sum(1 for x in rp if x.get("qualified")),
        "matched_replay_computable": m_rp,
        "matched_replay_qualified": m_q,
        "pred_bettable": bettable,
        "pred_alias_share_of_bettable": round(len(tim) / bettable, 4) if bettable else None,
        "pred_matched_share_of_bettable": round(pb.get("1_matched_production", 0) / bettable, 4) if bettable else None,
        "pred_no_board_share_of_bettable": round(pb.get("6_not_on_pool_board", 0) / bettable, 4) if bettable else None,
    }


def _fixture_clusters(pairs: list[tuple[str, str]]) -> list[list[int]]:
    """把「同一场的不同写法」并成一簇（队名相似度两侧都成立即同一场）。

    为什么要并：赛果与赛程是两套译名（预测写「神户胜利」、赛果写「神户胜利船」），
    直接按 `_canon` 去重会把一场算成两场，`开售 → 预测` 的比率就会虚低。
    """
    clusters: list[list[int]] = []
    for i, (h, a) in enumerate(pairs):
        placed = False
        for cl in clusters:
            ch, ca = pairs[cl[0]]
            ok, _ = pair_similarity(h, a, ch, ca)
            if ok:
                cl.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    # 一次并簇可能就够（簇内队名差异都很小），不做多轮合并以免把不同场并到一起
    return clusters


def coverage_report(root: Path, per_day: list[dict[str, Any]]) -> dict[str, Any]:
    """按**竞彩销售日**算我们这一侧的漏斗：开售 → 落库预测 → 被池子匹配上。

    这是回答「池子上百场、匹配只有 5~7」时唯一有意义的一侧：板面多大我们管不着，
    但「开售了却没预测」和「预测了却没进池」都是自家漏斗。
    开售场数 = 当天赛果 ∪ 当天预测，按 `_fixture_clusters` 把异写并成一场后计数
    （赛果是事后写入的，对已开赛的日子是硬证据；未开赛日只有预测，会低估开售数）。
    """
    matched_all: set[tuple[str, str]] = set()
    for r in per_day:
        for h, a in (r.get("matched_pairs") or []):
            matched_all.add((_canon(h), _canon(a)))
    days = {r["day"] for r in per_day}
    days |= {(date.fromisoformat(d) + timedelta(days=1)).strftime("%Y-%m-%d") for d in days}
    rows, tot = [], {"sold": 0, "pred": 0, "matched": 0}
    for day in sorted(days):
        preds = load_preds(root, day)
        pred_pairs = [(_canon(p["home_team"]), _canon(p["away_team"])) for p in preds or []]
        res_pairs = sorted(load_results_pairs(root, day))
        universe = pred_pairs + [x for x in res_pairs if x not in set(pred_pairs)]
        clusters = _fixture_clusters(universe)
        pred_idx = set(range(len(pred_pairs)))
        n_sold = len(clusters)
        n_pred = sum(1 for cl in clusters if set(cl) & pred_idx)
        n_matched = sum(1 for cl in clusters
                        if any(i in pred_idx and universe[i] in matched_all for i in cl))
        rows.append({"day": day, "sold": n_sold, "pred": n_pred, "matched": n_matched,
                     "pred_missing": preds is None})
        tot["sold"] += n_sold
        tot["pred"] += n_pred
        tot["matched"] += n_matched
    return {"per_day": rows, **tot}


def build_report(root: Path, days: list[str] | None = None,
                 duty_log: Path | None = None) -> dict[str, Any]:
    all_days = days if days is not None else snapshot_days(root)
    per_day = [r for r in (classify_day(root, d) for d in all_days) if r]
    pool_tot: dict[str, int] = {}
    pred_tot: dict[str, int] = {}
    src_tot: dict[str, int] = {}
    for r in per_day:
        for k, v in r["pool_buckets"].items():
            pool_tot[k] = pool_tot.get(k, 0) + v
        for k, v in r["pred_buckets"].items():
            pred_tot[k] = pred_tot.get(k, 0) + v
        for k, v in r["matched_from_dir"].items():
            src_tot[k] = src_tot.get(k, 0) + v
    derived = _derive(per_day)
    return {
        "n_days": len(per_day),
        "days": [r["day"] for r in per_day],
        "snapshots_missing": missing_snapshot_days([r["day"] for r in per_day]),
        "pool_board_total": sum(r["pool_board_total"] for r in per_day),
        "pool_entries": sum(r["pool_entries"] for r in per_day),
        "pool_buckets": pool_tot,
        "pred_total": sum(r["pred_total"] for r in per_day),
        "pred_buckets": pred_tot,
        "matched_from_dir": src_tot,
        "missing_pred_days": sorted({d for r in per_day for d in r["missing_pred_days"]}),
        "alias_evidence": [e for r in per_day for e in r["alias_evidence"]],
        "alias_candidates": alias_candidates(per_day),
        "derived": derived,
        "coverage": coverage_report(root, per_day),
        "duty_log": parse_duty_log(duty_log) if duty_log else {"available": False, "reason": "未指定 --duty-log"},
        "per_day": per_day,
    }


def _pct(n: int, d: int) -> str:
    return f"{n / d:6.1%}" if d else "     -  "


def render_text(rep: dict[str, Any]) -> str:
    L: list[str] = []
    per = rep["n_days"] or 1
    L.append(f"样本：{rep['n_days']} 张池子快照（{rep['days'][0]}…{rep['days'][-1]}）；"
             f"缺天 {len(rep['snapshots_missing'])} 个 {rep['snapshots_missing'][:12]}")
    L.append(f"板面场数累计 {rep['pool_board_total']}（日均 {rep['pool_board_total'] / per:.0f}），"
             f"带 1x2 赔率的池条目累计 {rep['pool_entries']}，落库预测 {rep['pred_total']} 条")
    L.append("")
    L.append("【池子侧漏斗】分母 = 带赔率池条目 + 0_no_site_odds（=板面场数）")
    denom = rep["pool_board_total"]
    for k in sorted(rep["pool_buckets"]):
        v = rep["pool_buckets"][k]
        L.append(f"  {k:<28} {v:6d}  {_pct(v, denom)}  日均{v / per:5.1f}  {POOL_BUCKET_MEANING.get(k, '')}")
    L.append("")
    L.append(f"【预测侧漏斗】分母 = 销售日 D/D+1 落库预测 {rep['pred_total']} 条")
    for k in sorted(rep["pred_buckets"]):
        v = rep["pred_buckets"][k]
        L.append(f"  {k:<28} {v:6d}  {_pct(v, rep['pred_total'])}")
    L.append("")
    cov = rep["coverage"]
    L.append("【销售日覆盖漏斗】分母 = 该销售日竞彩开售场数（results ∪ predictions 去重队名对）；")
    L.append("  「匹配」只统计有快照的那些时刻，缺快照的日子会低估")
    L.append(f"  开售 {cov['sold']} → 落库预测 {cov['pred']}（{cov['pred'] / cov['sold']:.1%}）"
             f" → 被池子匹配上 {cov['matched']}（占开售 {cov['matched'] / cov['sold']:.1%}、"
             f"占预测 {cov['matched'] / cov['pred']:.1%}）")
    for r in cov["per_day"]:
        if r["sold"] - r["pred"] >= 3 or r["pred_missing"]:
            L.append(f"    ▸ {r['day']} 开售{r['sold']:>3} 预测{r['pred']:>3} 匹配{r['matched']:>3}"
                     + ("   ← predictions.json 没落库（新北京日不算，见文档 §5）"
                        if r["pred_missing"] else ""))
    L.append("")
    dv = rep["derived"]
    L.append("【关键比率】")
    L.append(f"  板面上「那一刻结构上不可能匹配」（无赔率 / 开球 D+2 后 / 无竞彩开售证据）："
             f"{dv['pool_out_of_scope']}/{rep['pool_board_total']} = {dv['pool_out_of_scope_pct']:.1%}")
    L.append(f"  时间窗与去重另计：{dv['pool_timing_loss']} = {dv['pool_timing_loss_pct']:.1%}")
    L.append(f"  生产匹配日均 {dv['production_matches_per_day']} 场；这批场按冻结闸门重放，"
             f"{dv['matched_replay_qualified']}/{dv['matched_replay_computable']} 场本可合格")
    L.append(f"  别名可挽回（开赛时刻对得上）{dv['alias_time_verified']} 场、覆盖 {dv['alias_days_hit']} 天；"
             f"重放闸门后 {dv['alias_replay_qualified']}/{dv['alias_replay_computable']} 场本可合格"
             f"（对照合格率 {dv['matched_replay_qualified']}/{dv['matched_replay_computable']}）")
    L.append(f"  可投预测（扣掉已投与已开赛）{dv['pred_bettable']} 条："
             f"匹配 {dv['pred_matched_share_of_bettable']:.1%} / 别名丢 "
             f"{dv['pred_alias_share_of_bettable']:.1%} / 板上没这场 "
             f"{dv['pred_no_board_share_of_bettable']:.1%}")
    L.append("")
    L.append("【逐日】板面 / 带赔率 / 生产匹配 / 别名可挽回 / 落库预测 / 匹配到的预测")
    for r in rep["per_day"]:
        pb, rb = r["pool_buckets"], r["pred_buckets"]
        L.append(f"  {r['day']}  {r['pool_board_total']:>4} /{r['pool_entries']:>4}  匹配{pb.get('4_matched_production', 0):>3}"
                 f"  别名{pb.get('5_alias_recoverable', 0):>3}  预测{r['pred_total']:>3}"
                 f"  匹配预测{rb.get('1_matched_production', 0):>3}"
                 + ("   ← 没落库:" + ",".join(r["missing_pred_days"]) if r["missing_pred_days"] else ""))
    L.append("")
    src = rep.get("matched_from_dir") or {}
    L.append("【口径校验】匹配上的池条目，其预测来自哪个销售日目录")
    for k in sorted(src):
        L.append(f"  来自 {k}: {src[k]}")
    L.append("  → 快照落在 23:35 左右，而销售日 D+1 的预测通常次日凌晨 00:09–03:12 才落库；")
    L.append("    「来自 D+1」那部分在快照那一刻生产是拿不到的，所以本表是**回看上限**，")
    L.append("    必然高于 duty.log 的单轮匹配数（下面用日志交叉核对）。")
    L.append("")
    dl = rep.get("duty_log") or {}
    L.append("【duty.log 对照】")
    if dl.get("available"):
        L.append(f"  窗口 {dl['window']}，{dl['days']} 天 / {dl['rounds_total']} 轮；"
                 f"每轮匹配数 ≤7 的占 {dl['rounds_matched_le7']}/{dl['rounds_total']}"
                 f"；单日匹配峰值中位 {dl['matched_max_median']}（区间 {dl['matched_max_min']}–{dl['matched_max_max']}）；"
                 f"池最大中位 {dl['pool_max_median']}")
    else:
        L.append(f"  跳过：{dl.get('reason')}（快照口径已足够，日志只是交叉核对）")
    L.append("")
    L.append("【别名候选】只取开赛时刻对得上的证据（池子写法 → 我方写法）；本脚本不改任何别名表")
    for c in rep["alias_candidates"][:20]:
        L.append(f"  {c['pool_name']} → {c['ours_name']}  {c['n_days']} 天/{c['n_matches']} 场"
                 f"  [{'、'.join(c['competitions'][:3])}]  例：{c['examples'][0]}")
    if not rep["alias_candidates"]:
        L.append("  （无）")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="匹配缺口归因（只读量化）")
    ap.add_argument("--root", default=str(ROOT), help="仓库根目录（默认脚本所在仓库）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--duty-log", default="", help="可选：~/.football-engine/duty.log 路径，用于交叉核对轮次数")
    args = ap.parse_args(argv)
    rep = build_report(Path(args.root), duty_log=Path(args.duty_log) if args.duty_log else None)
    if not rep["n_days"]:
        print(f"没有可用的 {SNAP_DIR}/*.json 快照，无法归因")
        return 1
    print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else render_text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
