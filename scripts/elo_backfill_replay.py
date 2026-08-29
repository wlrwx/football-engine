#!/usr/bin/env python3
"""Elo/攻防回填 challenger（2026-08-29 深夜）

背景：生产 team_ratings.json 继承前代项目手工维护的 Elo（1302 队），
账本期内每队仅 1-5 场在线更新（EMA 学习率 0.05/场），攻防仍 85%+ 权重
压在手工先验上——先验新鲜度未知。

本脚本对比两个评级源在 DC 核上的样本外表现（144 场对齐子集，
全部晚于回放窗终点，隔离市场混合项）：
  CUR  现行 team_ratings.json（手工先验 + 少量在线更新）
  BF   matches.csv 近 2 年（2024-07→2026-07-18）顺序回放出的评级

治理闸：60/40 时间切分双段 t>=1.96。默认 dry-run，回填评级落
data/models/team_ratings_backfill.json 供人工审核，--apply 才覆盖生产。

用法: python3 scripts/elo_backfill_replay.py [--apply]
"""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.learning.elo_updater import EloConfig, EloUpdater  # noqa: E402
from engine.prediction.dixon_coles import DixonColesConfig, DixonColesModel  # noqa: E402
from engine.review.post_match import rps_score  # noqa: E402
from engine.team_aliases import canon_csv_team as canon  # noqa: E402

VAL_FRAC = 0.4
CSV_PATH = ROOT / "data" / "historical" / "matches.csv"
DB_PATH = ROOT / "data" / "state" / "match_history.db"
RATINGS = ROOT / "data" / "models" / "team_ratings.json"
BACKFILL_OUT = ROOT / "data" / "models" / "team_ratings_backfill.json"
REPLAY_FROM = "2024-07-01"   # 近 2 年（EMA 0.95/场，2 年足够收敛）
REPLAY_TO = "2026-07-18"     # 账本期严格留作样本外


def load_eval() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT date, home_team, away_team, score_home, score_away, match_id "
        "FROM match_history WHERE score_home IS NOT NULL AND score_away IS NOT NULL "
        "ORDER BY date")]
    conn.close()
    for r in rows:
        sh, sa = r["score_home"], r["score_away"]
        r["actual_idx"] = 0 if sh > sa else (1 if sh == sa else 2)
        r["h"], r["a"] = canon(r["home_team"]), canon(r["away_team"])
    return rows


def brier(probs, actual):
    return sum((p - (1.0 if i == actual else 0.0)) ** 2 for i, p in enumerate(probs))


def paired_t(arm, base):
    diffs = [a - b for a, b in zip(base, arm)]  # brier 差：>0 = arm 更差
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / max(1, n - 1)
    se = (var / n) ** 0.5 if var > 0 else 0.0
    return mean, (mean / se if se > 0 else 0.0)


def predict_metrics(model: DixonColesModel, ratings: dict, subset: list[dict]):
    """用给定评级表出 DC 核概率（不带市场混合，隔离评级层）。"""
    bs, hs, rs = [], [], []
    for r in subset:
        hr = ratings.get(r["h"])
        ar = ratings.get(r["a"])
        if hr is None or ar is None:
            p = (1 / 3, 1 / 3, 1 / 3)
        else:
            pred = model.predict(home=hr, away=ar, market_odds=None)
            p = (pred.home_win_prob, pred.draw_prob, pred.away_win_prob)
        a = r["actual_idx"]
        bs.append(brier(p, a))
        hs.append(int(max(range(3), key=lambda i: p[i]) == a))
        rs.append(rps_score(p, a))
    return bs, hs, rs


def main() -> int:
    apply_ = "--apply" in sys.argv
    evals = load_eval()
    model = DixonColesModel(DixonColesConfig())

    # CUR 臂：现行评级（EloUpdater._load 做归一键合并）
    cur = EloUpdater(RATINGS, EloConfig())
    # BF 臂：csv 近 2 年顺序回放
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "replay_ratings.json"
    bf = EloUpdater(tmp, EloConfig())
    rows = []
    with open(CSV_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = r.get("date") or ""
            if not (REPLAY_FROM <= d <= REPLAY_TO):
                continue
            try:
                rows.append((d, r["home_team"], r["away_team"],
                             int(r["home_score"]), int(r["away_score"]),
                             str(r.get("neutral", "")).lower() == "true"))
            except (ValueError, KeyError, TypeError):
                continue
    rows.sort(key=lambda x: x[0])
    for d, h, a, hg, ag, neutral in rows:
        bf.update(canon(h), canon(a), hg, ag, is_neutral=neutral)
    bf.save()
    print(f"回放 {len(rows)} 场（{REPLAY_FROM}→{REPLAY_TO}），评级 {len(bf.ratings)} 队")

    # 评估子集：两队都在两套评级里（信息对齐）
    subset = [r for r in evals if r["h"] in cur.ratings and r["a"] in cur.ratings
              and r["h"] in bf.ratings and r["a"] in bf.ratings]
    split = int(len(subset) * (1 - VAL_FRAC))
    print(f"评估子集 {len(subset)}/{len(evals)}（val=后 {len(subset)-split}）\n")

    cur_bs, cur_hs, cur_rs = predict_metrics(model, cur.ratings, subset)
    bf_bs, bf_hs, bf_rs = predict_metrics(model, bf.ratings, subset)

    n = len(subset)
    print(f"{'臂':<22}{'全Brier':>9}{'全RPS':>8}{'全命中':>8}   {'valBrier':>9}{'valRPS':>8}{'val命中':>8}   {'t(全)':>7}{'t(val)':>7}")
    print(f"{'CUR_现行评级':<22}{sum(cur_bs)/n:>9.4f}{sum(cur_rs)/n:>8.4f}{sum(cur_hs)/n:>8.1%}   "
          f"{sum(cur_bs[split:])/len(cur_bs[split:]):>9.4f}{sum(cur_rs[split:])/len(cur_rs[split:]):>8.4f}"
          f"{sum(cur_hs[split:])/len(cur_hs[split:]):>8.1%}   {'基线':>7}")
    m_f, t_f = paired_t(bf_bs, cur_bs)
    m_v, t_v = paired_t(bf_bs[split:], cur_bs[split:])
    print(f"{'BF_回填评级':<22}{sum(bf_bs)/n:>9.4f}{sum(bf_rs)/n:>8.4f}{sum(bf_hs)/n:>8.1%}   "
          f"{sum(bf_bs[split:])/len(bf_bs[split:]):>9.4f}{sum(bf_rs[split:])/len(bf_rs[split:]):>8.4f}"
          f"{sum(bf_hs[split:])/len(bf_hs[split:]):>8.1%}   {t_f:>7.2f}{t_v:>7.2f}")

    ok = abs(t_f) >= 1.96 and abs(t_v) >= 1.96 and m_f > 0 and m_v > 0
    print(f"\n裁决：{'✅ 双段过闸（BF 更优）' if ok else '❌ 未过闸——维持现行评级'}"
          f"（t>0 = 回填更好）")

    shutil.copy(tmp, BACKFILL_OUT)
    print(f"回填评级已存 {BACKFILL_OUT.name}（供审核；--apply 才覆盖生产文件）")
    if apply_ and ok:
        shutil.copy(tmp, RATINGS)
        print("✅ 已覆盖生产 team_ratings.json（在线更新将从此基础继续）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
