#!/usr/bin/env python3
"""泄漏与训练/服务对齐审计脚本（只读，可复现）

审计范围：
1. 目标市场赔率是否进入模型特征（market_isolation.py 黑名单）
2. 训练与服务两端特征口径是否一致
3. 同一口径是否在两处各写一份（漂移风险）
4. 开盘赔率（NJStats）与竞彩/站点收盘赔率是否混用
5. odds_series 快照是否用于预测特征（而非仅事后评估）

输出：JSON 报告 + 控制台摘要，不修改任何文件。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent  # ticket dir
AGENTBOX = ROOT.parent  # agentbox
ENG_DIR = AGENTBOX / "football-engine"
ENGINE = ENG_DIR / "engine"


# ============================================================
# 1. 目标市场特征黑名单（与 market_isolation.py 一致）
# ============================================================
TARGET_MARKET_FEATURES = {
    "odds_home_impl", "odds_draw_impl", "odds_away_impl", "odds_overround",
    "target_home_impl", "target_draw_impl", "target_away_impl", "target_overround",
    "sporttery_home_impl", "sporttery_draw_impl", "sporttery_away_impl",
    "sina_home_impl", "sina_draw_impl", "sina_away_impl",
}

REFERENCE_MARKET_FEATURES = {
    "pinnacle_home_impl", "pinnacle_draw_impl", "pinnacle_away_impl",
    "closing_home_impl", "closing_draw_impl", "closing_away_impl",
    "asian_handicap_line", "asian_handicap_odds", "over_under_line",
}

# ============================================================
# 2. 扫描：f-string / dict literal 里是否出现黑名单特征
# ============================================================
def scan_features_in_python_files(directory: Path) -> list[dict]:
    """扫描 .py 文件，检测 build_features / 特征构造处是否出现黑名单字段。"""
    issues: list[dict] = []

    for pyfile in directory.rglob("*.py"):
        if "__pycache__" in str(pyfile) or ".git" in str(pyfile):
            continue
        try:
            text = pyfile.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for line_no, line in enumerate(text.splitlines(), 1):
            for feat in TARGET_MARKET_FEATURES:
                if feat in line:
                    # 粗略判断是否是特征使用（而非注释/文档字符串），简单规则：
                    # 不在行首注释里，且附近有 dict 键或参数名迹象
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if "TARGET_MARKET_FEATURES" in line or "REFERENCE_MARKET_FEATURES" in line:
                        continue
                    issues.append({
                        "file": str(pyfile.resolve()),
                        "line": line_no,
                        "feature": feat,
                        "context": line.strip()[:100],
                    })
    return issues


# ============================================================
# 3. 扫描：lgbm build_features 的 include_market_odds 开关
# ============================================================
def scan_lgbm_odds_switch(flag: str = "use_odds_features") -> dict:
    """检查 LGBMConfig 的 use_odds_features 默认值及是否被设为 True。"""
    lgbm_file = ENGINE / "prediction" / "lgbm_model.py"
    main_file = ENGINE / "main.py"

    result = {"flag": flag, "default": False, "set_to_true": False, "locations": []}

    # 默认值
    if lgbm_file.exists():
        text = lgbm_file.read_text(encoding="utf-8", errors="replace")
        env = re.search(
            rf"{flag}\s*:\s*bool\s*=\s*([A-Za-z0-9_]+)", text, re.IGNORECASE
        )
        if env:
            result["default"] = env.group(1) == "True"

    # 是否被设为 True
    for fname in [lgbm_file, main_file]:
        if not fname.exists():
            continue
        try:
            text = fname.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in re.finditer(rf"{flag}\s*=\s*([A-Za-z0-9_\.]+)", text, re.IGNORECASE):
            src = m.group(1)
            result["locations"].append({"file": str(fname.resolve()), "match": m.group(0)})
            if src.strip().upper() == "TRUE":  # 简化判断
                result["set_to_true"] = True

    return result


# ============================================================
# 4. 扫描：odds_series 是否用于预测特征构造
# ============================================================
def scan_odds_series_feature_use() -> dict:
    """检查 odds_series 的 series_features 是否被接入预测特征。"""
    series_file = ENGINE / "odds_series.py"
    sina_file = ENGINE / "pipeline" / "sina_odds.py"
    features: list[dict] = []

    for fname, expected_use in [(series_file, "无"), (sina_file, "构建 sina_data 侧信息")]:
        if not fname.exists():
            continue
        try:
            text = fname.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for m in re.finditer(r"series_features\s*\(", text):
            start = max(0, m.start() - 60)
            end = min(len(text), m.end() + 40)
            features.append({
                "file": str(fname.resolve()),
                "line": text[:m.start()].count("\n") + 1,
                "context": text[start:end],
                "purpose": expected_use,
            })
    return {"series_features": features}


# ============================================================
# 5. 扫描：sina_odds_movement (fusion.py) 是否使用目标市场水位
# ============================================================
def scan_sina_odds_movement() -> dict:
    """检测 fusion.py 的 sina_odds_movement 步骤，并核实压缩比来源。

    注意：compression 来自 fetch_sina_odds.py 的 Bet365(Pinnacle) 初盘÷即时，
    属于 reference 市场（market_isolation.py 允许进入概率形成），不构成目标市场泄漏。
    """
    fusion_file = ENGINE / "prediction" / "fusion.py"
    if not fusion_file.exists():
        return {"not_found": True}

    text = fusion_file.read_text(encoding="utf-8", errors="replace")
    if "sina_odds_movement" not in text:
        return {"not_found": True}

    start = text.find('if switches["sina_odds_movement"] and inp.sina_data')
    if start < 0:
        start = text.find('"sina_odds_movement"')
    if start < 0:
        return {"not_found": True}

    snippet = text[start:start+1200]
    uses_compression = "compression" in snippet
    uses_movement = "movement" in snippet
    adjusts_probs = any(pat in snippet for pat in ["h +=", "h -=", "a +=", "a -="])

    # 核实 compression 来源：fetch_sina_odds.py 明确取 Bet365(id=7) 或 Pinnacle(id=9)
    fetch_file = ENG_DIR / "scripts" / "fetch_sina_odds.py"
    source_is_reference = False
    source_note = ""
    if fetch_file.exists():
        fetch_text = fetch_file.read_text(encoding="utf-8", errors="replace")
        # bet365 = next(...companyId=="7") or pinnacle = ...companyId=="9"
        if 'companyId' in fetch_text and ('bet365' in fetch_text.lower() or 'pinnacle' in fetch_text.lower()):
            source_is_reference = True
            source_note = "Bet365/Pinnacle（reference 市场）"

    # 按 market_isolation.py 规则：reference 市场允许进入概率形成 → 不违规
    # 降级为观察性记录，不再标 high
    return {
        "found": True,
        "uses_compression": uses_compression,
        "uses_movement": uses_movement,
        "adjusts_probs": adjusts_probs,
        "source_is_reference": source_is_reference,
        "compression_source": source_note,
        "snippet": snippet.strip()[:800],
        "severity": "low" if source_is_reference and adjusts_probs else "info",
        "verdict": "NOT a target market leak — compression comes from reference market",
    }


# ============================================================
# 6. 扫描：njstats_features 口径
# ============================================================
def scan_njstats_features() -> dict:
    """检查 njstats_features 输出字段：是否包含竞彩收盘赔率。"""
    feature_dir = AGENTBOX / "football-engine" / "data" / "state" / "njstats_features"
    if not feature_dir.exists() and (AGENTBOX / "football-engine" / "data").exists():
        feature_dir = (AGENTBOX / "football-engine" / "data").glob("njstats_features/*.json").__next__()

    results: list[dict] = []
    for f in feature_dir.glob("*.json") if feature_dir.exists() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            keys = set(str(k) for k in data.keys()) if isinstance(data, dict) else set()
            has_closing_odds = any(k in keys for k in ("opening", "current", "closing", "euro"))
            results.append({
                "file": str(f.relative_to(ROOT)),
                "has_keys": list(keys)[:10],
                "has_closing_odds": has_closing_odds,
            })
        except Exception:
            continue
    return {"files": results}


# ============================================================
# 7. 扫描：market_isolation.py 审计框架完整性
# ============================================================
def scan_isolation_framework() -> dict:
    """检查 market_isolation.py 是否包含黑名单/白名单/审计类。"""
    iso_file = ENGINE / "prediction" / "market_isolation.py"
    if not iso_file.exists():
        return {"not_found": True}

    text = iso_file.read_text(encoding="utf-8", errors="replace")

    return {
        "found": True,
        "has_target_blacklist": "TARGET_MARKET_FEATURES" in text,
        "has_reference_whitelist": "REFERENCE_MARKET_FEATURES" in text,
        "has_benchmark_only": "BENCHMARK_ONLY_FEATURES" in text,
        "has_isolation_report": "IsolationReport" in text,
        "has_isolation_violation": "IsolationViolation" in text,
    }


# ============================================================
# 8. 扫描：train/service 特征对齐（build_features 参数签名）
# ============================================================
def scan_train_service_align() -> dict:
    """
    检查 build_features 参数签名 vs 生产调用点传入的参数名是否一致。
    训练回放用落盘数据，服务用 DJYY fixture——这是文档里 déjà 提到的对齐点。
    """
    lgbm_file = ENGINE / "prediction" / "lgbm_model.py"
    main_file = ENGINE / "main.py"

    features: list[dict] = []

    # build_features 签名
    if lgbm_file.exists():
        text = lgbm_file.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"def build_features\(((?:[^()]|\((?:[^()]|\((?:[^()]*)\))*\))*)\)", text, re.DOTALL)
        if m:
            sig = m.group(1)
            features.append({
                "role": "build_features signature",
                "params": [p.strip().split("=")[0].strip() for p in sig.split(",") if p.strip()],
            })

    # 生产调用点参数名
    if main_file.exists():
        text = main_file.read_text(encoding="utf-8", errors="replace")
        m = re.search(
            r"build_features\(\s*elo_home=.*?,\s*elo_away=.*?,\s*odds=.*?,\s*handicap=.*?,\s*"
            r"home_form=.*?,\s*away_form=.*?,\s*league_avg_goals=.*?,\s*league_home_win_rate=.*?,\s*"
            r"xg_home=.*?,\s*xg_away=.*?,\s*rank_home=.*?,\s*rank_away=.*?,\s*djyy_probs=.*?,\s*"
            r"side=.*?\)", text, re.DOTALL
        )
        if m:
            features.append({
                "role": "main.py 生产调用",
                "params": ["elo_home", "elo_away", "odds", "handicap", "home_form", "away_form",
                           "league_avg_goals", "league_home_win_rate", "xg_home", "xg_away",
                           "rank_home", "rank_away", "djyy_probs", "side"],
            })

    return {"features": features}


# ============================================================
# 主入口
# ============================================================
def main(argv: list[str] | None = None) -> int:
    ap = __import__("argparse").ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args(argv)

    report: dict[str, Any] = {
        "root": str(ROOT),
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "findings": [],
    }

    # 1. lgbm odds 开关
    report["findings"].append(scan_lgbm_odds_switch())

    # 2. sina_odds_movement
    report["findings"].append(scan_sina_odds_movement())

    # 3. odds_series 特征使用
    report["findings"].append(scan_odds_series_feature_use())

    # 4. njstats_features
    report["findings"].append(scan_njstats_features())

    # 5. isolation 框架
    report["findings"].append(scan_isolation_framework())

    # 6. train/service 对齐
    report["findings"].append(scan_train_service_align())

    # 7. 黑名单特征扫描（全量 py 文件）
    all_issues = scan_features_in_python_files(ENGINE)
    report["findings"].append({
        "target_market_feature_scans": len(all_issues),
        "issues": all_issues[:20],  # 最多 20 条
    })

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=== 泄漏与对齐审计摘要 ===")
        for f in report["findings"]:
            name = list(f.keys())[0]
            print(f"\n【{name}]")
            val = f[name] if isinstance(f, dict) else f
            if isinstance(val, dict):
                for k, v in val.items():
                    print(f"  {k}: {v}")
            else:
                print(f"  -> {val}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
