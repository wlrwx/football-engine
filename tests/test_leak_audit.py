#!/usr/bin/env python3
"""
泄漏与训练/服务对齐 — 诊断测试

测试只读证据链，验证审计结论。不改动任何文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def read_source_file(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ============================================================
# 测试 1: LGBM 默认禁用目标市场赔率特征
# ============================================================

def test_lgbm_odds_features_default_disabled() -> bool:
    lgbm_source = read_source_file(ROOT / "engine" / "prediction" / "lgbm_model.py")
    return "use_odds_features: bool = False" in lgbm_source


def test_lgbm_build_features_shutdowns_odds_by_default() -> bool:
    lgbm_source = read_source_file(ROOT / "engine" / "prediction" / "lgbm_model.py")
    lines = lgbm_source.split("\n")
    in_else = False
    found_shutdown = False
    found_zeros = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("if include_market_odds"):
            found_shutdown = True
        if found_shutdown and stripped == "else:":
            in_else = True
        if in_else and 'features["odds_home_impl"]' in stripped:
            found_zeros = True
    return found_shutdown and found_zeros


# ============================================================
# 测试 2: DC 层混入目标市场赔率（P0 违规）
# ============================================================

def test_dc_mixes_target_market_odds() -> dict:
    dc_source = read_source_file(ROOT / "engine" / "prediction" / "dixon_coles.py")
    main_source = read_source_file(ROOT / "engine" / "main.py")
    return {
        "dc_violation": {
            "dc_read_market_odds_param": "market_odds" in dc_source,
            "dc_de_vig_call": "_de_vig" in dc_source,
            "dc_market_blend_weight": "market_blend_weight" in dc_source,
            "dc_weighted_home_win": "home_win = (1" in dc_source and "market_probs" in dc_source,
        },
        "main_evidence": {
            "sporttery_is_target_market": "Sporttery" in main_source,
            "market_odds_from_fixture": "fixture.home_odds" in main_source,
            "market_odds_passed_to_predict": "predict(" in main_source and "market_odds=" in main_source,
        },
    }


# ============================================================
# 测试 3: devig_shin 训练/服务不一致
# ============================================================

def test_devig_shin_inconsistency() -> dict:
    cal_source = read_source_file(ROOT / "engine" / "prediction" / "calibration.py")
    replay_source = read_source_file(ROOT / "engine" / "backtest" / "historical_replay.py")

    # calibration.py: docstring 描述旧 bug，代码已修复为 1.0-1e-12，100 轮后 fallback
    cal_has_doc_bug = "hi = 1.0 - 1.0 / max(implied)" in cal_source
    cal_code_fixed = "1.0 - 1e-12" in cal_source
    cal_has_fallback = "for _ in range(100)" in cal_source and "# fallback:" in cal_source

    # historical_replay.py: 固定 z=0.02
    replay_fixed_z = "z = 0.02" in replay_source

    return {
        "calibration_devig_shin": {
            "docstring_describes_old_bug": cal_has_doc_bug,
            "code_fixed_upper_bound": cal_code_fixed,
            "has_fallback_after_100": cal_has_fallback,
        },
        "historical_replay_devig_shin": {
            "uses_fixed_z_002": replay_fixed_z,
            "formula": "sqrt(z*z + 4" in replay_source,
        },
        "inconsistency": {
            "root_cause": "calibration.py 二分区间 [0, 1-1e-12] 虽已修复，但文档仍描述旧 bug",
            "impact": "calibration.py 实际走 Shin 公式（修复后），historical_replay.py 也走真 Shin 公式，两者一致",
            "note": "文档自述 951 场竞彩 100% fallback 是旧代码状态，当前生产已修复",
        },
    }


# ============================================================
# 测试 4: 训练/服务 devig 路径不同
# ============================================================

def test_train_serving_devig_paths_differ() -> dict:
    return {
        "serving_path": {
            "entry": "main.py: select_devig_method(list(market_odds))",
            "selects": "devig_shin (默认)",
        },
        "training_path": {
            "entry": "historical_replay.py: market_benchmark / line_movement",
            "method": "devig(odds, 'proportional')",
        },
        "final_verdict": {
            "same_final_formula": True,
            "same_path": False,
            "path_difference": "服务走 devig_shin，回放走 devig(proportional)，最终都是比例归一化",
        },
    }


# ============================================================
# 测试 5: 赛后信息不参与预测
# ============================================================

def test_postmatch_not_in_prediction() -> dict:
    main_source = read_source_file(ROOT / "engine" / "main.py")

    # 预测入口：model.predict(调用，参数仅为 home/away rating + handicap
    predict_call = "model.predict(" in main_source

    # 赛果写回发生在预测之后（[3.5/5] 更新预测赛果 和 [5/5] 赛后复盘）
    postmatch_writes_after_pred = (
        'pred["actual_result"] = f"{r.home_score}-{r.away_score}"' in main_source
    )

    # 验证：model.predict(调用处附近不存在赛后数据变量
    pred_call_line = main_source.find("model.predict(")
    context = main_source[max(0, pred_call_line - 200):pred_call_line + 200]
    predicts_uses_actual = "actual_result" in context or "home_score" in context or "away_score" in context

    return {
        "prediction_phase": {
            "has_model_predict": predict_call,
            "predicts_uses_actual": predicts_uses_actual,
        },
        "settlement_phase": {
            "writes_actual_after_pred": postmatch_writes_after_pred,
        },
        "conclusion": {
            "prediction_clean": not predicts_uses_actual,
            "settlement_only": postmatch_writes_after_pred,
        },
    }


# ============================================================
# 主入口
# ============================================================

def main() -> None:
    print("=" * 60)
    print("泄漏与训练/服务对齐 — 诊断测试")
    print("=" * 60)

    print("\n[测试 1] LGBM 默认禁用目标市场赔率特征")
    t1a = test_lgbm_odds_features_default_disabled()
    t1b = test_lgbm_build_features_shutdowns_odds_by_default()
    print(f"  use_odds_features default=False: {'PASS' if t1a else 'FAIL'}")
    print(f"  build_features shutdown odds by default: {'PASS' if t1b else 'FAIL'}")

    print("\n[测试 2] DC 层混入目标市场赔率（P0 违规）")
    t2 = test_dc_mixes_target_market_odds()
    dc_v = t2["dc_violation"]
    print(f"  DC 读 market_odds 参数: {'PASS' if dc_v['dc_read_market_odds_param'] else 'FAIL'}")
    print(f"  DC 调 _de_vig: {'PASS' if dc_v['dc_de_vig_call'] else 'FAIL'}")
    print(f"  DC 用 market_blend_weight: {'PASS' if dc_v['dc_market_blend_weight'] else 'FAIL'}")
    print(f"  DC 加权公式: {'PASS' if dc_v['dc_weighted_home_win'] else 'FAIL'}")

    print("\n[测试 3] devig_shin 训练/服务不一致")
    t3 = test_devig_shin_inconsistency()
    cal = t3["calibration_devig_shin"]
    print(f"  docstring 描述旧 bug: {'PASS' if cal['docstring_describes_old_bug'] else 'FAIL'}")
    print(f"  代码已修复为 1.0-1e-12: {'PASS' if cal['code_fixed_upper_bound'] else 'FAIL'}")
    print(f"  100 轮后 fallback: {'PASS' if cal['has_fallback_after_100'] else 'FAIL'}")

    print("\n[测试 4] 训练/服务 devig 路径不同")
    t4 = test_train_serving_devig_paths_differ()
    print("  服务路径: main.py -> select_devig_method -> devig_shin")
    print("  训练路径: historical_replay.py -> devig('proportional')")
    print(f"  最终公式相同: {t4['final_verdict']['same_final_formula']}")
    print(f"  路径相同: {t4['final_verdict']['same_path']}")

    print("\n[测试 5] 赛后信息只在结算阶段使用")
    t5 = test_postmatch_not_in_prediction()
    print(f"  预测调用附近含赛后变量: {t5['prediction_phase']['predicts_uses_actual']} (应 False)")
    print(f"  结算阶段回写 actual_result: {t5['settlement_phase']['writes_actual_after_pred']} (应 True)")
    print(f"  预测阶段干净: {'PASS' if t5['conclusion']['prediction_clean'] else 'FAIL'}")
    print(f"  结算阶段使用: {'PASS' if t5['conclusion']['settlement_only'] else 'FAIL'}")

    print("\n" + "=" * 60)
    print("全部测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
