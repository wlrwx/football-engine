#!/usr/bin/env python3
"""译名缺口扫描（2026-11-17，#97 事后全量化）。

只读回答一个问题：「预测没进站点池」里，有多少是**译名**能救的、多少是站点
**根本没覆盖**。探索工单 #17（匹配缺口归因）的口径答案：11-01~11-17 实测
13 天，未进池预测 ~114 场中译名可救的仅 2 场（布拉干RB、阿克宁顿）——
大头是站点覆盖缺口，**不该再为译名焦虑，也不该有人借「优化匹配」放宽口径**。

纪律（#102 教训：同口径两处实现是隐性事故源）：匹配逻辑**不重写**，直接 import
生产 `challenge_hourly.match_prediction/_canon`；本脚本只读、不写任何文件。

用法:
  .venv/bin/python scripts/alias_gap_scan.py [--days N]   # 默认全部可用天
退出码恒 0（诊断工具，不参与任何判定链）。
"""
from __future__ import annotations

import argparse
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
from challenge_hourly import _canon, _is_squad_variant, match_prediction  # noqa: E402

MIN_RATIO = 0.5


def detect_gap_candidates(pred: dict, pool: list[dict], matched: set) -> dict:
    """高信号译名缺口对：{(站点名, 我方名): {n, ratio, fixture}}。

    成立条件：同一场**另一队**归一化后完全一致，本队相似度 ≥ MIN_RATIO，
    且「互含」与「梯队差异」两类已有规则都解释不了——与「诺茨郡/诺斯郡」同构。
    """
    cands: dict = {}
    unmatched = [m for m in pool if match_prediction(pred, m["home"], m["away"]) is None]
    lost = [k for k in pred if k not in matched]
    for m in unmatched:
        mh, ma = _canon(m["home"]), _canon(m["away"])
        for (ph, pa) in lost:
            phn, pan = _canon(ph), _canon(pa)
            for side in (0, 1):
                anchor_ok = (mh == phn) if side == 0 else (ma == pan)
                if not anchor_ok:
                    continue
                a, b = (ma, pan) if side == 0 else (mh, phn)
                if a == b or a in b or b in a:
                    continue                      # 互含已被生产规则兜住
                if _is_squad_variant(a) != _is_squad_variant(b):
                    continue                        # 梯队/女队差异不是译名
                r = SequenceMatcher(None, a, b).ratio()
                if r < MIN_RATIO:
                    continue
                c = cands.setdefault((a, b), {"n": 0, "ratio": round(r, 2),
                                              "fixture": f"{m['home']}v{m['away']} / {ph}v{pa}"})
                c["n"] += 1
    return cands


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="只读译名缺口扫描（不写文件）")
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 天（默认全部）")
    args = ap.parse_args(argv)
    files = sorted((ROOT / "data/state/njstats/site_odds").glob("2026-1[01]-*.json"))
    if args.days:
        files = files[-args.days:]
    total_lost = total_pred = 0
    per_day, all_cands = [], {}
    for pf in files:
        day = pf.stem
        pred_file = ROOT / "data/daily" / day / "predictions.json"
        if not pred_file.exists():
            continue
        try:
            pool = json.loads(pf.read_text(encoding="utf-8")).get("matches") or []
            preds = json.loads(pred_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            per_day.append(f"{day}: 跳过（数据文件读不了）")
            continue
        pred = {(p["home_team"], p["away_team"]): p for p in preds}
        matched = set()
        for m in pool:
            got = match_prediction(pred, m["home"], m["away"])
            if got is not None:
                matched.add((got["home_team"], got["away_team"]))
        lost = len(pred) - len(matched)
        total_lost += lost
        total_pred += len(pred)
        per_day.append(f"{day}: 池{len(pool)} 预测{len(pred)} 匹配{len(matched)} 未进池{lost}")
        for key, v in detect_gap_candidates(pred, pool, matched).items():
            a = all_cands.setdefault(key, {**v, "days": []})
            a["n"] += v["n"]
            a["days"].append(day)
    print("\n".join(per_day))
    print(f"合计: 预测 {total_pred} 场，未进池 {total_lost} 场")
    print("--- 译名缺口候选（同场另一队同名，本站点名/我方名）---")
    if not all_cands:
        print("（无——未进池都是站点覆盖问题，别再改匹配口径）")
    for (site_name, our_name), v in sorted(all_cands.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{site_name!r} vs {our_name!r} | 次数{v['n']} 相似{v['ratio']} "
              f"例:{v['fixture']} 天:{','.join(v['days'][-3:])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
