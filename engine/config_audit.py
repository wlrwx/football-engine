#!/usr/bin/env python3
"""配置审计：找出"声明了但没有任何代码读取"的配置键（2026-10-11 新增）

## 为什么需要这个

本项目已被"配置说谎"咬过多次，每一处都是同一个模式：

| 配置 | 实际情况 |
|---|---|
| `fusion.djyy_weight: 0.2` | DJYY 数据源已死，实际贡献 0 |
| `prediction.handicap_smoothing_weight: 0.274`（在 strategy.json） | 代码读的字段叫 `handicap_smoothing` 且在 prediction.json —— **值恰好相同，纯属巧合** |
| `edge_gates.strict_min_probability_edge: 0.05` | 从初始提交起就从未被任何代码读取 |
| `strategy.model_weight.{base,max,calibration_prior}` | 同上，整块从未被读取 |

这些都不会报错。它们只是安静地躺在配置里，让人以为调它们有效。

本脚本做两件事：
1. 静态扫描：找出 config/**/*.json 里没有任何消费者引用的键
2. 运行时交叉验证：对 dataclass 型配置，检查传入的值是否真的被采用

用法：
    python -m engine.config_audit             # 报告
    python -m engine.config_audit --strict    # 有未消费键则返回非零（可作 CI 门禁）
    python -m engine.config_audit --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
# 只扫生产代码。tests/ 不算"消费者"——
# 一个只在测试里被引用的配置键，在生产上等同于死配置。
SRC_DIRS = [ROOT / "engine", ROOT / "scripts"]

# 明确标注为"已知未接线"的键：留着是因为有规划，但必须显式声明，
# 否则审计器会把它们和"忘记接线"的键混为一谈。
KNOWN_UNWIRED = {
    "strategy.json": {
        "edge_gates.strict_min_probability_edge":
            "严格模式门槛从未实现（自 init 提交起）。当前只读 min_probability_edge。",
        "edge_gates.strict_min_ev":
            "同上。",
        "strict_mode.until_samples":
            "严格模式整块未接线。",
        "strict_mode.max_stake":
            "严格模式整块未接线。",
    },
}


def _iter_json(node, prefix=""):
    """产出叶子键的完整点分路径（字符串），不下钻列表。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_json(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(node, list):
        # 列表视为一个叶子：time_decay_anchors 之类整体算一个键
        if prefix:
            yield prefix
    else:
        if prefix:
            yield prefix


def _all_python_text() -> str:
    out = []
    for d in SRC_DIRS:
        if not d.exists():
            continue
        for p in d.rglob("*.py"):
            # 排除本文件：KNOWN_UNWIRED 里写满了键名字面量，
            # 若把自己算进源码，每个"已知未接线"键都会被自查成"已引用"。
            if p.name == "config_audit.py":
                continue
            try:
                out.append(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return "\n".join(out)


def _dataclass_consumers() -> dict[str, set[str]]:
    """配置块 → 消费它的 dataclass 字段名集合。

    关键：这些配置块是通过
        XConfig(**{k: v for k, v in cfg.get("block", {}).items()
                   if k in XConfig.__dataclass_fields__})
    展开消费的，键名**不会**在代码里以字面量出现。
    只做字面量搜索会产生大量假阳性（实测 prediction.* 整块 9 个键全部误报）。
    """
    out: dict[str, set[str]] = defaultdict(set)
    try:
        import dataclasses
        from engine.prediction.dixon_coles import DixonColesConfig
        from engine.prediction.monte_carlo import MonteCarloConfig
        from engine.prediction.lgbm_model import LGBMConfig
        from engine.prediction.isotonic_cal import CalibrationConfig
        from engine.strategy.circuit_breaker import CircuitBreakerConfig
        mapping = {
            "prediction": [DixonColesConfig, MonteCarloConfig],
            "lgbm": [LGBMConfig],
            "calibration": [CalibrationConfig],
            "circuit_breaker": [CircuitBreakerConfig],
        }
        for block, classes in mapping.items():
            for cls in classes:
                for f in dataclasses.fields(cls):
                    out[block].add(f.name)
    except Exception as e:
        # 静默返回空会让整轮审计"零告警"却毫无意义 —— 必须显式暴露
        out["__error__"] = {f"{type(e).__name__}: {e}"}
    return out


def _key_referenced(key: str, src: str, dc_fields: set[str] | None = None) -> bool:
    """键是否真的被消费：dataclass 字段命中，或代码里出现字面量。"""
    parts = key.split(".")
    leaf = parts[-1]
    block = parts[0] if len(parts) > 1 else ""
    # (1) 通过 **dict 展开进 dataclass
    if dc_fields and block and leaf in dc_fields:
        return True
    # (2) 代码里以字面量出现
    return (f'"{leaf}"' in src or f"'{leaf}'" in src
            or f'"{key}"' in src or f"'{key}'" in src)


def audit_config_keys() -> dict:
    src = _all_python_text()
    dc_map = _dataclass_consumers()
    report: dict[str, dict] = {}
    for cfg_file in sorted(CONFIG_DIR.glob("*.json")):
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:
            report[cfg_file.name] = {"error": f"{type(e).__name__}: {e}"}
            continue
        keys = list(_iter_json(data))
        unref, ref = [], []
        for k in keys:
            block = k.split(".")[0] if "." in k else ""
            _ref = _key_referenced(k, src, dc_map.get(block))
            (ref if _ref else unref).append(k)
        known = KNOWN_UNWIRED.get(cfg_file.name, {})
        report[cfg_file.name] = {
            "total_leaf_keys": len(keys),
            "referenced": len(ref),
            "unreferenced": unref,
            "known_unwired": known,
            "unexpected_unwired": [k for k in unref if k not in known],
            "dataclass_aware_blocks": sorted(dc_map.keys()),
        }
    return report


# ---------------------------------------------------------------- 运行时交叉验证

def audit_dataclass_wiring() -> dict:
    """检查 dataclass 型配置：传入的键名是否与 dataclass 字段一致。

    背景：`main.py` 用
        MCConfig(**{k: v for k, v in pred_cfg["prediction"].items()
                    if k in MCConfig.__dataclass_fields__})
    这种过滤写法会把**名字对不上**的键静默丢弃。
    """
    out = {}
    try:
        from engine.prediction.monte_carlo import MonteCarloConfig
        import dataclasses
        pred = json.loads((CONFIG_DIR / "prediction.json").read_text(encoding="utf-8"))["prediction"]
        mc_fields = {f.name for f in dataclasses.fields(MonteCarloConfig)}
        # 两份配置里语义相近但命名不同的键
        alias_risks = []
        pairs = [("home_adv_weight", "home_advantage"),
                 ("handicap_smoothing_weight", "handicap_smoothing"),
                 ("xg_calibration", "xg_calibration")]
        for cfg_key, dc_field in pairs:
            if cfg_key in pred and cfg_key != dc_field:
                alias_risks.append({
                    "config_key": cfg_key, "config_value": pred.get(cfg_key),
                    "dataclass_field": dc_field, "field_in_config": dc_field in pred,
                    "note": "同名概念、两处命名；只调 config_key 会让两个模型分叉",
                })
        out["monte_carlo"] = {
            "fields": sorted(mc_fields),
            "config_keys_not_in_dataclass": sorted(k for k in pred if k not in mc_fields),
            "dataclass_fields_missing_from_config": sorted(mc_fields - set(pred)),
            "alias_risks": alias_risks,
        }
    except Exception as e:
        out["monte_carlo"] = {"error": f"{type(e).__name__}: {e}"}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="有未消费键则返回非零")
    args = ap.parse_args()

    cfg_report = audit_config_keys()
    wiring = audit_dataclass_wiring()

    if args.json:
        print(json.dumps({"config_keys": cfg_report, "dataclass_wiring": wiring},
                         ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print("配置审计 —— 找出「声明了但没有代码读取」的键")
    print("=" * 72)
    n_unexpected = 0
    for fname, rep in cfg_report.items():
        if "error" in rep:
            print(f"\n  {fname}: 解析失败 {rep['error']}")
            continue
        print(f"\n  {fname}  （叶子键 {rep['total_leaf_keys']}，被引用 {rep['referenced']}）")
        if rep["known_unwired"]:
            print(f"    已知未接线（{len(rep['known_unwired'])}，已在 KNOWN_UNWIRED 声明）:")
            for k, why in rep["known_unwired"].items():
                print(f"      · {k}\n          {why}")
        if rep["unexpected_unwired"]:
            n_unexpected += len(rep["unexpected_unwired"])
            print(f"    ⚠ 未声明的未消费键（{len(rep['unexpected_unwired'])}）:")
            for k in rep["unexpected_unwired"]:
                print(f"      ✗ {k}")

    print("\n" + "=" * 72)
    print("dataclass 接线交叉验证")
    print("=" * 72)
    mc = wiring.get("monte_carlo", {})
    if "error" in mc:
        print(f"  {mc['error']}")
    else:
        for r in mc.get("alias_risks", []):
            print(f"  ⚠ {r['config_key']}={r['config_value']} 与 dataclass 字段 "
                  f"{r['dataclass_field']} 概念相同但命名不同")
            print(f"      {r['note']}")

    print("\n" + "=" * 72)
    verdict = ("✓ 无未声明的未消费键" if n_unexpected == 0
               else f"✗ {n_unexpected} 个未声明的未消费键")
    print(f"  判定: {verdict}")
    print("  （已声明的未接线键不算问题 —— 它们在 KNOWN_UNWIRED 里写明了原因）")

    out = ROOT / "data" / "state" / "config_audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config_keys": cfg_report, "dataclass_wiring": wiring,
        "unexpected_unwired_total": n_unexpected, "verdict": verdict,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 已写入 {out.relative_to(ROOT)}")

    if args.strict and n_unexpected:
        print("\n::error::存在未声明的未消费配置键")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
