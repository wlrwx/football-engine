#!/usr/bin/env python3
"""分层校准偏差只读诊断（2026-11-07，每日探索 scout）

按联赛 / 赔率档(<1.60 / 1.60-2.50 / >2.50) / 选向(主/平/客) 分层，
对 chain=v2 洁净已结算样本计算 Brier、命中率 vs 打平线(1/odds)、
校准偏差(主推概率均值 − 实际命中率)，定位系统性偏差最大的层。

只读：不写任何状态文件，不改任何参数 / 门槛 / 校准。

口径
----
- 样本：data/state/review_ledger.jsonl 中 chain=="v2" 且 contaminated!=True。
  无 chain 的旧链样本与污染样本一律排除（AGENTS.md：污染样本不得进校准/回放）。
- 选向：best_selection = argmax(final_prob)，与 engine/review/post_match.py 一致；
  hit = (best_selection == actual_idx)。
- 赔率：主推方向的 1X2 赔率，按 match_id 连接 data/daily/<date>/predictions.json
  的 home_odds/draw_odds/away_odds（与 post_match.py 的 sel_odds 同一来源）。
  不用 odds_used：实际出注可能是比分玩法，其赔率不是 1X2 主推赔率。
  odds_synthetic=True 的合成赔率不参与赔率档 / 打平线 / ROI 统计。
- 赔率档边界（本次探索题目给定，不是生产闸门 L1-L5）：
  o < 1.60 → "<1.60"；1.60 <= o <= 2.50 → "1.60-2.50"；o > 2.50 → ">2.50"。
- Brier：三分类平方误差和，与 post_match.brier_score 同式；从 final_prob 重算，
  并与账本存储值核对。市场/模型 Brier 仅在对应概率存在的行上配对计算。
- 打平线：mean(1/odds)；缺口 edge = 命中率 − 打平线（真实赔率子样本）。
   flat ROI = mean(命中 ? odds−1 : −1)，SE = 逐行利润标准差 / sqrt(n)。
- 小样本约定（仅本报告口径，不改任何生产门槛）：
  n < 30 → 不可结论，不参与偏差排名；30 <= n < 100 → 样本偏小，仅供参考。

用法::

    .venv/bin/python scripts/stratified_calibration.py
    .venv/bin/python scripts/stratified_calibration.py --json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "state" / "review_ledger.jsonl"
DAILY_ROOT = ROOT / "data" / "daily"

DIRS = ("home", "draw", "away")
SIDES_ZH = {"home": "主胜", "draw": "平局", "away": "客胜"}
TIERS = ("<1.60", "1.60-2.50", ">2.50")
MIN_N = 30          # 不可结论线（仅本报告口径）
SMALL_N = 100       # 样本偏小线（仅本报告口径）
LEAGUE_SHOW_N = 15  # 联赛表单独展示的最低 n，其余并入长尾行


def brier3(probs: list, actual_idx: int) -> float:
    """三分类 Brier（与 engine/review/post_match.py::brier_score 同式）。"""
    if not probs or len(probs) < 3:
        return 1.0
    return sum((p - (1.0 if i == actual_idx else 0.0)) ** 2
               for i, p in enumerate(probs[:3]))


def tier_of(odds: float | None) -> str | None:
    """探索题目给定的赔率档；缺失/非法赔率返回 None。"""
    if odds is None:
        return None
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(o) or o <= 1.0:
        return None
    if o < 1.60:
        return "<1.60"
    if o <= 2.50:
        return "1.60-2.50"
    return ">2.50"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """命中率的 Wilson 95% 区间。n=0 返回 (0, 1)。"""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def load_ledger(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_prediction_index(daily_root: Path) -> dict[str, dict]:
    """match_id → {odds: [h,d,a], synthetic: bool}。按路径排序，首个生效。"""
    index: dict[str, dict] = {}
    for f in sorted(daily_root.glob("*/predictions.json")):
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            mid = it.get("match_id")
            if not mid or mid in index:
                continue
            index[mid] = {
                "odds": [it.get("home_odds"), it.get("draw_odds"), it.get("away_odds")],
                "synthetic": bool(it.get("odds_synthetic")),
            }
    return index


def select_sample(ledger: list[dict]) -> tuple[list[dict], dict]:
    """chain=v2 洁净样本；返回 (样本, 排除计数)。"""
    clean, excluded = [], defaultdict(int)
    for r in ledger:
        if r.get("chain") != "v2":
            excluded["legacy_or_other_chain"] += 1
            continue
        if r.get("contaminated"):
            excluded["contaminated"] += 1
            continue
        clean.append(r)
    return clean, dict(excluded)


def enrich(rows: list[dict], pred_index: dict[str, dict]) -> list[dict]:
    """给每行挂 sel_odds / odds_synthetic / side / tier（不改原 dict）。"""
    out = []
    for r in rows:
        r = dict(r)
        sel = r.get("best_selection")
        r["side"] = DIRS[sel] if sel in (0, 1, 2) else None
        info = pred_index.get(r.get("match_id", ""))
        odds = None
        synthetic = False
        if info is not None and sel in (0, 1, 2):
            odds = info["odds"][sel]
            synthetic = info["synthetic"]
        r["sel_odds"] = odds
        r["odds_synthetic"] = synthetic
        r["tier"] = tier_of(odds)
        out.append(r)
    return out


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def stratum_metrics(rows: list[dict]) -> dict:
    """一个层的只读统计。赔率类指标只用非合成赔率子样本。"""
    n = len(rows)
    hits = sum(1 for r in rows if r.get("hit"))
    hit_rate = hits / n if n else None
    lo, hi = wilson(hits, n)
    bf = _mean([brier3(r.get("final_prob"), r["actual_idx"]) for r in rows])
    p_sel = _mean([r["final_prob"][r["best_selection"]] for r in rows
                   if r.get("final_prob") and r.get("best_selection") in (0, 1, 2)])
    paired_m = [r for r in rows if r.get("market_fair")]
    bm = _mean([brier3(r["market_fair"], r["actual_idx"]) for r in paired_m])
    bf_m = _mean([brier3(r.get("final_prob"), r["actual_idx"]) for r in paired_m])
    paired_mod = [r for r in rows if r.get("model_raw")]
    bmod = _mean([brier3(r["model_raw"], r["actual_idx"]) for r in paired_mod])

    odds_rows = [r for r in rows
                 if r.get("tier") is not None and not r.get("odds_synthetic")]
    n_odds = len(odds_rows)
    breakeven = edge = roi = roi_se = hit_odds = None
    if n_odds:
        hit_odds = sum(1 for r in odds_rows if r.get("hit")) / n_odds
        breakeven = _mean([1.0 / float(r["sel_odds"]) for r in odds_rows])
        edge = hit_odds - breakeven
        profits = [(float(r["sel_odds"]) - 1.0) if r.get("hit") else -1.0
                   for r in odds_rows]
        roi = _mean(profits)
        roi_se = (statistics.stdev(profits) / math.sqrt(n_odds)) if n_odds >= 2 else None

    return {
        "n": n,
        "hits": hits,
        "hit_rate": hit_rate,
        "hit_ci95": [lo, hi],
        "brier_final": bf,
        "brier_market": bm,
        "brier_final_paired_market": bf_m,
        "n_market": len(paired_m),
        "brier_model": bmod,
        "n_model": len(paired_mod),
        "mean_p_sel": p_sel,
        "calib_gap": (p_sel - hit_rate) if (p_sel is not None and hit_rate is not None) else None,
        "n_odds": n_odds,
        "hit_rate_odds": hit_odds,
        "breakeven": breakeven,
        "edge": edge,
        "roi": roi,
        "roi_se": roi_se,
        "small": "不可结论" if n < MIN_N else ("偏小" if n < SMALL_N else ""),
    }


def stratify(rows: list[dict], key_fn) -> dict[str, dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        buckets[str(k)].append(r)
    return {k: stratum_metrics(rs) for k, rs in buckets.items()}


def rank_biases(strata: dict[str, dict], top: int = 5) -> dict[str, list]:
    """n>=MIN_N 的层按 |校准偏差| 与 |打平线缺口| 排名（赔率缺口看 n_odds）。"""
    calib = [(k, m) for k, m in strata.items()
             if m["n"] >= MIN_N and m.get("calib_gap") is not None]
    edge = [(k, m) for k, m in strata.items()
            if m["n_odds"] >= MIN_N and m.get("edge") is not None]
    calib.sort(key=lambda km: -abs(km[1]["calib_gap"]))
    edge.sort(key=lambda km: -abs(km[1]["edge"]))
    return {"calib": calib[:top], "edge": edge[:top]}


def analyze(ledger_path: Path, daily_root: Path) -> dict:
    ledger = load_ledger(ledger_path)
    sample, excluded = select_sample(ledger)
    rows = enrich(sample, load_prediction_index(daily_root))

    # 数据质量核对：重算 Brier 与账本存储值的最大偏差
    diffs = [abs(brier3(r.get("final_prob"), r["actual_idx"]) - r["brier_final"])
             for r in rows if r.get("brier_final") is not None and r.get("final_prob")]
    no_pred = sum(1 for r in rows if r.get("tier") is None)
    synthetic = sum(1 for r in rows if r.get("odds_synthetic"))

    overall = stratum_metrics(rows)
    by_tier = stratify(rows, lambda r: r.get("tier"))
    by_side = stratify(rows, lambda r: r.get("side"))
    by_league = stratify(rows, lambda r: r.get("league") or None)
    by_cross = stratify(rows, lambda r: (
        f"{r['tier']}×{SIDES_ZH[r['side']]}" if r.get("tier") and r.get("side") else None))

    all_strata = {}
    for name, strata in (("赔率档", by_tier), ("选向", by_side),
                         ("联赛", by_league), ("赔率档×选向", by_cross)):
        for k, m in strata.items():
            all_strata[f"{name}:{k}"] = m

    dates = [r.get("date", "") for r in rows if r.get("date")]
    return {
        "ledger": str(ledger_path),
        "ledger_total": len(ledger),
        "excluded": excluded,
        "n": len(rows),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "no_pred_odds": no_pred,
        "synthetic_odds": synthetic,
        "brier_recheck_max_diff": max(diffs) if diffs else None,
        "overall": overall,
        "by_tier": by_tier,
        "by_side": by_side,
        "by_league": by_league,
        "by_cross": by_cross,
        "ranking": rank_biases(all_strata),
        "min_n": MIN_N,
    }


def _pct(x: float | None) -> str:
    return f"{x:.1%}" if x is not None else "—"


def _f(x: float | None, nd: int = 4) -> str:
    return f"{x:.{nd}f}" if x is not None else "—"


def _row(k: str, m: dict) -> str:
    edge = m.get("edge")
    edge_s = f"{edge:+.1%}" if edge is not None else "—"
    roi = m.get("roi")
    roi_s = f"{roi:+.1%}" if roi is not None else "—"
    cg = m.get("calib_gap")
    cg_s = f"{cg:+.1%}" if cg is not None else "—"
    flag = f" ⚠{m['small']}" if m.get("small") else ""
    return (f"| {k}{flag} | {m['n']} | {_pct(m['hit_rate'])} | {_f(m['brier_final'])} | "
            f"{_f(m['brier_market'])} | {_pct(m['breakeven'])} | {edge_s} | {roi_s} | "
            f"{cg_s} | {m['n_odds']} |")


_HEADER = ("| 层 | n | 命中率 | Brier最终 | Brier市场 | 打平线 | 缺口 | flatROI | 校准偏差 | n赔率 |\n"
           "|---|---|---|---|---|---|---|---|---|---|")


def render_markdown(res: dict) -> str:
    L: list[str] = []
    L.append("# 分层校准偏差只读诊断")
    L.append("")
    L.append(f"- 账本：`{res['ledger']}`（总 {res['ledger_total']} 行）")
    L.append(f"- 样本：chain=v2 洁净 n={res['n']}，{res['date_min']} ~ {res['date_max']}；"
             f"排除 {res['excluded'] or '{}'}")
    L.append(f"- 主推赔率缺失 {res['no_pred_odds']} 行；合成赔率 {res['synthetic_odds']} 行"
             "（不参与赔率档/打平线/ROI）")
    L.append(f"- Brier 重算 vs 账本存储最大偏差：{_f(res['brier_recheck_max_diff'], 8)}")
    L.append(f"- 小样本口径：n<{MIN_N} 不可结论；{MIN_N}≤n<{SMALL_N} 偏小。"
             "缺口=命中率−mean(1/odds)；校准偏差=主推概率均值−命中率（>0 高估）。")
    L.append("- 打平线/缺口/flatROI 仅用 n赔率 子样本（非合成赔率），其命中率见 JSON "
             "hit_rate_odds；表中命中率列为全层口径。打平线含站点赔率水位，"
             "完美校准模型按含水位赔率投注也会呈负缺口，解读时须与校准偏差列分开。")
    L.append("")
    L.append("## 总体")
    L.append(_HEADER)
    L.append(_row("全部", res["overall"]))
    for title, key, order in (("按赔率档", "by_tier", TIERS),
                              ("按选向", "by_side", DIRS)):
        L.append("")
        L.append(f"## {title}")
        L.append(_HEADER)
        strata = res[key]
        for k in order:
            if k in strata:
                label = SIDES_ZH.get(k, k)
                L.append(_row(label, strata[k]))
        for k in strata:
            if k not in order:
                L.append(_row(k, strata[k]))
    L.append("")
    L.append("## 按联赛（n≥%d 单独展示，其余并入长尾）" % LEAGUE_SHOW_N)
    L.append(_HEADER)
    leagues = res["by_league"]
    shown = sorted((k for k in leagues if leagues[k]["n"] >= LEAGUE_SHOW_N),
                   key=lambda k: -leagues[k]["n"])
    tail = [k for k in leagues if k not in shown]
    for k in shown:
        L.append(_row(k, leagues[k]))
    if tail:
        tail_n = sum(leagues[k]["n"] for k in tail)
        L.append(f"| 长尾({len(tail)}个联赛) | {tail_n} | — | — | — | — | — | — | — | — |")
    L.append("")
    L.append("## 赔率档×选向")
    L.append(_HEADER)
    for k in sorted(res["by_cross"]):
        L.append(_row(k, res["by_cross"][k]))
    L.append("")
    L.append(f"## 系统性偏差排名（仅 n≥{MIN_N}；赔率缺口要求 n赔率≥{MIN_N}）")
    L.append("")
    L.append("按 |校准偏差|（命中率为 Wilson 95% CI；多层比较会放大极值，仅供参考）：")
    for k, m in res["ranking"]["calib"]:
        lo, hi = m["hit_ci95"]
        L.append(f"- {k}: 校准偏差 {m['calib_gap']:+.1%}（n={m['n']}，"
                 f"主推概率 {_pct(m['mean_p_sel'])} vs 命中 {_pct(m['hit_rate'])} "
                 f"[{lo:.1%}, {hi:.1%}]）")
    L.append("")
    L.append("按 |打平线缺口|：")
    for k, m in res["ranking"]["edge"]:
        L.append(f"- {k}: 缺口 {m['edge']:+.1%}（n赔率={m['n_odds']}，"
                 f"命中 {_pct(m['hit_rate_odds'])} vs 打平线 {_pct(m['breakeven'])}，"
                 f"flatROI {m['roi']:+.1%}）")
    L.append("")
    L.append("## 小样本层（不可结论，仅登记）")
    small = [(k, m) for k, m in
             [(f"{name}:{SIDES_ZH.get(k, k) if key == 'by_side' else k}", m)
              for name, key in
              (("赔率档", "by_tier"), ("选向", "by_side"),
               ("联赛", "by_league"), ("赔率档×选向", "by_cross"))
              for k, m in res[key].items()]
             if m["n"] < MIN_N]
    if small:
        L.append("、".join(f"{k}(n={m['n']})" for k, m in sorted(small)))
    else:
        L.append("（无）")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", type=Path, default=LEDGER)
    ap.add_argument("--daily-root", type=Path, default=DAILY_ROOT)
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)
    res = analyze(args.ledger, args.daily_root)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())