#!/usr/bin/env python3
"""中英队名候选的交叉验证（2026-10-28）

## 为什么要「验证」而不是直接写字典

`clv_pinnacle.py` 的 CLV 面板有 375 注卡在 `no_en`：中文队名解不出英文，
对不上 football-data 的行。补字典是最便宜的解法，但**手工写 138 个映射必然出错**，
错一个就会把 A 队的收盘价配到 B 队身上——CLV 是判据，脏数据比没数据更糟。

所以分两步：
1. `assets/team_zh_en_candidates.json`：候选（可多名，只是"猜测"）
2. 本脚本：用**同联赛 + 同日（±1 天）+ 正确主客侧**的 fdc 真实行做交叉验证，
   只有能对上真实行的候选才写进 `assets/team_zh_en.json`。

验证不通过的一律丢弃——宁可少一条，不可错一条。

用法：
    python scripts/verify_team_zh_en.py --dry-run     # 只看验证结果
    python scripts/verify_team_zh_en.py               # 写回字典
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date as _date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))
import clv_pinnacle as cp  # noqa: E402  复用 LEAGUE_MAP / _name_match

CANDIDATES = ROOT / "assets" / "team_zh_en_candidates.json"
DICT = ROOT / "assets" / "team_zh_en.json"
GROUP = "2026-10 交叉验证补全"
MIN_SCORE = 0.6


def _shift(d: str, days: int) -> str:
    y, m, dd = (int(x) for x in d.split("-"))
    return (_date(y, m, dd) + timedelta(days=days)).isoformat()


def load_preds() -> list[dict]:
    """所有每日预测（不只下注场次，样本更大）。"""
    out = []
    for f in sorted(cp.DAILY.glob("*/predictions.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = d.get("predictions", d) if isinstance(d, dict) else d
        for p in rows:
            if p.get("home_team") and p.get("away_team") and p.get("match_id"):
                out.append({
                    "date": p["match_id"][:10],
                    "competition": p.get("competition", ""),
                    "home": p["home_team"],
                    "away": p["away_team"],
                })
    return out


def load_fdc() -> dict:
    idx: dict[tuple, list] = defaultdict(list)
    if not cp.FDC.exists():
        return idx
    for line in cp.FDC.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        idx[(r["src"], r["date"])].append(r)
    return idx


def verify(candidates: dict, preds: list[dict], fdc: dict,
           known: dict[str, str]) -> tuple[dict[str, dict], list[dict]]:
    """逐条验证候选。返回 (verified, rejected)。

    verified[zh] = {"en": 名, "n": 证据条数, "samples": [...]}

    判定标准（三条同时满足）：
      1. 该场的中文联赛能映射到 fdc 文件（LEAGUE_MAP）
      2. 该中文队出现在预测的**同一侧**（主/客），候选英文名与之 token 命中 ≥0.6
      3. 若对手中文也能解析，则对手也必须命中该行的另一侧（防止同队名歧义）
    """
    by_zh: dict[str, list[dict]] = defaultdict(list)
    for p in preds:
        by_zh[p["home"]].append({**p, "side": "home"})
        by_zh[p["away"]].append({**p, "side": "away"})

    verified: dict[str, dict] = {}
    rejected: list[dict] = []

    for zh, cands in candidates.items():
        if not isinstance(cands, list):
            continue
        evidence: dict[str, list[str]] = defaultdict(list)
        for cand in cands:
            for p in by_zh.get(zh, []):
                files = cp.LEAGUE_MAP.get(p["competition"])
                if not files:
                    continue
                for delta in (-1, 0, 1):
                    day = _shift(p["date"], delta)
                    for fname in files:
                        for row in fdc.get((fname, day), []):
                            mine = row["home_en"] if p["side"] == "home" else row["away_en"]
                            theirs = row["away_en"] if p["side"] == "home" else row["home_en"]
                            if cp._name_match(cand, mine) < MIN_SCORE:
                                continue
                            opp_zh = p["away"] if p["side"] == "home" else p["home"]
                            opp_en = known.get(opp_zh) or known.get(opp_zh.rstrip("队"))
                            if opp_en and cp._name_match(opp_en, theirs) < MIN_SCORE:
                                continue  # 对手对不上 → 不是这一行
                            evidence[cand].append(f"{fname} {day} {row['home_en']} vs {row['away_en']}")
        if not evidence:
            rejected.append({"zh": zh, "candidates": cands, "reason": "no_fdc_row"})
            continue
        best = max(evidence, key=lambda k: len(evidence[k]))
        verified[zh] = {"en": best, "n": len(evidence[best]),
                        "samples": evidence[best][:3]}
        # 同名多候选都能对上时留个记录，便于人工复核歧义
        if len(evidence) > 1:
            verified[zh]["also"] = [k for k in evidence if k != best]
    return verified, rejected


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="中英队名候选交叉验证")
    ap.add_argument("--dry-run", action="store_true", help="不写回字典")
    args = ap.parse_args(argv)

    cands_all = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    known = cp.build_zh_en_dict()
    preds = load_preds()
    fdc = load_fdc()
    print(f"候选 {sum(len(v) for k, v in cands_all.items() if isinstance(v, dict))} 条 | "
          f"预测 {len(preds)} 场 | fdc 行 {sum(len(v) for v in fdc.values())}")

    merged: dict[str, str] = {}
    verified_all: dict[str, dict] = {}
    rejected_all: list[dict] = []
    for group, items in cands_all.items():
        if not isinstance(items, dict):
            continue
        v, r = verify(items, preds, fdc, {**known, **merged})
        for zh, info in v.items():
            merged.setdefault(zh, info["en"])
            verified_all[zh] = {**info, "group": group}
        rejected_all.extend({**x, "group": group} for x in r)

    print(f"\n验证通过 {len(verified_all)} 条 / 未通过 {len(rejected_all)} 条")
    for zh, info in sorted(verified_all.items(), key=lambda kv: -kv[1]["n"]):
        extra = f"（歧义候选 {info['also']}）" if info.get("also") else ""
        print(f"  ✓ {zh:<10} → {info['en']:<22} 证据 {info['n']} 条 {extra}")
    if rejected_all:
        print("\n未通过（可能是联赛/日期对不上，或候选名写错）：")
        for x in rejected_all:
            print(f"  ✗ {x['zh']:<10} 候选 {x['candidates']} — {x['reason']}")

    if args.dry_run:
        print("\n--dry-run：未写回字典")
        return 0

    data = json.loads(DICT.read_text(encoding="utf-8"))
    group = data.setdefault(GROUP, {})
    added = 0
    for zh, en in merged.items():
        if zh not in group:
            group[zh] = en
            added += 1
    DICT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n写回 {DICT.name}：新增 {added} 条到「{GROUP}」组")
    return 0


if __name__ == "__main__":
    sys.exit(main())
