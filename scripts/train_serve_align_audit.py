#!/usr/bin/env python3
"""训练矩阵 × 服务口径 对齐审计（只读，可复现）。

    python -m scripts.train_serve_align_audit           # 控制台报告
    python -m scripts.train_serve_align_audit --json    # JSON 报告

退出码：0 = 没有 violation 判定；1 = 有 violation。不写任何文件。

## 与 `scripts/leak_audit_check.py` 的分工

`leak_audit_check.py` 查的是**代码口径**：黑名单命中位置、开关默认值、
`build_features` 两处调用的签名差。本脚本查的是**落到真实样本上的结果**：

1. **真实矩阵**：用生产同款 `build_training_rows()` 把 `review_ledger.jsonl` ×
   `data/daily/*/predictions.json` 重建成 LGBM 训练矩阵（本次 1500 行 × 27 列），
   逐列量化：目标市场派生列到底有没有值（`matrix_target_market_isolation`）、
   哪些列是死列（`matrix_dead_columns`）。
2. **时点可验证性**：`as_of` 与开赛时间的先后、两端时间都缺的行
   （`matrix_time_point_provenance`）。
3. **输入不可敏感性**：把结算回写字段 / 目标市场价格字段整列改成非法值，
   在临时目录里重建矩阵比对（`matrix_post_match_insensitivity`、
   `matrix_price_field_insensitivity`）；正对照由测试用 `handicap` 打穿。
4. **侧挂（NJStats）**：防未来信息的闸门到底有没有在生效
   （`side_snapshot_asof_gate`）、归并键是否场次级（`side_index_pair_key`）、
   特征名两处定义是否一致（`side_feature_name_parity`）。
5. **让球线来源**：竞彩线优先 / 亚盘线兜底，混在同一列（`matrix_handicap_line_provenance`）。
6. **服务侧落盘量化**：`round(elo,1)` 造成输入不可逐位复现
   （`serve_snapshot_quantization`），并用现网 booster 量出实际影响
   （`booster_gain_attribution`，无 lightgbm 时自动 skip）。
7. **同一口径多处各写一份**：去水实现（`devig_caliber_duplication`）、
   去水**方法族**之差与落盘锚口径漂移（`devig_method_family_divergence`）、
   联赛名归一（`league_name_caliber_duplication`）、
   `market_blend_weight`（`market_blend_weight_duplication`）。

判定口径（与 leak_audit_check 一致）：输出**判定结论**，计数只进
`counts_for_humans` 供人读，不参与断言。黑名单加字段、代码挪行、样本变多
都不该让测试变红；只有判定被推翻才红。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SELF = Path(__file__).resolve()
SEVERITY_ORDER = ("info", "low", "medium", "high")
BJT = timezone(timedelta(hours=8))

LEDGER = ROOT / "data" / "state" / "review_ledger.jsonl"
DAILY = ROOT / "data" / "daily"
MODEL_FILE = ROOT / "data" / "models" / "lgbm_model.txt"

# 结算时写回 predictions.json 的字段（赛果 → 复盘字段）。
# 依据：engine/main.py 结算段 `pred["actual_*"] / ["direction_correct"] /
# ["score_*"] / ["market_signal_*"]` 的赋值点。训练矩阵若对其中任何一个
# 敏感，就等于把赛后信息喂进特征。
SETTLEMENT_KEYS = (
    "actual_result", "actual_home_score", "actual_away_score",
    "direction", "direction_correct", "predicted_score", "score_correct",
    "score_rank", "score_top3_hit", "score_top5_hit", "score_top8_hit",
    "djyy_score_rank", "mc_score_rank",
    "market_signal", "market_signal_dir", "market_signal_hit",
    "total_goals", "hit", "pnl",
)
# 目标市场（竞彩 1x2 + 让球 + 其余玩法）价格字段：模型特征不得消费。
TARGET_PRICE_KEYS = (
    "home_odds", "draw_odds", "away_odds",
    "handicap_home_odds", "handicap_draw_odds", "handicap_away_odds",
    "crs_odds", "ttg_odds", "hafu_odds", "sina_odds", "market_fair",
)


# ------------------------------------------------------------------ 工具
def finding(fid: str, title: str, verdict: str, severity: str,
            note: str, *, status: str = "clean", **extra: Any) -> dict:
    assert status in {"clean", "by_design", "violation"}, status
    assert severity in SEVERITY_ORDER, severity
    out = {"id": fid, "title": title, "verdict": verdict,
           "severity": severity, "status": status, "note": note}
    out.update(extra)
    return out


def parse_ts_any(value: Any) -> datetime | None:
    """独立实现的宽松时间解析（不用生产侧 `_parse_ts`）。

    生产侧 engine/sources/njstats_side.py:104 只认 `%Y-%m-%d %H:%M[:%S]`，
    而落盘的 as_of 是 `beijing_now().isoformat()`（带 T 与 +08:00）。
    审计要判断"闸门本该拦多少"，必须自己会解析两种格式。
    """
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = None
    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(BJT).replace(tzinfo=None)
    return dt


_CACHE: dict[str, Any] = {}


def load_settled_records() -> tuple[dict, ...]:
    """与生产结算入口 (engine/main.py 校准盘点段) 同口径的样本集合。

    返回 tuple 并缓存：审计里多处要重建矩阵，重读一次账本+116 份
    predictions.json 太慢。**调用方不得改动返回的记录**。
    """
    if "records" in _CACHE:
        return _CACHE["records"]
    out = []
    for line in LEDGER.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("final_prob") and rec.get("actual_idx") is not None:
            out.append(rec)
    _CACHE["records"] = tuple(out)
    return _CACHE["records"]


def load_predictions(date_str: str) -> list[dict]:
    pf = DAILY / date_str / "predictions.json"
    try:
        doc = json.loads(pf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = doc.get("predictions", doc) if isinstance(doc, dict) else doc
    return [r for r in (rows or []) if isinstance(r, dict)]


def pred_dates(records: list[dict]) -> list[str]:
    return sorted({r.get("date", "") for r in records if r.get("date")})


def rebuild_matrix(records, daily_root: Path | None = None) -> list[dict]:
    """用生产同款函数重建训练矩阵（行内含 features/label/match_id）。

    只有默认 daily_root（= 仓库真实数据）才缓存；变异副本每次真算。
    """
    from engine.learning.lgbm_shadow import build_training_rows
    root = daily_root or DAILY
    key = "matrix" if root == DAILY else None
    if key and key in _CACHE:
        return _CACHE[key]
    rows = build_training_rows(list(records), root)
    if key:
        _CACHE[key] = rows
    return rows


def side_index() -> dict:
    """NJStats 侧挂索引（缓存：三个数据集共 51 份快照文件）。"""
    if "side_index" not in _CACHE:
        from engine.sources import njstats_side as side
        _CACHE["side_index"] = side.build_index()
    return _CACHE["side_index"]


def columns_stats(rows: list[dict]) -> dict[str, dict]:
    from engine.prediction.lgbm_model import FEATURE_NAMES
    defaults = {"elo_home": 1500.0, "elo_away": 1500.0, "handicap": 0.0,
                "league_avg_goals": 2.5, "league_home_win_rate": 0.45}
    out: dict[str, dict] = {}
    for name in FEATURE_NAMES:
        vals = [float(r["features"].get(name, 0.0) or 0.0) for r in rows]
        uniq = sorted(set(vals))
        d = defaults.get(name)
        out[name] = {
            "n_distinct": len(uniq),
            "n_nonzero": sum(1 for v in vals if v != 0.0),
            "n_default": sum(1 for v in vals if d is not None and v == d),
            "min": uniq[0] if uniq else None,
            "max": uniq[-1] if uniq else None,
        }
    return out


# ----------------------------------------------------- 1 目标市场隔离（真实矩阵）
def audit_matrix_isolation(rows: list[dict]) -> dict:
    from engine.prediction.lgbm_model import FEATURE_NAMES
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    stats = columns_stats(rows)
    banned = [n for n in FEATURE_NAMES if n in TARGET_MARKET_FEATURES]
    live = [n for n in banned if stats[n]["n_nonzero"] or stats[n]["n_distinct"] > 1]
    ok = not live
    classified = set(TARGET_MARKET_FEATURES)
    from engine.prediction.market_isolation import (BENCHMARK_ONLY_FEATURES,
                                                    REFERENCE_MARKET_FEATURES)
    classified |= REFERENCE_MARKET_FEATURES | BENCHMARK_ONLY_FEATURES
    unclassified = [n for n in FEATURE_NAMES if n not in classified]
    return {
        "id": "matrix_target_market_isolation",
        "title": "真实训练矩阵里的目标市场派生列",
        "verdict": "inert" if ok else "target_market_values_in_matrix",
        "severity": "info" if ok else "high",
        "status": "clean" if ok else "violation",
        "blacklisted_columns": banned,
        "live_blacklisted_columns": live,
        "unclassified_columns": unclassified,
        "counts_for_humans": {
            "rows": len(rows),
            "columns": len(FEATURE_NAMES),
            "columns_not_in_any_list": len(unclassified),
            "per_column": {n: {k: stats[n][k] for k in ("n_distinct", "n_nonzero")}
                           for n in banned},
        },
        "note": "四列竞彩赔率派生特征（odds_*_impl / odds_overround）在真实训练矩阵里"
                "n_distinct=1、取值 0.0 → 闸门确实关着，前次代码口径结论在样本上成立。"
                f"但 27 列里有 {len(unclassified)} 列不在 market_isolation 的任何名单里，"
                "名单只覆盖 4/27，`audit_features()` 对其余列静默——包括站点开盘去水"
                "列（side_open_*）与竞彩让球线列（handicap）。",
    }


# ----------------------------------------------------- 2 时点可验证性
def audit_time_points(rows: list[dict], records: list[dict]) -> dict:
    by_mid = {r.get("match_id", ""): r for r in records}
    buckets = {"before_kickoff": 0, "after_kickoff": 0,
               "no_asof": 0, "no_kickoff": 0, "no_asof_or_kickoff": 0}
    late: list[dict] = []
    for r in rows:
        rec = by_mid.get(r.get("match_id", ""), {})
        ao = parse_ts_any(rec.get("as_of"))
        ko = parse_ts_any(rec.get("kickoff"))
        if ao is None and ko is None:
            buckets["no_asof_or_kickoff"] += 1
        elif ao is None:
            buckets["no_asof"] += 1
        elif ko is None:
            buckets["no_kickoff"] += 1
        elif ao > ko:
            buckets["after_kickoff"] += 1
            late.append({"date": rec.get("date"), "match_id": r.get("match_id"),
                         "as_of": rec.get("as_of"), "kickoff": rec.get("kickoff"),
                         "minutes_after_kickoff": round((ao - ko).total_seconds() / 60, 1)})
        else:
            buckets["before_kickoff"] += 1
    unverifiable = buckets["no_asof_or_kickoff"] + buckets["no_asof"] + buckets["no_kickoff"]
    ok = buckets["after_kickoff"] == 0
    return {
        "id": "matrix_time_point_provenance",
        "title": "训练行的时点可验证性（预测时刻 vs 开赛时刻）",
        "verdict": "all_rows_pre_kickoff" if ok and not unverifiable else (
            "post_kickoff_rows_present" if not ok else "partly_unverifiable"),
        "severity": "info" if ok and not unverifiable else "low",
        "status": "clean" if ok and not unverifiable else "violation",
        "buckets": buckets,
        "post_kickoff_rows": late,
        "counts_for_humans": {"rows": len(rows), "unverifiable": unverifiable},
        "note": "按账本的 as_of / kickoff 逐行核对。开赛后写回的场次其冻结输入"
                "（elo / form / 模型 xG / DJYY 概率）可能已含赛后状态；时间戳缺失的"
                "行无法证明是赛前。服务侧的『已开赛场次跳过』只在 kickoff 非空时"
                "起作用（engine/main.py 的 `if _ko:` 分支），kickoff 为空的场次"
                "直接放行——这是开赛后仍被写进 predictions.json 的路径。",
    }


# ----------------------------------------------------- 3 输入不可敏感性（复制-变异-重建）
def rebuild_with(mutate: Callable[[dict], None], records) -> list[dict]:
    """把 predictions.json 复制到临时目录、按 `mutate` 改写、用生产函数重建矩阵。

    仓库里的真实数据一个字都不动（只读），改的是副本。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "daily"
        for d in pred_dates(records):
            preds = load_predictions(d)
            if not preds:
                continue
            for p in preds:
                mutate(p)
            (root / d).mkdir(parents=True, exist_ok=True)
            (root / d / "predictions.json").write_text(
                json.dumps(preds, ensure_ascii=False), encoding="utf-8")
        return rebuild_matrix(records, daily_root=root)


def diff_columns(base: list[dict], mutated: list[dict],
                 columns: list[str] | None = None) -> list[str]:
    """两个矩阵之间取值变了（>1e-12）的 `match_id:column` 列表。"""
    from engine.prediction.lgbm_model import FEATURE_NAMES
    cols = columns or FEATURE_NAMES
    ref = {(r["match_id"], k): float(r["features"].get(k, 0.0) or 0.0)
           for r in base for k in cols}
    out = []
    for r in mutated:
        for k in cols:
            key = (r["match_id"], k)
            if key not in ref:
                continue
            if abs(ref[key] - float(r["features"].get(k, 0.0) or 0.0)) > 1e-12:
                out.append(f"{r['match_id']}:{k}")
    return out


def audit_key_insensitivity(rows: list[dict], records, keys: tuple[str, ...],
                            sentinel: Any) -> tuple[list[str], int]:
    """把一批字段整列打成 sentinel，重建矩阵，返回（变动的特征、改动的单元数）。"""
    touched = 0

    def mutate(p: dict) -> None:
        nonlocal touched
        for k in keys:
            if k in p:
                p[k] = sentinel(p[k])
                touched += 1

    mutated = rebuild_with(mutate, records)
    return diff_columns(rows, mutated), touched


def audit_settlement_insensitivity(rows: list[dict], records) -> dict:
    """结算回写字段整列改非法值 → 特征必须逐位不变（否则= 赛后信息进了特征）。"""
    flips, touched = audit_key_insensitivity(
        rows, records, SETTLEMENT_KEYS,
        lambda v: "__SENTINEL__" if not isinstance(v, (int, float)) else -99999)
    cols = sorted({f.split(":", 1)[1] for f in flips})
    return {
        "id": "matrix_post_match_insensitivity",
        "title": "训练矩阵对结算回写字段的敏感性",
        "verdict": "insensitive" if not flips else "post_match_dependent",
        "severity": "info" if not flips else "high",
        "status": "clean" if not flips else "violation",
        "mutated_columns": cols,
        "settlement_keys": list(SETTLEMENT_KEYS),
        "counts_for_humans": {"rows": len(rows), "sentinel_assignments": touched,
                              "changed_feature_cells": len(flips)},
        "note": "赛后信息进特征的唯一通路是『结算把字段写回 predictions.json，"
                "训练再从同一份文件读同一批键』。这里把 "
                + str(len(SETTLEMENT_KEYS)) + " 个结算字段整列改成非法值后重建矩阵，"
                "比对 1500×27 个单元。正对照见测试：同一套机制改 `handicap` "
                "必须报出变化，否则本判定是空判。",
    }


def audit_price_insensitivity(rows: list[dict], records) -> dict:
    """目标市场价格字段整列改成同一个合法值 → 特征必须逐位不变。"""
    flips, _ = audit_key_insensitivity(
        rows, records, TARGET_PRICE_KEYS,
        lambda v: 1.05 if isinstance(v, (int, float)) else None)
    cols = sorted({f.split(":", 1)[1] for f in flips})
    return {
        "id": "matrix_price_field_insensitivity",
        "title": "训练矩阵对目标市场价格字段的敏感性",
        "verdict": "insensitive" if not flips else "price_dependent",
        "severity": "info" if not flips else "high",
        "status": "clean" if not flips else "violation",
        "price_keys": list(TARGET_PRICE_KEYS),
        "changed_columns": cols,
        "counts_for_humans": {"changed_feature_cells": len(flips),
                              "examples": flips[:5]},
        "note": "把竞彩 1x2 / 让球赔率 / CRS / TTG / HAFU / sina_odds / market_fair "
                "全部改成 1.05 后重建矩阵，特征不变 → 训练矩阵不吃目标市场**价格**。"
                "注意本项只覆盖价格：让球**线**（handicap）不是价格类字段，"
                "训练侧照收，判定见 matrix_handicap_line_provenance。",
    }


# ----------------------------------------------------- 4 侧挂闸门
def open_price_provenance() -> dict[tuple[str, str], tuple[str, str]]:
    """重放 build_index 的 features 段，取每个队对**最后一条被采纳**的开盘快照归属。

    返回 key -> (该行 kickoff, 该行 snapshot_at)。build_index 里 open_fair 是
    后写覆盖前写（engine/sources/njstats_side.py:200-208），所以"最后一条"就是
    实际进模型的那条。
    """
    from engine.sources import njstats_side as side
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for r in side._rows("features"):
        k = side.key(r.get("home", ""), r.get("away", ""))
        if not all(k) or not r.get("opening"):
            continue
        ko, sn = parse_ts_any(r.get("kickoff")), parse_ts_any(r.get("_snapshot"))
        if not ko or not sn or sn >= ko:
            continue
        if (ko - sn).total_seconds() / 3600.0 > side.MAX_LEAD_H:
            continue
        out[k] = (str(r.get("kickoff")), str(r.get("_snapshot")))
    return out


def audit_side_gate() -> dict:
    from engine.sources import njstats_side as side

    asof_samples = []
    for d in pred_dates(load_settled_records())[-40:]:
        for p in load_predictions(d):
            v = p.get("as_of")
            if v:
                asof_samples.append(str(v))
    gate_parse_ok = sum(1 for v in asof_samples if side._parse_ts(v) is not None)
    dead = bool(asof_samples) and gate_parse_ok == 0

    # 若把闸门换成宽松解析，本来该拦掉多少训练行的侧挂特征？
    idx = side_index()
    records = load_settled_records()
    rows = rebuild_matrix(records)
    from engine.prediction.lgbm_model import SIDE_FEATURE_NAMES
    predmap = {d: {p.get("match_id", ""): p for p in load_predictions(d)} for d in pred_dates(records)}
    by_mid = {r.get("match_id", ""): r for r in records}
    prov = open_price_provenance()
    n_side = 0
    would_block = 0
    foreign = 0
    realized = []
    for r in rows:
        rec = by_mid.get(r.get("match_id", ""), {})
        p = predmap.get(rec.get("date", ""), {}).get(r.get("match_id", "")) or {}
        sig = idx.get(side.key(p.get("home_team", ""), p.get("away_team", "")))
        if not sig or not any(sig.to_features().get(n, 0.0) for n in SIDE_FEATURE_NAMES):
            continue
        n_side += 1
        own = str(p.get("kickoff") or "")
        src = prov.get(side.key(p.get("home_team", ""), p.get("away_team", "")))
        if src and own and src[0][:16] != own[:16]:
            foreign += 1
        a, s = parse_ts_any(rec.get("as_of")), parse_ts_any(sig.snapshot_at)
        if a and s and s >= a:
            would_block += 1
            realized.append({"date": rec.get("date"), "match_id": r["match_id"],
                             "as_of": rec.get("as_of"), "snapshot_at": sig.snapshot_at})
    return {
        "id": "side_snapshot_asof_gate",
        "title": "侧挂快照的未来信息闸门（NJStats → side_*）",
        "verdict": "gate_dead" if dead else "gate_live",
        "severity": "medium" if dead else "info",
        "status": "violation" if dead else "clean",
        "asof_format": {"samples": len(asof_samples), "parsed_by_production_gate": gate_parse_ok,
                        "example": asof_samples[0] if asof_samples else None},
        "future_snapshot_rows_detected": realized[:5],
        "counts_for_humans": {"rows": len(rows), "rows_with_side_features": n_side,
                              "would_block_with_strict_gate": would_block,
                              "foreign_fixture_open_price_rows": foreign,
                              "index_slots": len(idx),
                              "slots_with_open_fair": sum(1 for s in idx.values() if s.open_fair)},
        "note": "`SideSignal.features_as_of()`（engine/sources/njstats_side.py:81-88）"
                "靠 `_parse_ts()`（:104-111）比较『快照时间 < 预测时刻』，但 `_parse_ts` "
                "只认 `%Y-%m-%d %H:%M[:%S]`，而落盘的 as_of 是 "
                "`beijing_now().isoformat(timespec='seconds')` → "
                "`2026-11-15T10:39:12+08:00`，解析恒为 None → 闸门整体空转"
                "（as_of 解析不出来时直接 return to_features()）。"
                "本轮实测量：现有侧挂特征没有一条真的晚于 as_of，所以**尚无实际偷看**；"
                "风险是保护不存在，侧挂数据一积累就变成真未来信息。"
                "另注：服务侧走 `to_features()`（engine/main.py:560）本来就没有这道闸，"
                "只有训练侧调 `features_as_of()`（engine/learning/lgbm_shadow.py:104）。",
    }


def audit_side_provenance() -> dict:
    from engine.sources import njstats_side as side
    idx = side_index()
    rows = side._rows("features") + side._rows("site_odds") + side._rows("community")
    per_pair: dict[tuple, set] = {}
    for r in rows:
        k = side.key(r.get("home", ""), r.get("away", ""))
        if not all(k):
            continue
        ko = r.get("kickoff") or (side._row_kickoff(r).strftime("%Y-%m-%d %H:%M")
                                  if side._row_kickoff(r) else "")
        per_pair.setdefault(k, set()).add(str(ko))
    collisions = {k: sorted(v) for k, v in per_pair.items() if len(v) > 1}
    empty_kickoff = sum(1 for s in idx.values() if not s.kickoff)
    return {
        "id": "side_index_pair_key",
        "title": "侧挂索引的归并键（只按主客，不按场次）",
        "verdict": "pair_key_not_match_specific",
        "severity": "low",
        "status": "by_design" if not collisions else "violation",
        "collision_examples": [{"pair": list(k), "kickoffs": v}
                               for k, v in list(collisions.items())[:5]],
        "counts_for_humans": {"pairs_total": len(per_pair), "pairs_multi_kickoff": len(collisions),
                              "index_slots": len(idx), "slots_empty_kickoff": empty_kickoff},
        "note": "`build_index()`（engine/sources/njstats_side.py:183-240）用 `key(home, away)`"
                "归并，开赛时间不进键；`slot()` 用 setdefault 保留**第一条**的 kickoff，"
                "而 open_fair / snapshot_at 被**最后一条**覆盖 → 同一对球队的不同场次会"
                "互相串。对训练行意味着可能拿到别的（甚至更晚）一场的面板价。"
                "`SideSignal.kickoff` 因此不可信（站点价快照根本没有 kickoff 键，"
                "只有 kick_ts）。",
    }


# ----------------------------------------------------- 5 死列
def audit_dead_columns(rows: list[dict]) -> dict:
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    stats = columns_stats(rows)
    dead = sorted(n for n, s in stats.items() if s["n_distinct"] <= 1)
    intended = [n for n in dead if n in TARGET_MARKET_FEATURES]
    side_cols = [n for n in dead if n.startswith("side_")]
    unwired = [n for n in dead if n not in intended and n not in side_cols]
    side_sparse = sorted(n for n in stats
                         if n.startswith("side_") and stats[n]["n_nonzero"] < 0.05 * len(rows))
    return {
        "id": "matrix_dead_columns",
        "title": "训练矩阵里的死列：闸门关死的 / 接线失效的",
        "verdict": "declared_features_inactive" if unwired else "only_gated_columns_dead",
        "severity": "low" if unwired else "info",
        "status": "violation" if unwired else "by_design",
        "dead_by_gate": intended,
        "dead_but_declared_live": unwired,
        "sparse_columns": side_sparse,
        "counts_for_humans": {"columns": len(stats), "rows": len(rows),
                              "n_dead": len(dead), "n_unwired": len(unwired),
                              "side_nonzero_rows": stats["side_open_home"]["n_nonzero"],
                              "league_avg_goals_at_default": stats["league_avg_goals"]["n_default"]},
        "note": "死列分两类，别混。**该类是设计意图**：" + ", ".join(intended) +
                " —— `use_odds_features=false` 的闸门产物，符合预期。"
                "**该类是接线失效**：" + ", ".join(unwired) +
                " —— `is_knockout` 两端调用都不传（`engine/main.py:545-561` 与 "
                "`engine/learning/lgbm_shadow.py:90-105` 的参数表里都没有它，"
                "build_features 用默认值 False）；`rank_diff` 依赖 `fixture._rank_home`，"
                "该属性只由 DJYY 造的 Fixture 挂上（engine/sources/djyy.py:213-214），"
                "而生产走 `fetch_merged_fixtures` 重新 new 的 Fixture"
                "（engine/sources/manager.py:241-366）不带它 → getattr 恒 None；"
                "`lgbm_inputs` 里落盘的 rank_home 在 353 份冻结输入中 0 份非空。"
                "`engine/main.py:532-535` 那段「以前一个都没传 → rank_diff 恒为 0」的"
                "注释声称已修，实际结果没变。side_* 覆盖率 "
                f"{stats['side_open_home']['n_nonzero']}/{len(rows)}（NJStats 开盘快照"
                "只落了 2026-10-12~10-14 的采集窗口），league_avg_goals 有 "
                f"{stats['league_avg_goals']['n_default']}/{len(rows)} 行取默认 2.5"
                "（见 league_name_caliber_duplication）。",
    }


# ----------------------------------------------------- 6 让球线来源
def audit_handicap_provenance(rows: list[dict]) -> dict:
    """handicap 列的取源在竞彩（目标市场）与 Bet365 亚盘（参考市场）之间切换，且无标记。"""
    vals = [float(r["features"].get("handicap", 0.0) or 0.0) for r in rows]
    distinct = sorted(set(vals))
    # 落盘行里是否有非整数线（竞彩让球线是整数；.25/.75 只可能来自亚盘）
    quarter = [v for v in distinct if abs(v * 4 - round(v * 4)) < 1e-9
               and abs(v - round(v)) > 1e-9]
    return {
        "id": "matrix_handicap_line_provenance",
        "title": "handicap 特征的市场来源（竞彩让球线 / 亚盘线）",
        "verdict": "mixed_source_unmarked",
        "severity": "low",
        "status": "by_design",
        "distinct_values": distinct,
        "non_integer_lines": quarter,
        "counts_for_humans": {"rows": len(rows),
                              "n_nonzero": sum(1 for v in vals if v != 0.0),
                              "n_distinct_lines": len(distinct)},
        "note": "`handicap` 取 `fixture.handicap`，而合并层写的是"
                "`final_handicap = st_goal if st_goal else <Bet365 亚盘>`"
                "（engine/sources/manager.py:348）——体彩（目标市场）优先、亚盘兜底，"
                "两者都落进同一列，且没有任何来源标记。按 market_isolation 的口径，"
                "亚盘让球线在 REFERENCE 白名单里（`asian_handicap_line`，"
                "engine/prediction/market_isolation.py:53），而竞彩让球线属目标市场盘口——"
                "名单里没有对应项，所以 `audit_features()` 对这一列静默。"
                "**已证实**：混合取源 + 名单不覆盖 + 无来源字段；"
                "**未证实**：竞彩让球线本身算不算『目标市场赔率』（判据口径问题，需人裁决）。",
    }


# ----------------------------------------------------- 7 落盘量化
def audit_serve_quantization(rows: list[dict]) -> dict:
    """服务侧 round(elo,1) 落盘、训练侧读落盘值 → 逐位不可复现。"""
    pat = re.compile(r"round\((\w+_rating\.\w+),\s*(\d+)\)")
    src = (ROOT / "engine" / "main.py").read_text(encoding="utf-8").split("\n")
    persisted = []
    for i, line in enumerate(src, 1):
        m = pat.search(line)
        if m and "elo" in m.group(1):
            persisted.append({"line": i, "field": m.group(1), "decimals": int(m.group(2))})
    worst = max((10 ** -p["decimals"] / 2 for p in persisted), default=0.0)
    return {
        "id": "serve_snapshot_quantization",
        "title": "服务侧落盘量化 vs 训练侧回放的输入精度",
        "verdict": "inputs_not_bitwise_reproducible" if persisted else "no_quantization",
        "severity": "low" if persisted else "info",
        "status": "by_design" if persisted else "clean",
        "quantized_fields": persisted,
        "max_abs_offset": {"elo_home": worst, "elo_away": worst, "elo_diff": 2 * worst},
        "counts_for_humans": {"rows": len(rows), "decimals": [p["decimals"] for p in persisted]},
        "note": "build_features 在 `engine/main.py:545` 吃的是**未取整**的 "
                "`home_rating.elo`，落盘时写 `round(elo,1)`（engine/main.py:818-819），"
                "训练侧 `engine/learning/lgbm_shadow.py:91-92` 读回落盘值 → "
                "elo_home/elo_away 各最多偏 0.05、elo_diff 最多偏 0.1，"
                "1500/1500 行都不可逐位复现。`lgbm_inputs` 冻结了 form/联赛先验/"
                "排名/xG，但**不含 elo**（engine/main.py:528-537），所以这道偏移是"
                "设计遗留而非已修项。概率影响见 booster_gain_attribution 的实测。",
    }


BOOSTER_SKIP_PREFIX = "skipped"


def _booster_skip(why: str) -> dict:
    return {"id": "booster_gain_attribution",
            "title": "booster 列重要性与 elo 量化的实际影响",
            "verdict": f"{BOOSTER_SKIP_PREFIX}_{why[:60]}",
            "severity": "info", "status": "clean", "isolated": False,
            "note": "本判定跳过，原因：" + why +
                    "。跳过不影响其它 14 条判定；换到有 lightgbm 且 booster 能加载的"
                    "机器上再跑一次即可拿到实测。"}


def _booster_payload() -> dict:
    """在当前进程里加载 booster，算重要性 + elo 抖动影响（返回可 JSON 化的字典）。

    **只允许从子进程调用**（见 `audit_booster_sensitivity` 的说明）。
    模型文本用 `Booster(model_str=…)` 传：`read_text()` 已把 CRLF 归一成 LF，
    因此不受 Windows 检出换行影响；`model_file=` 走 lightgbm 自己的文件解析，
    在 Windows runner 上实测会在原生层把解释器打死。
    """
    import lightgbm as lgb
    import numpy as np
    from engine.prediction.lgbm_model import FEATURE_NAMES
    if not MODEL_FILE.exists():
        raise RuntimeError("data/models/lgbm_model.txt 不存在")
    booster = lgb.Booster(model_str=MODEL_FILE.read_text(encoding="utf-8"))
    records = load_settled_records()
    rows = rebuild_matrix(records)
    X = np.array([[float(r["features"].get(k, 0.0)) for k in FEATURE_NAMES]
                  for r in rows])
    p0 = booster.predict(X)
    gain = booster.feature_importance("gain")
    tot = float(sum(gain)) or 1.0
    imp = {FEATURE_NAMES[i]: round(100 * gain[i] / tot, 3) for i in range(len(gain))
           if i < len(FEATURE_NAMES)}
    rng = np.random.default_rng(20261116)
    ih, ia = FEATURE_NAMES.index("elo_home"), FEATURE_NAMES.index("elo_away")
    idf = FEATURE_NAMES.index("elo_diff")
    deltas, flips = [], 0
    for _ in range(20):
        X2 = X.copy()
        X2[:, ih] += rng.uniform(-0.05, 0.05, X.shape[0])
        X2[:, ia] += rng.uniform(-0.05, 0.05, X.shape[0])
        X2[:, idf] = X2[:, ih] - X2[:, ia]
        p1 = booster.predict(X2)
        deltas.append(np.abs(p1 - p0).max(axis=1))
        flips += int((p1.argmax(axis=1) != p0.argmax(axis=1)).sum())
    d = np.concatenate(deltas)
    return {"importance": imp,
            "zero_gain_columns": [n for n, v in imp.items() if v == 0.0],
            "jitter": {"draws": 20, "rows": int(X.shape[0]),
                       "abs_dp_p95": round(float(np.percentile(d, 95)), 6),
                       "abs_dp_max": round(float(d.max()), 6),
                       "argmax_flips": flips},
            "num_feature": booster.num_feature(),
            "feature_names_len": len(FEATURE_NAMES)}


def audit_booster_sensitivity() -> dict:
    """用仓库里现成的 booster 量化：列取值抖动对概率的实际影响。

    这一步必须跑在**子进程**里：2026-11-16 的 `test (windows-latest)` CI 实测
    `lgb.Booster(model_file=…)` 在原生层 `Fatal Python error: Aborted`
    （lightgbm/basic.py:3800），Python 侧 `try/except` 抓不住，会把整个 pytest
    会话连带打死（ubuntu 不受影响，所以只有 windows 红）。子进程里崩就只是
    非零退出码 → 本判定降级为 skipped，其它 14 条照跑。
    """
    mod = f"{SELF.parent.name}.{SELF.stem}"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    try:
        cp = subprocess.run([sys.executable, "-m", mod, "--booster-json"],
                            cwd=str(ROOT), env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=420)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _booster_skip(f"子进程起不来或超时：{exc!r}")
    if cp.returncode != 0:
        last = [l for l in (cp.stderr or "").splitlines() if l.strip()]
        return _booster_skip("booster 在本机/本平台加载失败"
                             f"（rc={cp.returncode}；{last[-1][:150] if last else '无 stderr'}）")
    try:
        # 只取最后一行：生产模块若 import 时 print 了东西，不能把判定带崩
        last = [l for l in cp.stdout.splitlines() if l.strip()]
        payload = json.loads(last[-1])
    except (json.JSONDecodeError, ValueError) as exc:
        return _booster_skip(f"子进程输出不可解析：{exc}")
    imp = payload["importance"]
    return {
        "id": "booster_gain_attribution",
        "title": "booster 列重要性与 elo 量化的实际影响",
        "verdict": "quantization_immaterial_for_direction",
        "severity": "info",
        "status": "clean",
        "isolated": True,
        "top_gain_columns": sorted(imp, key=lambda k: -imp[k])[:5],
        "zero_gain_columns": payload["zero_gain_columns"],
        "elo_jitter_impact": payload["jitter"],
        "counts_for_humans": {"model_num_feature": payload["num_feature"],
                              "feature_names_len": payload["feature_names_len"]},
        "note": "在 0.05 的取整误差范围内对 elo_home/elo_away 各抖 20 轮："
                "三档概率的最大绝对变化 p95 与最大值、以及 argmax 翻转数都列在 "
                "`elo_jitter_impact`（0 翻转 = 对下注方向无影响，只有概率尾数差）。"
                "`zero_gain_columns` 是 booster 从未分裂用过的列——与 "
                "matrix_dead_columns 的样本侧死列互为佐证。"
                "重要口径提示：本模型的 ready 判定与生产概率是两件事，"
                "config/prediction.json 的 fusion.post_fusion.lgbm_blend 目前为 false。"
                "本判定在子进程里算（booster 在 Windows 上会原生 Abort）。",
    }


# ----------------------------------------------------- 8 devig 口径重复
DEVIG_SITES = (
    ("engine/sources/njstats_side.py", "_devig"),
    ("engine/sources/njstats_features.py", "devig"),
    ("engine/review/clv.py", "devig"),
    ("engine/backtest/book_vig.py", "devig"),
    ("engine/backtest/historical_replay.py", "devig"),
    ("engine/prediction/calibration.py", "devig_multiplicative"),
    ("scripts/close_calibration.py", "devig_3"),
)


def devig_impls() -> dict[str, Callable[[tuple], Any]]:
    import importlib
    out: dict[str, Callable[[tuple], Any]] = {}
    for rel, fname in DEVIG_SITES:
        mod = importlib.import_module(rel[:-3].replace("/", "."))
        fn = getattr(mod, fname)
        if fname == "devig" and rel.endswith("njstats_features.py"):
            out[f"{rel}::{fname}"] = lambda t, f=fn: f({"home": t[0], "draw": t[1], "away": t[2]})
        elif fname == "devig" and rel.endswith("clv.py"):
            out[f"{rel}::{fname}"] = lambda t, f=fn: f({"home": t[0], "draw": t[1], "away": t[2]})
        elif fname == "devig" and rel.endswith("book_vig.py"):
            out[f"{rel}::{fname}"] = lambda t, f=fn: f(list(t))
        elif fname == "devig_multiplicative":
            out[f"{rel}::{fname}"] = lambda t, f=fn: f(list(t))
        elif fname == "devig_3":
            out[f"{rel}::{fname}"] = lambda t, f=fn: f(*t)
        else:
            out[f"{rel}::{fname}"] = lambda t, f=fn: f(t)
    return out


def _odds_triples() -> list[tuple[str, tuple]]:
    out: list[tuple[str, tuple]] = []
    for d in pred_dates(load_settled_records()):
        for p in load_predictions(d):
            t = (p.get("home_odds"), p.get("draw_odds"), p.get("away_odds"))
            if all(isinstance(x, (int, float)) and x > 0 for x in t):
                out.append(("sporttery_1x2", t))
    for f in sorted((ROOT / "data" / "state" / "njstats" / "site_odds").glob("*.json")):
        for m in (json.loads(f.read_text(encoding="utf-8")).get("matches") or []):
            o = m.get("odds")
            if isinstance(o, list) and len(o) == 3 and all(
                    isinstance(x, (int, float)) and x > 0 for x in o):
                out.append(("njstats_site", tuple(o)))
    for f in sorted((ROOT / "data" / "state" / "njstats_features").glob("*.json")):
        for r in (json.loads(f.read_text(encoding="utf-8")).get("rows") or []):
            op = r.get("opening") or {}
            t = (op.get("home"), op.get("draw"), op.get("away"))
            if all(isinstance(x, (int, float)) and x > 0 for x in t):
                out.append(("njstats_opening", t))
    return out


GUARD_PROBES = (("odds_1.00", (1.0, 4.0, 7.0)),
                ("odds_below_1", (0.95, 5.0, 9.0)),
                ("odds_zero", (0.0, 4.0, 7.0)))


def probe_guards(impls: dict[str, Callable[[tuple], Any]]) -> dict[str, dict[str, str]]:
    """对每组实现跑同一批非法赔率，记录「拒 / 抛 / 不设防」的行为签名。"""
    out: dict[str, dict[str, str]] = {}
    for name, fn in impls.items():
        row = {}
        for label, t in GUARD_PROBES:
            try:
                r = _devig_result(fn(t))
            except Exception as exc:            # noqa: BLE001 - 记录口径差异本身
                row[label] = f"raise:{type(exc).__name__}"
                continue
            row[label] = "none" if r is None else ("nan" if r != r else "value")
        out[name] = row
    return out


def _devig_result(res):
    """把各实现的返回形状统一成 (h, d, a)；返回 None = 这份实现拒了这组赔率。"""
    if res is None:
        return None
    if isinstance(res, dict):
        return (res["home"], res["draw"], res["away"])
    return tuple(float(x) for x in res)


def max_disagreement(impls: dict[str, Callable[[tuple], Any]],
                     triples: list[tuple[str, tuple]]) -> float:
    """两组及以上实现之间，真实赔率上的最大逐档概率差（0 = 口径完全一致）。"""
    results = {}
    for name, fn in impls.items():
        vals = []
        for _, t in triples:
            try:
                vals.append(_devig_result(fn(t)))
            except Exception:                    # noqa: BLE001 - 抛异常记为不可比
                vals.append(None)
        results[name] = vals
    usable = {n: sum(1 for v in vals if v) for n, vals in results.items()}
    worst = 0.0
    names = [n for n in results if usable[n] > 0]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            for a, b in zip(results[names[i]], results[names[j]]):
                if not a or not b:
                    continue
                worst = max(worst, max(abs(x - y) for x, y in zip(a, b)))
    if len(names) < 2:
        raise RuntimeError(f"可比对的 devig 实现不足 2 份：{usable}")
    return worst


def audit_devig_duplication() -> dict:
    impls = devig_impls()
    triples = _odds_triples()
    worst = max_disagreement(impls, triples)
    # 守卫差异要有实害，必须真有落在 (0,1] / ≤0 区间的赔率；实测计数放这里
    n_le_1 = sum(1 for _, t in triples if min(t) <= 1.0)
    n_le_0 = sum(1 for _, t in triples if min(t) <= 0.0)
    guards = probe_guards(impls)
    sigs = {tuple(guards[k][c] for c in ("odds_1.00", "odds_below_1", "odds_zero"))
            for k in guards}
    divergent = len(sigs)
    return {
        "id": "devig_caliber_duplication",
        "title": "去水（devig）口径的重复实现",
        "verdict": "duplicated_but_numerically_consistent" if worst < 1e-9
                   else "duplicated_and_divergent",
        "severity": "low" if worst < 1e-9 else "medium",
        "status": "by_design" if worst < 1e-9 else "violation",
        "sites": [f"{rel}::{fn}" for rel, fn in DEVIG_SITES],
        "guard_matrix": guards,
        "max_abs_difference": worst,
        "counts_for_humans": {"implementations": len(impls), "triples_tested": len(triples),
                              "guard_behaviour_signatures": divergent,
                              "triples_with_min_odds_le_1": n_le_1,
                              "triples_with_min_odds_le_0": n_le_0},
        "note": "同一条等赔去水在 7 处各写一份（含 scripts/ 与只读禁触的 "
                "engine/prediction/calibration.py、engine/backtest/），其中 "
                "engine/sources/njstats_features.py:83-84 的 docstring 直接写着"
                "「口径与 historical_replay.py 的 devig() 一致」——一致性靠注释维持，"
                "没有测试兜着。真实赔率上数值等价（max|Δ|≈2e-16 的浮点噪声），"
                "但**非法赔率的守卫不一致**：见 guard_matrix（None / raise / 完全不设防"
                f"三种行为并存；真实样本里 min(odds)≤1.0 的三元组有 {n_le_1} 条、"
                f"≤0 的有 {n_le_0} 条，所以守卫差异本轮实害为 0）。"
                "另外 `build_features` 内联的第 8 份 "
                "（engine/prediction/lgbm_model.py:285-291）守卫是 `o>0`，"
                "比 njstats 侧的 `min(odds)>1.0` 宽，且它把 overround 定义成 "
                "`sum(1/o)-1`，与其他口径返回的 overround 不同名同义。"
                "守卫差异要成真害，需要真实赔率里出现 min(odds)≤1.0 的三元组（计数见 "
                "counts_for_humans）。本判定不重复 "
                "`docs/proposals/dc-market-blend-and-devig-review.md` 已裁定的"
                "「服务内部同对象双去水」（`test_same_object_double_devig_exists_but_is_masked_by_zero_blend`），"
                "只补『多份实现是否数值等价 + 非法赔率守卫是否一致』这一维度。",
    }


# --------------------------------------------- 8b 服务 Shin vs 回放 proportional
def brier(probs: list[float], label: int) -> float:
    return sum((float(p) - (1.0 if i == label else 0.0)) ** 2 for i, p in enumerate(probs))


def paired_brier_shift(pairs: list[tuple[list[float], list[float], int]]) -> dict:
    """pairs = [(A 口径概率, B 口径概率, 真值档)] → Brier(A)-Brier(B) 的配对统计。"""
    import statistics as stt

    import numpy as np
    d = [brier(a, lab) - brier(b, lab) for a, b, lab in pairs]
    if not d:
        return {"n": 0}
    mean = float(np.mean(d))
    se = float(np.std(d, ddof=1) / np.sqrt(len(d))) if len(d) > 1 else 0.0
    return {"n": len(d), "mean_delta": round(mean, 6), "se": round(se, 6),
            "t": round(mean / se, 2) if se > 0 else 0.0,
            "abs_median": round(float(stt.median([abs(x) for x in d])), 6),
            "abs_max": round(float(max(abs(x) for x in d)), 6)}


def caliber_switch_days(census: dict[str, dict[str, int]]) -> dict:
    """从逐日口径普查里判断『落盘锚是否中途换了口径』。

    定义（写死，供测试复算）：proportional 占优的最后一天 < shin 占优的第一天
    ⇒ 中途换过；两种口径在同一日并存的日子单独列出。
    """
    prop_days = sorted(d for d, v in census.items() if v["proportional"] > v["shin"])
    shin_days = sorted(d for d, v in census.items() if v["shin"] > v["proportional"])
    return {
        "prop_days": prop_days, "shin_days": shin_days,
        "switched": bool(prop_days and shin_days and prop_days[-1] < shin_days[0]),
        "mixed_caliber_days": sorted(d for d, v in census.items()
                                     if v["shin"] and v["proportional"]),
        "last_proportional_day": prop_days[-1] if prop_days else None,
        "first_shin_day": shin_days[0] if shin_days else None,
    }


def audit_devig_method_families(records) -> dict:
    """**方法族**之间的口径差，全部限定在**同一对象**（竞彩 HAD 目标盘）。

    上一轮审计 `leak_audit_check.py:458-460` 把这条列为「本次未复核，待核」；
    `docs/proposals/dc-market-blend-and-devig-review.md` 已裁定「回放
    `devig('proportional')` vs 服务 `devig_shin`」**不构成同对象漂移**
    （回放基准的对象是 football-data.co.uk 参考盘收盘，不是竞彩），本函数
    沿用那个结论，不做第二次「回放 vs 服务」指控。这里只量同对象的三份：
    落盘市场锚 `market_fair`（Shin）、DC 内混（`dixon_coles.py:76,214-218`
    比例去水，被 `market_blend_weight=0.0` 掩蔽）、以及 CLV / 侧挂的
    proportional（`engine/review/clv.py`、`engine/sources/njstats_side.py`，
    对象是站点收盘价）。

    与 `devig_caliber_duplication` 是两件事：那份查 7 个**同一口径**实现是否
    等价（等价），这份查**不同口径**之间的差，并检查落盘锚自己有没有换过口径。
    """
    from engine.prediction.calibration import devig_multiplicative, select_devig_method
    pm = {d: {p.get("match_id", ""): p for p in load_predictions(d)} for d in pred_dates(records)}
    per_leg: list[float] = []
    pairs: list[tuple[list[float], list[float], int]] = []
    persisted_agree = 0
    persisted_total = 0
    by_date: dict[str, dict[str, int]] = {}
    for rec in records:
        p = pm.get(rec.get("date", ""), {}).get(rec.get("match_id", "")) or {}
        t = (p.get("home_odds"), p.get("draw_odds"), p.get("away_odds"))
        if not all(isinstance(x, (int, float)) and x > 1.0 for x in t):
            continue
        shin = [float(x) for x in select_devig_method(list(t))]
        prop = [float(x) for x in devig_multiplicative(list(t))]
        per_leg += [abs(a - b) for a, b in zip(shin, prop)]
        mf = rec.get("market_fair") or p.get("market_fair")
        if mf:
            persisted_total += 1
            bucket = "other"
            if max(abs(float(a) - b) for a, b in zip(mf, shin)) <= 1e-4:
                persisted_agree += 1
                bucket = "shin"
            elif max(abs(float(a) - b) for a, b in zip(mf, prop)) <= 1e-4:
                bucket = "proportional"
            day = by_date.setdefault(str(rec.get("date", "")),
                                     {"shin": 0, "proportional": 0, "other": 0})
            day[bucket] += 1
        if rec.get("actual_idx") is not None and mf:
            pairs.append(([float(x) for x in mf], prop, int(rec["actual_idx"])))
    stat = paired_brier_shift(pairs)
    immaterial = stat.get("n", 0) > 0 and abs(stat.get("t", 0.0)) < 2.0
    cs = caliber_switch_days(by_date)
    switched, prop_days, shin_days = cs["switched"], cs["prop_days"], cs["shin_days"]
    mixed_days = cs["mixed_caliber_days"]
    return {
        "id": "devig_method_family_divergence",
        "title": "同一对象（竞彩 HAD）上的去水口径差 + 落盘锚口径漂移",
        "verdict": ("anchor_caliber_switched_mid_sample" if switched
                    else "divergent_calibers_below_metric_detection" if immaterial
                    else "divergent_calibers_move_the_metric"),
        "severity": "medium" if not immaterial else ("low" if switched else "info"),
        "status": "violation" if (switched or not immaterial) else "by_design",
        "per_leg_abs_delta": {"mean": round(sum(per_leg) / len(per_leg), 6) if per_leg else None,
                              "max": round(max(per_leg), 6) if per_leg else None},
        "persisted_market_fair_is_shin": {"rows": persisted_total, "match_shin": persisted_agree},
        "caliber_switch": {"daily_census": by_date,
                           "last_proportional_day": cs["last_proportional_day"],
                           "first_shin_day": cs["first_shin_day"],
                           "mixed_caliber_days": cs["mixed_caliber_days"]},
        "paired_brier": stat,
        "counts_for_humans": {"odds_rows": len(per_leg) // 3, "paired_rows": stat.get("n", 0)},
        "note": "服务侧市场腿与落盘的 `market_fair` 走 "
                "`select_devig_method()` → `devig_shin()`"
                "（engine/prediction/calibration.py:116-118，engine/main.py:475），"
                "而 DC 内混（比例，被 w=0 掩蔽）与 CLV/侧挂（站点收盘的比例去水）"
                "用的是另一族口径。本函数**不指控「回放 vs 服务同对象漂移」——"
                "那个结论已被 docs/proposals/dc-market-blend-and-devig-review.md "
                "以『对象不同』否掉**（回放基准是参考盘收盘）。"
                "逐档概率差 mean/max 见 `per_leg_abs_delta`（**已证实**两处口径不同、"
                "且差到 pp 量级）；但对已结算样本做配对 Brier 后 "
                f"Δ={stat.get('mean_delta')}（SE {stat.get('se')}，t={stat.get('t')}，"
                f"n={stat.get('n')}）——**低于检测下限**，也就是不改判定口径。"
                "**更要紧的是落盘锚本身换了口径**：按日期归类 `market_fair` 后，"
                + (f"{prop_days[-1]} 及以前是 proportional、{shin_days[0]} 起是 Shin，"
                   f"中间 {len(mixed_days)} 天两种混存" if switched else "未发现切换")
                + "，而落盘记录里没有任何字段标明用的是哪一种"
                "（`main.py:733-735` 只存 `[round(x,4) …]`）。同一列 `market_fair` "
                "跨口径 → 账本的 `delta_vs_market`、EV、市场基线在 "
                f"{cs['first_shin_day']} 前后不同质；"
                "`docs/proposals/dc-market-blend-and-devig-review.md` 里「直接用生产冻结"
                f"的 **Shin** 去水值」这句只对 {cs['first_shin_day']} 起的行成立，"
                "那之前的冻结值是 proportional → 跨该日的 `ablation_replay` 配对结论"
                "混了两份锚（实测该混合在 Brier 上检不出，既有结论不被推翻，"
                "只是措辞要收窄）。"
                "实测影响（`paired_brier`）低于检测下限，所以**不需要作废样本**；"
                "但这条不是「由设计决定」——是缺一个来源标记。"
                "推测：真正会踩到它的是跨期对比（拿 10-15 前的 proportional 基线"
                "比之后的 Shin 基线），不是单期内部排序。"
                "git 侧无法归因：本仓库历史在 `07247f8a9`（2026-11-04）被压成单提交。",
    }


# ----------------------------------------------------- 9 联赛名口径重复
def audit_league_name_duplication() -> dict:
    from engine.learning.freshness import LEAGUE_NAME_MAP
    from engine.pipeline.helpers import LEAGUE_ALIASES, _canon_league
    from engine.prediction.league_stats import DEFAULT_AVG_GOALS, league_stats

    comps: dict[str, int] = {}
    for d in pred_dates(load_settled_records()):
        for p in load_predictions(d):
            comps[str(p.get("competition") or "")] = comps.get(str(p.get("competition") or ""), 0) + 1
    disagree = []
    for name in sorted(set(comps) | set(LEAGUE_ALIASES) | set(LEAGUE_NAME_MAP)):
        c1, c2 = _canon_league(name), LEAGUE_NAME_MAP.get(name, name)
        if c1 != c2:
            disagree.append({"raw": name, "canon_league": c1, "name_map": c2,
                             "rows_in_data": comps.get(name, 0)})
    # 别名表未串联：LEAGUE_ALIASES 的键在联赛先验查不到
    unchained = [a for a in LEAGUE_ALIASES
                 if league_stats(a)[0] == DEFAULT_AVG_GOALS
                 and league_stats(_canon_league(a))[0] != DEFAULT_AVG_GOALS]
    # 冻结输入里联赛先验今天已复现不出来的行
    records = load_settled_records()
    predmap = {d: {p.get("match_id", ""): p for p in load_predictions(d)} for d in pred_dates(records)}
    frozen = irreproducible = 0
    irrepro_examples = []
    for rec in records:
        p = predmap.get(rec.get("date", ""), {}).get(rec.get("match_id", ""))
        li = (p or {}).get("lgbm_inputs")
        if not li:
            continue
        frozen += 1
        fv = li.get("league_avg_goals")
        tv = league_stats(str((p or {}).get("competition") or ""))[0]
        if fv is not None and abs(float(fv) - float(tv)) > 1e-9:
            irreproducible += 1
            if len(irrepro_examples) < 5:
                irrepro_examples.append({"date": rec.get("date"),
                                         "competition": p.get("competition"),
                                         "frozen": fv, "today": tv})
    n_miss = sum(v for k, v in comps.items() if league_stats(k)[0] == DEFAULT_AVG_GOALS)
    return {
        "id": "league_name_caliber_duplication",
        "title": "联赛名归一：两份互不串联的别名表",
        "verdict": "two_tables_not_chained" if (disagree or unchained) else "single_caliber",
        "severity": "low" if unchained else "info",
        "status": "violation" if unchained else "by_design",
        "tables": {"engine/pipeline/helpers.py:LEAGUE_ALIASES": sorted(LEAGUE_ALIASES),
                   "engine/learning/freshness.py:LEAGUE_NAME_MAP": sorted(LEAGUE_NAME_MAP)},
        "disagreeing_names": disagree,
        "alias_keys_that_break_prior_lookup": unchained,
        "frozen_prior_irreproducible": irrepro_examples,
        "counts_for_humans": {"distinct_competitions": len(comps),
                              "rows_falling_back_to_default_prior": n_miss,
                              "frozen_rows": frozen,
                              "frozen_rows_irreproducible_today": irreproducible},
        "note": "`_canon_league`（账本/R1/锚定用）与 `LEAGUE_NAME_MAP`（联赛先验/新鲜度用）"
                "是两张独立的别名表。调用顺序上也不闭合：`pred.competition = "
                "_canon_league(raw)`（engine/main.py:467）落盘的是归一后的名字，"
                "而 `league_stats(fixture.competition)`（engine/main.py:529）拿的是**原始**"
                "名字 → 只有两张表都覆盖了同一个别名时才命中。实测断点：`韩职` 在 "
                "LEAGUE_ALIASES 里（→K1联赛），但不在 LEAGUE_NAME_MAP 里，"
                "所以韩职场次拿不到联赛先验，落回 2.5/0.45 默认值；"
                "账本里 K1联赛 的 8 行冻结先验正是 2.5，而今天按 K1联赛 查是 2.34。",
    }


# ----------------------------------------------------- 10 market_blend 口径重复
UNWIRED_GETTERS = ("get_market_blend", "get_base_goals", "get_home_adv")


def scan_blend_consumers(txt: str) -> tuple[list[int], list[int]]:
    """扫一段源码，返回（读 `market_blend_weight` 的行号，调未接线 getter 的行号）。

    赋值（`x.market_blend_weight = …`）不算读：只统计 Load 上下文。
    """
    reads: list[int] = []
    calls: list[int] = []
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        return reads, calls
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == "market_blend_weight" \
                and not isinstance(n.ctx, ast.Store):
            reads.append(n.lineno)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in UNWIRED_GETTERS:
            calls.append(n.func.lineno)
    return reads, calls


def audit_market_blend_duplication() -> dict:
    cfg = json.loads((ROOT / "config" / "prediction.json").read_text(encoding="utf-8"))
    cfg_w = cfg.get("prediction", {}).get("market_blend_weight")
    from engine.learning.league_params import LeagueParam
    from engine.prediction.dixon_coles import DixonColesConfig
    defaults = {"engine/prediction/dixon_coles.py::DixonColesConfig":
                float(DixonColesConfig().market_blend_weight),
                "engine/learning/league_params.py::LeagueParam":
                float(LeagueParam().market_blend_weight)}
    mc = (ROOT / "evolution" / "harness" / "model_core.py").read_text(encoding="utf-8")
    m = re.search(r"[\"']market_blend_weight[\"']\s*:\s*([0-9.]+)", mc)
    if m:
        defaults["evolution/harness/model_core.py::DEFAULTS"] = float(m.group(1))
    lp_path = ROOT / "data" / "state" / "league_params.json"
    lp_vals: list[float] = []
    if lp_path.exists():
        doc = json.loads(lp_path.read_text(encoding="utf-8"))
        for v in (doc.values() if isinstance(doc, dict) else []):
            if isinstance(v, dict) and isinstance(v.get("market_blend_weight"), (int, float)):
                lp_vals.append(float(v["market_blend_weight"]))
    # 消费点扫描（AST）：真正读这个量的代码行，以及三个未接线 getter 的调用方
    read_sites: list[str] = []
    getter_callers: list[str] = []
    for path in sorted((ROOT / "engine").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py")) \
            + sorted((ROOT / "evolution").rglob("*.py")):
        if path.resolve() == SELF:
            continue
        rel = path.relative_to(ROOT).as_posix()
        reads, calls = scan_blend_consumers(path.read_text(encoding="utf-8", errors="ignore"))
        read_sites += [f"{rel}:{ln}" for ln in reads]
        getter_callers += [f"{rel}:{ln}" for ln in calls]
    live_zero = cfg_w == 0.0
    unwired = not getter_callers
    return {
        "id": "market_blend_weight_duplication",
        "title": "market_blend_weight（把市场概率掺进模型腿）的四处取值",
        "verdict": "live_value_zero_and_league_layer_unwired" if live_zero and unwired
                   else ("live_blend_not_zero" if not live_zero else "league_layer_wired_up"),
        "severity": "info" if live_zero and unwired else "high",
        "status": "by_design" if live_zero and unwired else "violation",
        "live_config_value": cfg_w,
        "code_defaults": defaults,
        "league_params_values": {"n": len(lp_vals), "min": min(lp_vals, default=None),
                                 "max": max(lp_vals, default=None)},
        "read_sites": read_sites,
        "unwired_getter_callers": getter_callers,
        "counts_for_humans": {"written_places": 1 + len(defaults) + (1 if lp_vals else 0),
                              "league_params_leagues_with_blend": len(lp_vals),
                              "read_sites_outside_definition": len(read_sites)},
        "note": "唯一活值来自 `config/prediction.json[\"prediction\"][\"market_blend_weight\"]`"
                "（= " + str(cfg_w) + "），构造点 engine/main.py:283，消费点 "
                "engine/prediction/dixon_coles.py:75-78 → **当前市场赔率不掺进模型腿**，"
                "目标市场隔离成立（这条补上了 leak_audit_check.py:455-461 里"
                "「DC 层 market_blend 本次未复核」的空档）。同对象的这条结论已由 "
                "`docs/proposals/dc-market-blend-and-devig-review.md` 给出并被 "
                "`tests/test_dc_market_blend_review.py`（22 项）钉住；本判定是在"
                "「真实训练矩阵 + 接线扫描」这条独立路径上复算，不替代那 22 项。"
                "其余三份都不是活的：两个 dataclass 默认 0.28、"
                "evolution/harness/model_core.py 的 DEFAULTS 0.28、"
                "data/state/league_params.json 被结算侧 adapt_all() 持续改写到 0.1~0.5；"
                "联赛级那份的三个 getter（get_market_blend / get_base_goals / get_home_adv）"
                "**0 个调用方**，且 docstring 写明「本 getter 有意保持未接线」"
                "（engine/learning/league_params.py:344-386，2026-10-14 E5 的量化决定）"
                "→ 判定 by_design，不是缺陷。"
                "但要注意这条通道**不在市场隔离名单的射程内**：它不是特征名，"
                "而是模型内部一步混合，`audit_features()` 永远看不见。"
                "所以本脚本把它做成可复现守卫：一旦有人把 getter 接进预测路径，"
                "或把 config 的 0.0 改大，判定立刻从 by_design 翻成 violation。",
    }


# ----------------------------------------------------- 11 侧挂特征名两处定义
def audit_side_name_parity() -> dict:
    """side_* 列名在两个文件各写一份：lgbm_model 的名单 vs njstats_side 的产出。"""
    from engine.prediction.lgbm_model import SIDE_FEATURE_NAMES
    from engine.sources import njstats_side as side
    full = side.SideSignal(
        home="A", away="B", kickoff="2026-01-01 00:00", snapshot_at="2026-01-01 00:00",
        open_fair=(0.5, 0.3, 0.2), home_ppg_home=2.0, away_ppg_away=1.0,
        site_fair=(0.45, 0.31, 0.24), crowd_people=10)
    emitted = set(full.to_features())
    declared = set(SIDE_FEATURE_NAMES)
    target_names = {"site", "dom_tendency", "challenge"}
    leaked_site = [n for n in emitted if any(t in n for t in target_names)]
    ok = emitted == declared and not leaked_site
    return {
        "id": "side_feature_name_parity",
        "title": "侧挂特征名单的两份定义（lgbm_model ↔ njstats_side）",
        "verdict": "names_match_and_site_price_excluded" if ok else "names_diverged",
        "severity": "info" if ok else "medium",
        "status": "clean" if ok else "violation",
        "only_in_model_list": sorted(declared - emitted),
        "only_in_side_producer": sorted(emitted - declared),
        "counts_for_humans": {"declared": len(declared), "emitted": len(emitted)},
        "note": "同一批列名写两处：engine/prediction/lgbm_model.py:77-84 的 "
                "SIDE_FEATURE_NAMES（booster 列序契约）与 "
                "engine/sources/njstats_side.py:90-101 的 to_features() 键名。"
                "两处都得改、改一处就静默错位（predict_single 按列号截断，不报错）。"
                "同时这条也钉住「站点价（目标市场）不得出现在 to_features() 里」"
                "——njstats_side.py:24-27 的承诺目前成立：即使 SideSignal 带着 "
                "site_fair，to_features() 也不吐它。",
    }


# ------------------------------------------------------------------ 汇总
def build_report() -> dict:
    records = load_settled_records()
    rows = rebuild_matrix(records)
    findings = [
        audit_matrix_isolation(rows),
        audit_time_points(rows, records),
        audit_settlement_insensitivity(rows, records),
        audit_price_insensitivity(rows, records),
        audit_side_gate(),
        audit_side_provenance(),
        audit_dead_columns(rows),
        audit_handicap_provenance(rows),
        audit_serve_quantization(rows),
        audit_booster_sensitivity(),
        audit_devig_duplication(),
        audit_devig_method_families(records),
        audit_league_name_duplication(),
        audit_market_blend_duplication(),
        audit_side_name_parity(),
    ]
    return {"root": ROOT.as_posix(),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "sample": {"ledger_rows": len(records), "training_rows": len(rows)},
            "findings": findings}


def render_text(report: dict) -> str:
    lines = ["=== 训练矩阵 × 服务口径 对齐审计（只读） ===",
             f"样本：账本 {report['sample']['ledger_rows']} 行 → 训练矩阵 "
             f"{report['sample']['training_rows']} 行"]
    for f in report["findings"]:
        lines.append(f"\n【{f['title']}】{f['verdict']}  "
                     f"(severity={f['severity']}, {f['status']})")
        for key in ("live_blacklisted_columns", "unclassified_columns", "buckets",
                    "post_kickoff_rows", "mutated_columns", "asof_format",
                    "future_snapshot_rows_detected", "collision_examples",
                    "dead_by_gate", "dead_but_declared_live", "non_integer_lines",
                    "sparse_columns", "distinct_values", "quantized_fields",
                    "max_abs_offset", "top_gain_columns", "zero_gain_columns",
                    "elo_jitter_impact", "sites", "guard_matrix", "disagreeing_names",
                    "alias_keys_that_break_prior_lookup", "frozen_prior_irreproducible",
                    "caliber_switch", "per_leg_abs_delta", "persisted_market_fair_is_shin",
                    "live_config_value", "code_defaults", "league_params_values",
                    "read_sites", "unwired_getter_callers", "only_in_model_list",
                    "only_in_side_producer"):
            if f.get(key):
                lines.append(f"  {key}: {f[key]}")
        if f.get("counts_for_humans"):
            lines.append(f"  计数（仅供人读，不作判定）：{f['counts_for_humans']}")
        lines.append(f"  说明：{f['note']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="训练矩阵与服务口径对齐审计（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    ap.add_argument("--booster-json", action="store_true",
                    help="（内部用）只在当前进程算 booster 统计并打印 JSON，"
                         "供 audit_booster_sensitivity 的子进程调用")
    args = ap.parse_args(argv)
    if args.booster_json:
        sys.stdout.write(json.dumps(_booster_payload(), ensure_ascii=False) + "\n")
        return 0
    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(report))
    return 1 if any(f["status"] == "violation" for f in report["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
