#!/usr/bin/env python3
"""CLV 面板覆盖失败的只读审计（2026-11-08，探索工单 #7）

`clv_pinnacle.py` 只报三个失败桶的总数（league_uncovered / no_en / no_match），
本脚本把每个桶拆到**可行动的成因**，并量化「本可回收却丢了」的部分：

- league_uncovered → 按联赛名分布（哪些是 fdc 结构性不覆盖，哪些疑似映射缺口）
- no_en           → prediction 行缺失（含 match_id 缺日期前缀这种可回收的格式漂移）
                    / taken 赔率缺失 / 中英字典解不出（列出具体队名与次数）
- no_match        → 该日该文件根本没有 fdc 行（缓存过期 / 源文件抓取失败 / fdc 缺该场）
                    / 有行但队名对不上（给出最接近的候选与得分）
                    / ±1 天能找到同名行（日期口径漂移，可回收）

**只读**：不写 data/state 下任何文件，不改判定口径；分类顺序与
`clv_pinnacle.main` 完全一致，顶层计数应与面板 stats 对得上（同一份输入时）。
fdc parsed.jsonl 不在 git 里（Actions 缓存传递），缺它时 no_match 只能标
`fdc_unavailable`，先跑 `scripts/fetch_football_data.py` 再审计。

用法：
    python scripts/clv_coverage_audit.py            # JSON 报告打到 stdout
    python scripts/clv_coverage_audit.py --out /tmp/clv_coverage_audit.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import clv_pinnacle as cp  # noqa: E402  复用纯函数与 LEAGUE_MAP，保证口径一致

SAMPLE_CAP = 25  # 每个子成因最多留这么多个样本，计数不受影响


def _shift(d: str, days: int) -> str:
    y, m, dd = (int(x) for x in d.split("-"))
    return (_date(y, m, dd) + timedelta(days=days)).isoformat()


def build_zh_en_dict(root: Path) -> dict:
    """与 clv_pinnacle.build_zh_en_dict 相同，但 root 可参数化（便于测试）。"""
    zh_en: dict = {}
    seed = root / "assets" / "team_zh_en.json"
    if seed.exists():
        try:
            raw = json.loads(seed.read_text(encoding="utf-8"))
            for group in raw.values():
                if isinstance(group, dict):
                    zh_en.update(group)
        except (OSError, ValueError):
            pass
    for f in (root / "data" / "daily").glob("*/context_cache.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
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


def load_bets(root: Path) -> list[dict]:
    bets = []
    ledger = root / "data" / "state" / "review_ledger.jsonl"
    if not ledger.exists():
        return bets
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("best_selection") is not None:
            bets.append(r)
    return bets


def load_fdc(root: Path) -> tuple[dict, dict, bool]:
    """返回 (fdc_by_file, fdc_index[(src,date)], available)。"""
    path = root / "data" / "state" / "fdc" / "parsed.jsonl"
    by_file: dict[str, list] = defaultdict(list)
    index: dict[tuple, list] = defaultdict(list)
    if not path.exists():
        return by_file, index, False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        by_file[r["src"]].append(r)
        index[(r["src"], r["date"])].append(r)
    return by_file, index, True


def _best_match(files: list[str], day: str, home_en: str, away_en: str,
                fdc_index: dict) -> tuple[dict | None, float, dict | None]:
    """同日匹配 + 记录最接近候选（即使低于 0.5 阈值）。

    全 0 分时 best_row 保留该日第一行作诊断样本（说明「fdc 有该日数据，
    只是没有这场」），best 仍为 None，与生产判定一致。
    """
    best, best_score, best_row = None, 0.0, None
    for fname in files:
        for r in fdc_index.get((fname, day), []):
            sc = min(cp._name_match(home_en, r["home_en"]),
                     cp._name_match(away_en, r["away_en"]))
            if best_row is None or sc > best_score:
                best_score, best_row = sc, r
                if sc >= 0.5:
                    best = r
    return best, best_score, best_row


def _date_shift_match(files: list[str], date: str, home_en: str, away_en: str,
                      fdc_index: dict) -> tuple[dict | None, float]:
    """±1 天找同名行（竞彩北京日期 vs fdc 当地日期的时区漂移）。"""
    for shift in (-1, 1):
        alt, score, _ = _best_match(files, _shift(date, shift),
                                    home_en, away_en, fdc_index)
        if alt is not None:
            return alt, score
    return None, 0.0


# ---------------------------------------------------------------------------
# what-if 诊断：量化「如果修了队名归一，能多回收多少」。**不是生产改动**。
# 生产的 clv_pinnacle._norm 用 `[^a-z ]` 直接删掉重音字母（Marítimo→"mar timo"），
# 而不是转写（Maritimo），导致同一支队对不上。这里模拟转写后的匹配。
# ---------------------------------------------------------------------------
# ø/ß/æ 不在 NFKD 分解范围，需显式映射
_TRANSLIT_MAP = {"ø": "o", "Ø": "O", "ß": "ss", "æ": "ae", "Æ": "AE",
                  "đ": "d", "Đ": "D", "ł": "l", "Ł": "L"}

# **诊断假设，非生产映射**：这些别名在 fdc 里确有同场对手可核对（见提案文档），
# 但上线前必须走 verify_team_zh_en.py 的交叉验证，错一个就把 A 队收盘价配到 B 队。
PROPOSED_NAME_ALIASES = {"depor": "la coruna", "sporting cp": "sp lisbon",
                         "cologne": "fc koln"}


def _translit(s: str) -> str:
    """重音转写（Marítimo→Maritimo、Lillestrøm→Lillestrom），而非删字符。"""
    for a, b in _TRANSLIT_MAP.items():
        s = s.replace(a, b)
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _norm_translit(s: str) -> str:
    s = _translit(s).lower()
    s = re.sub(r"\b(fc|sc|if|ac|as|afc|cfc|bk|sk|fk|cf)\b", " ", s)
    s = re.sub(r"[^a-z ]", " ", s)
    return " ".join(s.split())


def _name_match_translit(a: str, b: str) -> float:
    ta = {t for t in _norm_translit(a).split() if len(t) > 2}
    tb = {t for t in _norm_translit(b).split() if len(t) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def _what_if_score(files: list[str], day: str, home: str, away: str,
                   fdc_index: dict, use_alias: bool) -> float:
    if use_alias:
        home = PROPOSED_NAME_ALIASES.get(_norm_translit(home), home)
        away = PROPOSED_NAME_ALIASES.get(_norm_translit(away), away)
    best = 0.0
    for fname in files:
        for r in fdc_index.get((fname, day), []):
            sc = min(_name_match_translit(home, r["home_en"]),
                     _name_match_translit(away, r["away_en"]))
            if sc > best:
                best = sc
    return best


def audit(root: Path) -> dict:
    """只读审计。返回 JSON 可序列化的报告；不写任何文件。"""
    bets = load_bets(root)
    zh_en = build_zh_en_dict(root)
    fdc_by_file, fdc_index, fdc_available = load_fdc(root)
    token_index = cp.build_token_index(fdc_by_file) if fdc_available else {}

    status_path = root / "data" / "state" / "fdc" / "status.json"
    status = {}
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
    latest_by_src = status.get("latest_by_src") or {}
    failed_files = status.get("failed") or {}

    stats = {"bets": len(bets), "league_uncovered": 0, "no_en": 0,
             "no_match": 0, "no_close": 0, "ok": 0}
    uncovered_leagues: Counter = Counter()
    no_en_causes: Counter = Counter()
    no_match_causes: Counter = Counter()
    unresolved_teams: Counter = Counter()
    samples: dict[str, list] = defaultdict(list)
    recoverable = {"match_id_format": 0, "date_shift_no_rows": 0,
                   "date_shift_name_mismatch": 0,
                   "name_translit_only": 0, "name_translit_plus_alias": 0,
                   "dict_candidate_teams": 0, "dict_candidate_bets": 0}

    def add_sample(bucket: str, item: dict) -> None:
        if len(samples[bucket]) < SAMPLE_CAP:
            samples[bucket].append(item)

    taken_cache: dict[str, dict] = {}
    daily = root / "data" / "daily"
    for b in bets:
        date, lg = b.get("date", ""), b.get("league", "")
        mid = b.get("match_id")
        base = {"date": date, "match_id": mid, "league": lg}
        files = cp.LEAGUE_MAP.get(lg)
        if not files:
            stats["league_uncovered"] += 1
            uncovered_leagues[lg] += 1
            continue

        if date not in taken_cache:
            pf = daily / date / "predictions.json"
            preds = []
            if pf.exists():
                try:
                    d = json.loads(pf.read_text(encoding="utf-8"))
                    preds = d.get("predictions", d) if isinstance(d, dict) else d
                except (OSError, ValueError):
                    preds = []
            taken_cache[date] = {p.get("match_id"): p for p in preds}
        pred = taken_cache[date].get(mid)
        if pred is None:
            # 生产口径这里会落到 no_en（taken 取不到）。区分成因：
            stats["no_en"] += 1
            norm = f"{date}_{mid}" if mid and not str(mid).startswith(date) else None
            if norm and norm in taken_cache[date]:
                no_en_causes["prediction_id_format"] += 1
                recoverable["match_id_format"] += 1
                add_sample("prediction_id_format",
                           {**base, "normalized_match_id": norm})
            else:
                no_en_causes["prediction_row_missing"] += 1
                add_sample("prediction_row_missing", base)
            continue
        pred = pred or {}
        sel = int(b["best_selection"])
        taken = pred.get(cp.SELECTION_ODDS[sel])
        if not taken or taken <= 1.0:
            stats["no_en"] += 1
            no_en_causes["taken_odds_missing"] += 1
            add_sample("taken_odds_missing", {**base, "taken": taken})
            continue

        home_zh = pred.get("home_team", "")
        away_zh = pred.get("away_team", "")
        home_en = cp._resolve_team(home_zh, zh_en, token_index)
        away_en = cp._resolve_team(away_zh, zh_en, token_index)
        if not home_en or not away_en:
            stats["no_en"] += 1
            no_en_causes["team_unresolved"] += 1
            missing = [z for z, e in ((home_zh, home_en), (away_zh, away_en)) if not e]
            for z in missing:
                unresolved_teams[z] += 1
            recoverable["dict_candidate_bets"] += 1
            add_sample("team_unresolved", {**base, "unresolved_zh": missing})
            continue

        if not fdc_available:
            stats["no_match"] += 1
            no_match_causes["fdc_unavailable"] += 1
            continue

        best, best_score, best_row = _best_match(files, date, home_en, away_en,
                                                 fdc_index)
        if best is None:
            stats["no_match"] += 1
            day_rows = sum(len(fdc_index.get((f, date), [])) for f in files)
            if day_rows == 0:
                # 该日该文件一行都没有：缓存旧 / 源文件失败 / fdc 缺该日
                if any(f in failed_files for f in files):
                    cause = "source_file_failed"
                elif any(latest_by_src.get(f, "") < date for f in files
                         if f in latest_by_src):
                    cause = "cache_stale_for_date"
                else:
                    cause = "no_fdc_rows_for_date"
                no_match_causes[cause] += 1
                add_sample(cause, {**base, "pair_en": f"{home_en} vs {away_en}"})
                # ±1 天找同名行：日期口径漂移是可回收的
                alt, alt_score = _date_shift_match(files, date, home_en, away_en,
                                                   fdc_index)
                if alt is not None:
                    recoverable["date_shift_no_rows"] += 1
                    add_sample("date_shift_recoverable",
                               {**base, "cause": cause, "fdc_date": alt["date"],
                                "score": round(alt_score, 2)})
            else:
                no_match_causes["name_mismatch"] += 1
                add_sample("name_mismatch", {
                    **base, "pair_en": f"{home_en} vs {away_en}",
                    "best_score": round(best_score, 2),
                    "closest_fdc_row": (f"{best_row['home_en']} vs {best_row['away_en']}"
                                        if best_row else None),
                })
                # 同日有行但对不上：也可能只是日期漂移（该日恰好有别的比赛）
                alt, alt_score = _date_shift_match(files, date, home_en, away_en,
                                                   fdc_index)
                if alt is not None:
                    recoverable["date_shift_name_mismatch"] += 1
                    add_sample("date_shift_recoverable",
                               {**base, "cause": "name_mismatch",
                                "fdc_date": alt["date"],
                                "score": round(alt_score, 2)})
                else:
                    # what-if：重音转写（安全、通用）能否回收；再加别名字典（需交叉验证）
                    sc_t = _what_if_score(files, date, home_en, away_en,
                                          fdc_index, use_alias=False)
                    sc_ta = _what_if_score(files, date, home_en, away_en,
                                           fdc_index, use_alias=True)
                    if sc_t >= 0.5:
                        recoverable["name_translit_only"] += 1
                        add_sample("name_translit_recoverable",
                                   {**base, "pair_en": f"{home_en} vs {away_en}",
                                    "score": round(sc_t, 2)})
                    elif sc_ta >= 0.5:
                        recoverable["name_translit_plus_alias"] += 1
                        add_sample("name_alias_recoverable",
                                   {**base, "pair_en": f"{home_en} vs {away_en}",
                                    "score": round(sc_ta, 2)})
            continue

        bench, _ = cp.pick_close(best, sel)
        if not bench:
            stats["no_close"] += 1
            add_sample("no_close", base)
            continue
        stats["ok"] += 1

    recoverable["dict_candidate_teams"] = len(unresolved_teams)
    report = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fdc_available": fdc_available,
            "fdc_rows": sum(len(v) for v in fdc_by_file.values()),
            "fdc_status_at": status.get("at"),
            "fdc_latest_date": status.get("latest_date"),
            "fdc_failed_files": failed_files,
            "zh_en_dict_size": len(zh_en),
            "bet_date_range": ([min(b["date"] for b in bets),
                                max(b["date"] for b in bets)] if bets else None),
        },
        "stats": stats,
        "pct_of_bets": {k: (round(v / len(bets), 4) if bets else 0.0)
                        for k, v in stats.items() if k != "bets"},
        "league_uncovered_by_league": dict(uncovered_leagues.most_common()),
        "no_en_causes": dict(no_en_causes.most_common()),
        "no_match_causes": dict(no_match_causes.most_common()),
        "unresolved_team_names": dict(unresolved_teams.most_common()),
        "recoverable": recoverable,
        "samples": {k: v for k, v in sorted(samples.items())},
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CLV 面板覆盖失败只读审计")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="仓库根目录（默认脚本所在仓库）")
    parser.add_argument("--out", type=Path, default=None,
                        help="报告写入该路径（默认 stdout）；不要指向 data/state")
    args = parser.parse_args(argv)
    report = audit(args.root)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"报告已写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())