#!/usr/bin/env python3
"""DC challenger 重放：数据扩容 + 时间衰减衰减对比（2026-08-29 深夜）

账本深诊结论：生产 shrinkage DC 只用 match_history.db 348 场训练（357 队，
每队不足 1 场），decay=0.0025 偏强（参照实现扫描最优 0.001）。本脚本在
严格样本外（账本期 2026-07-20 起，全部晚于训练窗终点）对比：

  A 旧包  DB 348 场 + decay 0.0025           ← 现状
  D       DB 348 场 + decay 0.001            ← 只动衰减
  B 新包  matches.csv 5 年窗(归一化队名) + decay 0.001  ← 数据+衰减

评估子集 = 两队都可安全映射进 csv 队名空间的账本场次（K1/日职/杯赛等
译名体系不同者剔除，如实披露偏置）。治理闸：60/40 时间切分双段 t>=1.96。

用法: python3 scripts/dc_challenger_replay.py   （B 臂拟合较慢，可用 --fast 跳过）
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.prediction.shrinkage_dc import (  # noqa: E402
    ShrinkageDCConfig,
    fit_shrinkage_dc,
)
from engine.review.post_match import rps_score  # noqa: E402
from engine.sources.base import MatchResult  # noqa: E402
from engine.team_aliases import canon_csv_team as canon  # noqa: E402

VAL_FRAC = 0.4
CSV_PATH = ROOT / "data" / "historical" / "matches.csv"
DB_PATH = ROOT / "data" / "state" / "match_history.db"
TRAIN_FROM = "2021-07-01"          # decay=0.001 时 5 年权重仍余 16%
CSV_TO = "2026-07-18"              # 账本期（07-20 起）严格留作样本外


def load_db_matches() -> list[MatchResult]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT match_id, date, home_team, away_team, score_home, score_away "
        "FROM match_history WHERE score_home IS NOT NULL AND score_away IS NOT NULL"
    ).fetchall()
    conn.close()
    return [MatchResult(match_id=str(r[0]), match_date=str(r[1]), home_team=str(r[2]),
                        away_team=str(r[3]), home_score=int(r[4]), away_score=int(r[5]),
                        competition="") for r in rows]


def load_csv_matches() -> list[MatchResult]:
    out = []
    with open(CSV_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = r.get("date") or ""
            if not (TRAIN_FROM <= d <= CSV_TO):
                continue
            try:
                hg, ag = int(r["home_score"]), int(r["away_score"])
            except (ValueError, KeyError, TypeError):
                continue
            out.append(MatchResult(match_id=f"csv_{d}_{r['home_team']}_{r['away_team']}",
                                   match_date=d, home_team=canon(r["home_team"]),
                                   away_team=canon(r["away_team"]),
                                   home_score=hg, away_score=ag, competition=r.get("competition", "")))
    return out


def brier(probs, actual):
    return sum((p - (1.0 if i == actual else 0.0)) ** 2 for i, p in enumerate(probs))


def load_eval():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id, date, home_team, away_team, score_home, score_away, match_id "
        "FROM match_history WHERE score_home IS NOT NULL AND score_away IS NOT NULL "
        "ORDER BY date")]
    conn.close()
    for r in rows:
        sh, sa = r["score_home"], r["score_away"]
        r["actual_idx"] = 0 if sh > sa else (1 if sh == sa else 2)
        r["h"], r["a"] = canon(r["home_team"]), canon(r["away_team"])
    return rows


def paired_t(arm, base):
    diffs = [b - a for a, b in zip(base, arm)]
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / max(1, n - 1)
    se = (var / n) ** 0.5 if var > 0 else 0.0
    return mean, (mean / se if se > 0 else 0.0)


def main() -> int:
    fast = "--fast" in sys.argv
    evals = load_eval()
    db_matches = load_db_matches()

    print("== 拟合 ==", flush=True)
    # A/D 臂：滚动拟合（每个评估日只用其之前的 DB 历史，防 train=test 泄漏；
    # 与 weekly_run 的滚动口径一致。冷启动日（历史<min_matches）该日预测记 1/3 并计数）
    def rolling_run(decay: float):
        by_date: dict[str, list] = {}
        for r in evals:
            by_date.setdefault(r["date"], []).append(r)
        bs, hs, rs, cold = [], [], [], 0
        for d in sorted(by_date):
            hist = [m for m in db_matches if m.match_date < d]
            model = None
            if len(hist) >= 20:
                try:
                    model = fit_shrinkage_dc(hist, ShrinkageDCConfig(min_matches=20, decay=decay))
                except (ValueError, RuntimeError):
                    model = None
            for r in by_date[d]:
                p = model.predict_probs(r["h"], r["a"]) if model else (1/3, 1/3, 1/3)
                if model is None:
                    cold += 1
                a = r["actual_idx"]
                bs.append(brier(p, a))
                hs.append(int(max(range(3), key=lambda i: p[i]) == a))
                rs.append(rps_score(p, a))
        return bs, hs, rs, cold

    b_a, h_a, r_a, cold_a = rolling_run(0.0025)
    print(f"A 旧包 滚动拟合 完成（冷启动 {cold_a} 场）", flush=True)
    b_d, h_d, r_d, cold_d = rolling_run(0.001)
    print(f"D 旧数据新衰减 滚动拟合 完成（冷启动 {cold_d} 场）", flush=True)

    m_b = None
    if not fast:
        csv_matches = load_csv_matches()
        cfg_b = ShrinkageDCConfig(min_matches=20, decay=0.001)
        print(f"B 拟合中: csv {len(csv_matches)} 场…", flush=True)
        m_b = fit_shrinkage_dc(csv_matches, cfg_b)
        print(f"B 新包({m_b.n_matches} 场, decay .001) 完成", flush=True)

    # 评估子集：B 臂队名空间内（两臂信息对齐）；B 缺席时用 A 的
    universe = (m_b or m_a_placeholder(evals)).teams if m_b else None
    if m_b is not None:
        subset = [r for r in evals if r["h"] in universe and r["a"] in universe]
    else:
        subset = evals
    split = int(len(subset) * (1 - VAL_FRAC))
    idx_of = {id(r): i for i, r in enumerate(evals)}
    sub_idx = [idx_of[id(r)] for r in subset]
    print(f"\n== 评估（子集 {len(subset)}/{len(evals)} 场，val=后 {len(subset)-split} 场）==", flush=True)

    def run(model):
        bs, hs, rs = [], [], []
        for r in subset:
            p = model.predict_probs(r["h"], r["a"])
            a = r["actual_idx"]
            bs.append(brier(p, a))
            hs.append(int(max(range(3), key=lambda i: p[i]) == a))
            rs.append(rps_score(p, a))
        return bs, hs, rs

    arms: dict[str, tuple] = {}
    arms["A_旧包(滚动,.0025)"] = ([b_a[i] for i in sub_idx], [h_a[i] for i in sub_idx], [r_a[i] for i in sub_idx])
    arms["D_旧数据(滚动,.001)"] = ([b_d[i] for i in sub_idx], [h_d[i] for i in sub_idx], [r_d[i] for i in sub_idx])
    if m_b is not None:
        arms["B_新包(csv+decay.001)"] = run(m_b)

    print(f"\n{'臂':<26}{'全Brier':>9}{'全RPS':>8}{'全命中':>8}   {'valBrier':>9}{'valRPS':>8}{'val命中':>8}   {'t(全)':>7}{'t(val)':>7}")
    base = arms["A_旧包(滚动,.0025)"]
    for name, (bs, hs, rs) in arms.items():
        _, tb_f = (0, 0) if name.startswith("A_") else paired_t(bs, base[0])
        _, tb_v = (0, 0) if name.startswith("A_") else paired_t(bs[split:], base[0][split:])
        print(f"{name:<26}{sum(bs)/len(bs):>9.4f}{sum(rs)/len(rs):>8.4f}{sum(hs)/len(hs):>8.1%}   "
              f"{sum(bs[split:])/len(bs[split:]):>9.4f}{sum(rs[split:])/len(rs[split:]):>8.4f}"
              f"{sum(hs[split:])/len(hs[split:]):>8.1%}   {tb_f:>7.2f}{tb_v:>7.2f}")

    # 生产 model_raw 基线（账本 join，同子集）
    ledger = {}
    for line in (ROOT / "data/state/review_ledger.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            ledger[rec.get("match_id")] = rec
    mb, mh, mr, mn = 0.0, 0, 0.0, 0
    for r in subset:
        rec = ledger.get(r["match_id"])
        if not rec or not rec.get("model_raw"):
            continue
        p = rec["model_raw"]
        if isinstance(p, dict):
            p = [p.get("home", 0), p.get("draw", 0), p.get("away", 0)]
        a = r["actual_idx"]
        mn += 1
        mb += brier(p, a)
        mh += int(max(range(3), key=lambda i: p[i]) == a)
        mr += rps_score(p, a)
    if mn:
        print(f"{'生产 model_raw(参照)':<26}{mb/mn:>9.4f}{mr/mn:>8.4f}{mh/mn:>8.1%}   (同子集 n={mn})")

    # B vs 生产 model_raw 配对显著性（champion/challenger 升级闸的关键证据）
    b_full = arms.get("B_新包(csv+decay.001)")
    if b_full and mn:
        pairs = [(i, r) for i, r in enumerate(subset)
                 if (rec := ledger.get(r["match_id"])) and rec.get("model_raw")]
        b_idx = [i for i, _ in pairs]
        b_vals = [b_full[0][i] for i in b_idx]
        mr_vals = []
        for _, r in pairs:
            rec = ledger[r["match_id"]]
            p = rec["model_raw"]
            if isinstance(p, dict):
                p = [p.get("home", 0), p.get("draw", 0), p.get("away", 0)]
            mr_vals.append(brier(p, r["actual_idx"]))
        _, t_bf = paired_t(b_vals, mr_vals)
        half = len(pairs) // 2
        _, t_bv = paired_t(b_vals[half:], mr_vals[half:])
        print(f"\nB vs 生产 model_raw 配对 t：全 {t_bf:+.2f} / 后半 {t_bv:+.2f}"
              f"（Brier 差为负 = B 更优；双段 |t|>=1.96 = 过闸）")
    return 0


def m_a_placeholder(evals):
    raise SystemExit("需要 B 臂定义评估子集；请勿 --fast 运行完整对比")


if __name__ == "__main__":
    sys.exit(main())
