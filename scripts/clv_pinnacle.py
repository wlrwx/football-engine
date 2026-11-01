#!/usr/bin/env python3
"""下注级 CLV · Pinnacle 收盘基准（2026-10-11，全免费数据）

football-data.co.uk 的 60k+ 场历史 CSV 自带 Pinnacle 收盘价（PSCH/PSCD/PSCA），
把账本里已结算的下注匹配上去，得到"vs 尖锐收盘"的 CLV——之前用 DJYY 市场均价
只能算近似口径，这是行业标准口径的免费实现。

**口径限制（2026-10-28 实测，别再当 bug 追）**：
`new/` 系列文件里 Pinnacle 收盘价到 2025 年后基本为空——BRA 2026 年 319 行，
PSCH 非空 0 行（2012–2024 年基本 100%），所以近年注只能退到 B365 收盘。
基准阶梯是 psc → b365c → maxc → avgc，报告里 `benchmark_stats` 按基准分开统计；
读 `overall` 之前先看它用的是哪一档，B365 收盘比 Pinnacle 软，
把两者混成一句「vs 尖锐收盘」会高估我们贴近市场的程度。

匹配链：账本行 →（队名 ZH→EN 字典，源自 context_cache 阵容的中英双名）
       →（同日 + 同联赛文件）→ football-data 行 → PSC 收盘价。
未覆盖：欧国联/国际赛/K联赛等 fdc 没有的联赛，以及字典缺队的场次。
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "data" / "state" / "review_ledger.jsonl"
DAILY = ROOT / "data" / "daily"
FDC = ROOT / "data" / "state" / "fdc" / "parsed.jsonl"
OUT = ROOT / "data" / "state" / "clv_pinnacle.json"

# 我们的中文联赛 → fdc 缓存文件
LEAGUE_MAP = {
    "英超": ["2627_E0", "2526_E0"], "英冠": ["2627_E1", "2526_E1"],
    "德甲": ["2627_D1", "2526_D1"], "德乙": ["2627_D2", "2526_D2"],
    "西甲": ["2627_SP1", "2526_SP1"], "西乙": ["2627_SP2", "2526_SP2"],
    "意甲": ["2627_I1", "2526_I1"], "意乙": ["2627_I2", "2526_I2"],
    "法甲": ["2627_F1", "2526_F1"], "法乙": ["2627_F2", "2526_F2"],
    "荷甲": ["2627_N1", "2526_N1"], "葡超": ["2627_P1", "2526_P1"],
    "苏超": ["2627_SC0", "2526_SC0"], "希腊超": ["2627_G1"], "土超": ["2627_T1"],
    "瑞士超": ["2627_SW1"],
    "日职": ["new_JPN"], "日乙": ["new_JPN"], "日职乙": ["new_JPN"],
    "中超": ["new_CHN"], "巴甲": ["new_BRA"], "巴乙": ["new_BRA"],
    "芬超": ["new_FIN"], "挪超": ["new_NOR"], "瑞超": ["new_SWE"],
    "瑞典超": ["new_SWE"], "美职": ["new_USA"], "美职联": ["new_USA"],
    "阿甲": ["new_ARG"], "罗甲": ["new_RO1"], "波兰甲": ["new_POL"],
    "俄超": ["new_RUS"], "墨超": ["new_MEX"], "奥甲": ["new_AUT"],
}
SELECTION_ODDS = ("home_odds", "draw_odds", "away_odds")


def _norm(s: str) -> str:
    """队名宽松归一：去 FC/SC/IF 等前后缀、小写、去非字母。"""
    s = s.lower()
    s = re.sub(r"\b(fc|sc|if|ac|as|afc|cfc|bk|sk|fk|cf)\b", " ", s)
    s = re.sub(r"[^a-z ]", " ", s)
    return " ".join(s.split())


def _tokens(s: str) -> set:
    return {t for t in _norm(s).split() if len(t) > 2}


def build_zh_en_dict() -> dict:
    """ZH→EN 队名字典：config/team_zh_en.json 人工种子 + 缓存阵容中英双名自动积累。"""
    zh_en: dict = {}
    seed = ROOT / "assets" / "team_zh_en.json"
    if seed.exists():
        try:
            raw = json.loads(seed.read_text(encoding="utf-8"))
            for group in raw.values():
                if isinstance(group, dict):
                    zh_en.update(group)
        except Exception:
            pass
    for f in DAILY.glob("*/context_cache.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for v in d.values():
            if not isinstance(v, dict):
                continue
            lu = v.get("lineups")
            if not isinstance(lu, dict):
                continue
            for side in ("home", "away"):
                s = lu.get(side) or {}
                if s.get("name_zh") and s.get("name_en"):
                    zh_en.setdefault(s["name_zh"], s["name_en"])
    return zh_en


def _resolve_team(zh: str, zh_en: dict, tokens_index: dict) -> str | None:
    """ZH → 英文名。字典直查 → 字典值归一化后查 token 索引。"""
    en = zh_en.get(zh)
    if en:
        return en
    # 常见变体：去掉"队"字再试
    en = zh_en.get(zh.rstrip("队"))
    if en:
        return en
    for t in tokens_index:
        if t in zh or zh.rstrip("队") in t:
            return tokens_index[t]
    return None


def build_token_index(fdc_by_file: dict) -> dict:
    """英文名 token 组合 → 规范英文名（从 CSV 数据侧建）。"""
    idx: dict = {}
    for rows in fdc_by_file.values():
        for r in rows:
            for name in (r["home_en"], r["away_en"]):
                key = " ".join(sorted(_tokens(name)))
                if key and key not in idx:
                    idx[key] = name
    return idx


def _name_match(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


# 基准阶梯：越靠前越锐。sel 是本注选中的结果（0=主胜 1=平 2=客胜）。
_CLOSE_LADDER = (("psc", "pinnacle"), ("b365c", "b365"),
                 ("maxc", "max_market"), ("avgc", "avg_market"))


def pick_close(row: dict, sel: int) -> tuple[float | None, str | None]:
    """按阶梯取收盘价，返回 (价格, 基准名)。

    2026-10-28 抽出：原来这段内联在主循环里，无法单测。它的口径直接决定
    「CLV 是 vs 尖锐收盘还是 vs 软收盘」，值得锁一个测试——
    实测 2026 年的行 psc_* 普遍为空（源站不给 Pinnacle 收盘了），
    所以近年注基本都退到 b365，报告必须按 benchmark 分开统计。
    """
    for prefix, name in _CLOSE_LADDER:
        trio = [row.get(f"{prefix}_{s}") for s in ("h", "d", "a")]
        if sel < len(trio) and trio[sel]:
            return trio[sel], name
    return None, None


def main() -> int:
    zh_en = build_zh_en_dict()

    # fdc 索引：(file, date) → [rows]
    fdc_by_file: dict[str, list] = defaultdict(list)
    with open(FDC, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            fdc_by_file[r["src"]].append(r)
    fdc_index: dict[tuple, list] = defaultdict(list)
    for rows in fdc_by_file.values():
        for r in rows:
            fdc_index[(r["src"], r["date"])].append(r)
    token_index = build_token_index(fdc_by_file)

    bets = []
    for line in open(LEDGER, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("best_selection") is None:
            continue
        bets.append(r)

    # 账本行 → 当日 predictions 取 taken 价（队名中文）
    taken_cache: dict[str, dict] = {}
    stats = {"bets": len(bets), "league_uncovered": 0, "no_en": 0, "no_match": 0,
             "no_close": 0, "ok": 0}
    matched = []
    for b in bets:
        date, lg = b.get("date", ""), b.get("league", "")
        files = LEAGUE_MAP.get(lg)
        if not files:
            stats["league_uncovered"] += 1
            continue
        if date not in taken_cache:
            pf = DAILY / date / "predictions.json"
            preds = []
            if pf.exists():
                try:
                    d = json.loads(pf.read_text(encoding="utf-8"))
                    preds = d.get("predictions", d) if isinstance(d, dict) else d
                except Exception:
                    preds = []
            taken_cache[date] = {p.get("match_id"): p for p in preds}
        pred = taken_cache[date].get(b.get("match_id"), {})
        sel = int(b["best_selection"])
        taken = pred.get(SELECTION_ODDS[sel])
        home_zh = pred.get("home_team", "")
        away_zh = pred.get("away_team", "")
        if not taken or taken <= 1.0:
            stats["no_en"] += 1
            continue

        home_en = _resolve_team(home_zh, zh_en, token_index)
        away_en = _resolve_team(away_zh, zh_en, token_index)
        if not home_en or not away_en:
            stats["no_en"] += 1
            continue

        # 同日 + 同文件 + 队名匹配
        best, best_score = None, 0.0
        for fname in files:
            for r in fdc_index.get((fname, date), []):
                sc = min(_name_match(home_en, r["home_en"]),
                         _name_match(away_en, r["away_en"]))
                if sc > best_score:
                    best, best_score = r, sc
        if not best or best_score < 0.5:
            stats["no_match"] += 1
            continue
        # 收盘基准择锐：Pinnacle 收盘 → B365 收盘 → 全市场最高收盘 → 均值收盘
        bench, bench_name = pick_close(best, sel)
        if not bench:
            stats["no_close"] += 1
            continue
        clv = taken / bench - 1.0
        stats["ok"] += 1
        matched.append({
            "date": date, "match_id": b.get("match_id"), "tier": b.get("confidence_tier"),
            "taken": taken, "close": bench, "benchmark": bench_name,
            "clv": round(clv, 4), "score": round(best_score, 2),
            "pair": f"{home_en} vs {away_en}",
        })

    report = {"stats": stats, "records": matched}
    if matched:
        clvs = [m["clv"] for m in matched]
        n = len(clvs)
        mean = statistics.mean(clvs)
        sd = statistics.stdev(clvs) if n > 1 else 0.0
        report["overall"] = {
            "n": n,
            "mean": round(mean, 4),
            "median": round(statistics.median(clvs), 4),
            "pos_pct": round(sum(1 for x in clvs if x > 0) / n, 3),
            "t": round(mean / (sd / n ** 0.5), 2) if sd > 0 else 0.0,
        }
        # 2026-10-28：按基准分开统计。
        # 起因是「258 条全部 benchmark=b365」被当成匹配 bug 查了一轮，结论是源数据本身：
        # new/ 文件里 PSCH 在 2025 年之后基本为空（BRA 2026 年 319 行 PSCH 非空 0），
        # 退到 B365 收盘是唯一选择。那就必须把口径写进报告，不能一句「vs 尖锐收盘」带过——
        # B365 的收盘比 Pinnacle 软，混在一起讲会高估「我们贴近市场」的程度。
        by_bench: dict[str, list[float]] = defaultdict(list)
        for m in matched:
            by_bench[m.get("benchmark") or "unknown"].append(m["clv"])
        report["benchmark_stats"] = {
            k: {
                "n": len(v),
                "mean": round(statistics.mean(v), 4),
                "median": round(statistics.median(v), 4),
                "pos_pct": round(sum(1 for x in v if x > 0) / len(v), 3),
            }
            for k, v in sorted(by_bench.items())
        }
        report["latest_matched_date"] = max(m["date"] for m in matched)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"stats": stats, "overall": report.get("overall"),
                      "benchmark_stats": report.get("benchmark_stats"),
                      "latest_matched_date": report.get("latest_matched_date")},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
