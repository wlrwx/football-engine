#!/usr/bin/env python3
"""泄漏与训练/服务对齐审计（只读，可复现）。

    python -m scripts.leak_audit_check          # 控制台报告
    python -m scripts.leak_audit_check --json   # JSON 报告

退出码：0 = 没有 violation 判定；1 = 有 violation。不写任何文件。

合并说明（2026-11-04）
---------------------
原来有两份重复入口：本文件（真扫描）与 `scripts/leak_audit/audit_leakage.py`
（写死结论的叙述稿）。现合并为本文件一份。叙述稿里**本次没有复核**的观察点
（DC 层 market_blend、devig_shin 口径）保留在 `UNVERIFIED_NOTES`，只进报告、
不参与判定、不给高于 info 的 severity。

判定口径
--------
输出的是**判定结论**，计数（命中数、每文件条数、参数个数）只作为报告文本。
severity 规则：生产开关关闭（路径不活）的发现一律封顶 info；参考市场信号
按 market_isolation 的口径不算目标市场泄漏。
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import io
import json
import re
import sys
import tokenize
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SELF = Path(__file__).resolve()
GATE_NAMES = {"include_market_odds", "use_odds_features"}
DEFINITION_NAMES = {
    "TARGET_MARKET_FEATURES", "REFERENCE_MARKET_FEATURES", "BENCHMARK_ONLY_FEATURES",
}
SEVERITY_ORDER = ("info", "low", "medium", "high")
CALL_SITES = ("engine/main.py", "engine/learning/lgbm_shadow.py")


# ------------------------------------------------------------------ 工具
def rel_path(path: Path) -> str:
    """仓库相对路径，POSIX 分隔符（Windows 上也是 /）。"""
    return path.relative_to(ROOT).as_posix()


def read_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def read_config(rel: str) -> dict:
    return json.loads(read_text(rel))


def py_files(*dirs: str):
    for d in dirs:
        for p in sorted((ROOT / d).rglob("*.py")):
            if p.resolve() != SELF and "__pycache__" not in p.parts:
                yield p


def cap_severity(severity: str, *, path_active: bool) -> str:
    """路径不活（生产开关关闭）时，判定不得高于 info。"""
    if path_active:
        return severity
    return "info"


def reads_key(tree: ast.AST, key: str) -> list[int]:
    """消费某个 dict 键的行号：x["key"] / x.get("key") / x.pop("key")。"""
    lines: list[int] = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                and n.slice.value == key):
            lines.append(n.lineno)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"get", "pop"} and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == key):
            lines.append(n.lineno)
    return lines


def allowed_ranges(tree: ast.AST) -> list[tuple[int, int, str]]:
    """黑名单字段允许出现的行区间：定义/特征名表、受闸赋值、__main__ 自检。"""
    ranges: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if names & DEFINITION_NAMES or any(n.endswith("FEATURE_NAMES") for n in names):
                ranges.append((node.lineno, node.end_lineno or node.lineno, "definition"))
        if isinstance(node, ast.If):
            if any(isinstance(n, ast.Name) and n.id in GATE_NAMES
                   for n in ast.walk(node.test)):
                ranges.append((node.lineno, node.end_lineno or node.lineno, "gated"))
            if any(isinstance(n, ast.Name) and n.id == "__name__"
                   for n in ast.walk(node.test)):
                ranges.append((node.lineno, node.end_lineno or node.lineno, "selftest"))
    return ranges


def occurrences(text: str, features: set[str]) -> list[tuple[int, str]]:
    """代码里对黑名单字段的引用（注释/docstring 不算）。"""
    out: list[tuple[int, str]] = []
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        words: list[str] = []
        if tok.type == tokenize.NAME:
            words = [tok.string]
        elif tok.type == tokenize.STRING:
            try:
                value = ast.literal_eval(tok.string)
            except (ValueError, SyntaxError):
                value = None
            words = [value] if isinstance(value, str) else []
        elif fstring_middle is not None and tok.type == fstring_middle:
            words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", tok.string)
        out.extend((tok.start[0], w) for w in words if w in features)
    return out


def classify_hits(text: str, features: set[str], *, in_tests: bool = False) -> dict:
    ranges = [] if in_tests else allowed_ranges(ast.parse(text))
    allowed: list[dict] = []
    unallowed: list[dict] = []
    for line, feat in occurrences(text, features):
        kind = "tests" if in_tests else next(
            (k for lo, hi, k in ranges if lo <= line <= hi), "")
        (allowed if kind else unallowed).append({"line": line, "feature": feat, "kind": kind})
    return {"allowed": allowed, "unallowed": unallowed}


def gate_set_to_true() -> list[str]:
    """任何把 use_odds_features / include_market_odds 写死成 True 的位置。"""
    offenders: list[str] = []
    for p in py_files("engine", "scripts", "tests", "evolution"):
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if (isinstance(n, ast.keyword) and n.arg in GATE_NAMES
                    and isinstance(n.value, ast.Constant) and n.value.value is True):
                offenders.append(f"{rel_path(p)}:{n.lineno}")
            # Assign 与 AnnAssign 都要看：dataclass 字段默认值
            # （use_odds_features: bool = False）是带注解的赋值。
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    name = (t.id if isinstance(t, ast.Name)
                            else t.attr if isinstance(t, ast.Attribute) else "")
                    if (name in GATE_NAMES and isinstance(n.value, ast.Constant)
                            and n.value.value is True):
                        offenders.append(f"{rel_path(p)}:{n.lineno}")
    return offenders


def call_kwargs(rel: str) -> set[str]:
    calls = [n for n in ast.walk(ast.parse(read_text(rel)))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "build_features"]
    passed: set[str] = set()
    for c in calls:
        passed |= {kw.arg for kw in c.keywords if kw.arg}
    return passed


# ------------------------------------------------------------------ 1 黑名单
def audit_blacklist() -> dict:
    """判定：黑名单字段的命中是否全部落在允许位置。计数只进报告文本。"""
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES

    features = set(TARGET_MARKET_FEATURES)
    per_file: dict[str, int] = {}
    kinds: dict[str, int] = {}
    unallowed: list[str] = []
    for p in py_files("engine", "tests"):
        rel = rel_path(p)
        res = classify_hits(p.read_text(encoding="utf-8"), features,
                            in_tests=rel.startswith("tests/"))
        hits = res["allowed"] + res["unallowed"]
        if hits:
            per_file[rel] = len(hits)
        for h in res["allowed"]:
            kinds[h["kind"]] = kinds.get(h["kind"], 0) + 1
        unallowed.extend(f"{rel}:{h['line']} {h['feature']}" for h in res["unallowed"])

    ok = not unallowed
    return {
        "id": "target_market_blacklist",
        "title": "目标市场特征黑名单扫描",
        "verdict": "all_hits_in_allowed_locations" if ok else "unexpected_hit",
        "severity": "info" if ok else "high",
        "status": "clean" if ok else "violation",
        "evidence": sorted(unallowed),
        "counts_for_humans": {
            "blacklist_fields": len(features),
            "hits_total": sum(per_file.values()),
            "hits_per_file": per_file,
            "hits_per_kind": kinds,
        },
        "note": "允许位置 = 黑名单/特征名表定义、受 include_market_odds 闸控制的"
                "赋值（含 else 置零）、__main__ 自检演示、tests/。lgbm_model.py 的"
                "命中是特征名表 + 受闸赋值，不是「参数注释」。计数只统计代码引用，"
                "不含注释/docstring 里的顺带提及。",
    }


# ------------------------------------------------------------------ 2 开关
def audit_lgbm_gate() -> dict:
    """判定：按当前配置的闸构造特征时，目标市场赔率是否进入特征。"""
    from engine.prediction.lgbm_model import LGBMConfig, build_features
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES

    offenders = gate_set_to_true()
    default = LGBMConfig().use_odds_features
    configs = {"LGBMConfig() 默认值": default}
    for rel in ("config/prediction.json", "evolution/demo/base_prediction.json"):
        configs[rel] = read_config(rel)["lgbm"]["use_odds_features"]
    leaks: list[str] = []
    for rel, gate in configs.items():
        a = build_features(elo_home=1500.0, elo_away=1400.0, odds=(2.0, 3.2, 3.5),
                           include_market_odds=gate)
        b = build_features(elo_home=1500.0, elo_away=1400.0, odds=(1.2, 6.0, 15.0),
                           include_market_odds=gate)
        for k in sorted(set(TARGET_MARKET_FEATURES) & set(a)):
            if not (a[k] == b[k] == 0.0):
                leaks.append(f"{rel}:{k}")

    ok = not offenders and not leaks
    return {
        "id": "lgbm_odds_gate",
        "title": "lgbm use_odds_features 开关",
        "verdict": "target_odds_do_not_enter_features" if ok else "target_odds_enter_features",
        "severity": "info" if ok else "high",
        "status": "clean" if ok else "violation",
        "default": LGBMConfig().use_odds_features,
        "config_switches": configs,
        "set_to_true_at": offenders,
        "leaking_features": leaks,
        "note": "判定按「特征是否随目标市场赔率变化」作出，不断言配置字面值；"
                "翻开开关会真泄漏，届时本判定转 violation。",
    }


# ------------------------------------------------------------------ 3 水位
def _sina_source_company() -> str | None:
    """行为核实：混入一家「竞彩官方」时，取数代码选谁。"""
    path = ROOT / "scripts" / "fetch_sina_odds.py"
    spec = importlib.util.spec_from_file_location("fetch_sina_odds_audit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    match = {"matchId": "1", "matchNo": "周五001", "team1": "A", "team2": "B"}
    bet365 = {"companyId": "7", "companyName": "Bet365",
              "o1Ini": "2.1", "o2Ini": "3.2", "o3Ini": "3.6",
              "o1New": "1.8", "o2New": "3.3", "o3New": "4.2"}
    other = {"companyId": "99", "companyName": "竞彩官方",
             "o1Ini": "9.9", "o2Ini": "9.9", "o3Ini": "9.9",
             "o1New": "1.1", "o2New": "1.1", "o3New": "1.1"}
    snap = mod.extract_odds_snapshot(match, [bet365, other], [], [], {})
    return (snap.get("euro") or {}).get("company")


def audit_sina_movement() -> dict:
    """判定：水位信号是否目标市场泄漏；开关关闭时 severity 封顶 info。"""
    from engine.prediction.fusion import FusionInput, fuse_probabilities

    step = "sina_odds_movement"
    found = step in read_text("engine/prediction/fusion.py")
    post = dict(read_config("config/prediction.json")["fusion"]["post_fusion"])
    active = bool(post.get(step))
    signal = {"movement": {"home": "down", "draw": "flat", "away": "up"},
              "compression": {"home": 1.2, "draw": 1.0, "away": 0.8}}
    out = fuse_probabilities(FusionInput(
        model_probs=(0.5, 0.3, 0.2), sina_data=signal, post_fusion=post))
    fired = any(t["step"] == step for t in out.trace)
    changed = out.probs != (0.5, 0.3, 0.2)

    try:
        company = _sina_source_company()
    except Exception as exc:  # pragma: no cover - 取数脚本不可导入时如实降级
        company = None
        source_note = f"未能行为核实取数公司：{exc}"
    else:
        source_note = f"取数首选公司 = {company}"
    is_reference = company in {"Bet365", "Pinnacle"}

    if not active:
        verdict = "path_inactive_no_probability_change"
        severity, status = "info", "clean"
        if fired or changed:
            verdict, severity, status = "switch_off_but_probabilities_changed", "high", "violation"
    elif is_reference:
        verdict, severity, status = "reference_market_signal_allowed", "low", "by_design"
    else:
        verdict, severity, status = "unidentified_market_source", "high", "violation"

    return {
        "id": "sina_odds_movement",
        "title": "fusion 新浪水位信号",
        "step_found": found,
        "switch_on": active,
        "step_fired": fired,
        "probabilities_changed": changed,
        "source_is_reference": is_reference,
        "verdict": verdict,
        "severity": cap_severity(severity, path_active=active),
        "status": status,
        "note": f"{source_note}；compression = 该公司初盘÷即时。市场隔离口径允许"
                "参考市场信号进入概率形成，故不是目标市场泄漏；且生产开关关闭时"
                "该路径不活，判定封顶 info（原 high 误报已降级）。",
    }


# ------------------------------------------------------------------ 4 序列
def audit_odds_series() -> dict:
    """判定：series 是否被概率形成消费；列出真实消费者。"""
    from engine.prediction.fusion import FusionInput, fuse_probabilities
    from engine.pipeline.sina_odds import build_sina_data

    key = "series"
    fusion_reads = reads_key(ast.parse(read_text("engine/prediction/fusion.py")), key)
    base = {"movement": {"home": "down"},
            "compression": {"home": 1.2, "draw": 1.0, "away": 1.0}}
    injected = {**base, key: {"points": 9, "recent_home": -5.0, "recent_away": 4.0}}
    p1 = fuse_probabilities(FusionInput(model_probs=(0.5, 0.3, 0.2), sina_data=base)).probs
    p2 = fuse_probabilities(FusionInput(model_probs=(0.5, 0.3, 0.2), sina_data=injected)).probs

    consumers: list[str] = []
    for p in py_files("engine", "scripts"):
        for line in reads_key(ast.parse(p.read_text(encoding="utf-8")), key):
            consumers.append(f"{rel_path(p)}:{line}")

    built = build_sina_data({"euro": {}}, "1999-01-01_周一999")
    ok = not fusion_reads and p1 == p2
    return {
        "id": "odds_series",
        "title": "odds_series 特征使用",
        "built_in": "engine/pipeline/sina_odds.py build_sina_data",
        "built_key_present": key in built,
        "fusion_read_lines": fusion_reads,
        "probabilities_changed_by_series": p1 != p2,
        "consumers": consumers,
        "verdict": "not_used_in_probability_formation" if ok else "used_in_probability_formation",
        "severity": "info" if ok else "high",
        "status": "clean" if ok else "violation",
        "note": "消费者是页面展示（build_site 水位表/标签）与 predictions.json 落盘，"
                "不是「仅用于结算验证」——engine/review 与 scripts 里没有读取该键的代码。",
    }


# ------------------------------------------------------------------ 5 开盘
def audit_njstats_opening() -> dict:
    """判定：NJStats 侧只读开盘价，站点价（目标市场）不进特征。"""
    from engine.sources.njstats_features import devig, extract_record
    from engine.sources.njstats_side import SideSignal

    record = {"matchId": "m1", "homeName": "A", "awayName": "B",
              "openingHome": 2.5, "openingDraw": 3.2, "openingAway": 2.8,
              "closingHome": 1.9, "closingDraw": 3.4, "closingAway": 4.0}
    out = extract_record(record, "2026-11-04", "2026-11-04 08:00:00")
    opening_ok = (out["opening"] == {"home": 2.5, "draw": 3.2, "away": 2.8}
                  and out["opening_fair"] == devig(out["opening"]))

    def side_features(site_fair):
        return SideSignal(home="A", away="B", kickoff="", snapshot_at="",
                          open_fair=(0.4, 0.3, 0.3), site_fair=site_fair).to_features()

    base = side_features(None)
    site_ok = bool(base) and side_features((0.9, 0.05, 0.05)) == base
    ok = opening_ok and site_ok
    return {
        "id": "njstats_opening",
        "title": "NJStats 开盘赔率与站点价",
        "reads_opening_only": opening_ok,
        "site_price_reaches_features": not site_ok,
        "verdict": "opening_only_site_price_excluded" if ok else "target_price_in_features",
        "severity": "info" if ok else "high",
        "status": "clean" if ok else "violation",
        "note": "openingHome 属于哪家市场，采集代码未标识（另列待核，info）；"
                "当前 lgbm_blend=false 且 lgbm_status.ready=false，不进生产融合。",
    }


# ------------------------------------------------------------------ 6 对齐
def audit_train_serving_alignment() -> dict:
    """判定：未传参数是否都取签名默认值，两端特征是否逐位相同。

    不判「参数个数是否相等」——签名加一个带默认值的参数属正常演进。
    """
    from engine.prediction.lgbm_model import build_features

    params = inspect.signature(build_features).parameters
    gate = read_config("config/prediction.json")["lgbm"]["use_odds_features"]
    samples = {
        "elo_home": 1500.0, "elo_away": 1400.0, "odds": (2.0, 3.2, 3.5),
        "handicap": -0.5, "home_form": 1.2, "away_form": 0.9,
        "league_avg_goals": 2.6, "league_home_win_rate": 0.44,
        "xg_home": 1.4, "xg_away": 1.1,
        "djyy_probs": {"home": 0.5, "draw": 0.3, "away": 0.2},
        "is_knockout": False, "rank_home": 3, "rank_away": 8,
        "side": {"side_open_home": 0.4}, "include_market_odds": gate,
    }
    sites = {rel: call_kwargs(rel) for rel in CALL_SITES}
    missing_required: list[str] = []
    unknown: list[str] = []
    omitted: dict[str, list[str]] = {}
    for rel, passed in sites.items():
        unknown.extend(f"{rel}:{k}" for k in sorted(passed - set(params)))
        omitted[rel] = sorted(set(params) - passed)
        missing_required.extend(
            f"{rel}:{k}" for k in omitted[rel]
            if params[k].default is inspect.Parameter.empty)

    def value(name: str):
        return samples.get(name, params[name].default)

    diff: dict[str, Any] = {}
    if not missing_required and not unknown:
        dicts = [build_features(**{k: value(k) for k in sorted(passed)})
                 for passed in sites.values()]
        first = dicts[0]
        for other in dicts[1:]:
            for k in set(first) | set(other):
                if first.get(k) != other.get(k):
                    diff[k] = [first.get(k), other.get(k)]

    ok = not missing_required and not unknown and not diff
    return {
        "id": "train_serving_alignment",
        "title": "训练/服务特征对齐",
        "verdict": "no_feature_skew" if ok else "feature_skew",
        "severity": "info" if ok else "high",
        "status": "clean" if ok else "violation",
        "omitted_params": omitted,
        "missing_required_params": missing_required,
        "unknown_params": unknown,
        "feature_diff": diff,
        "counts_for_humans": {
            "signature_params": len(params),
            "passed_per_call_site": {rel: len(p) for rel, p in sites.items()},
        },
        "note": "原审计「16 个参数完全一致」不成立：生产调用未传 is_knockout，"
                "训练调用未传 odds/is_knockout。实质结论成立——未传参数两端同取"
                "签名默认值，共同输入下特征逐位相同，无实际偏移。",
    }


# ------------------------------------------------------------------ 报告
UNVERIFIED_NOTES = [
    "DC 层 market_blend_weight 把竞彩赔率拼进模型概率"
    "（engine/prediction/dixon_coles.py 的 market_odds 分支）——本次未复核，待核。",
    "devig 口径：回放 devig('proportional') vs 服务 select_devig_method→devig_shin"
    "（engine/prediction/calibration.py）——本次未复核，待核；"
    "calibration.py 属禁触路径，任何改动必须人工 review。",
]


def build_report() -> dict:
    findings = [
        audit_blacklist(),
        audit_lgbm_gate(),
        audit_sina_movement(),
        audit_odds_series(),
        audit_njstats_opening(),
        audit_train_serving_alignment(),
    ]
    return {
        "root": ROOT.as_posix(),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "findings": findings,
        "unverified_notes": [
            {"severity": cap_severity("medium", path_active=False), "note": n}
            for n in UNVERIFIED_NOTES
        ],
    }


def render_text(report: dict) -> str:
    lines = ["=== 泄漏与训练/服务对齐审计（只读） ==="]
    for f in report["findings"]:
        lines.append(f"\n【{f['title']}] {f['verdict']}  (severity={f['severity']}, {f['status']})")
        for key in ("evidence", "set_to_true_at", "leaking_features", "consumers",
                    "missing_required_params", "unknown_params", "feature_diff"):
            if f.get(key):
                lines.append(f"  {key}: {f[key]}")
        if "counts_for_humans" in f:
            lines.append(f"  计数（仅供人读，不作判定）：{f['counts_for_humans']}")
        if "omitted_params" in f:
            lines.append(f"  未传参数（取签名默认值）：{f['omitted_params']}")
        lines.append(f"  说明：{f['note']}")
    lines.append("\n--- 未复核的观察点（来自被合并的 audit_leakage.py，只作记录） ---")
    for n in report["unverified_notes"]:
        lines.append(f"  [{n['severity']}] {n['note']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="泄漏与训练/服务对齐审计（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = ap.parse_args(argv)

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(report))
    return 1 if any(f["status"] == "violation" for f in report["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
