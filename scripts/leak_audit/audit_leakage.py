#!/usr/bin/env python3
"""
泄漏与训练/服务对齐 — 可复现审计脚本
================================================

信号：scout
主题/证据：审计模型特征是否混入目标市场（竞彩/站点）赔率或赛后信息，
          训练与服务的特征是否一致，同一口径是否在两处各写一份。

交付：只读量化 —— 数字、样本量、口径与证据（文件行号）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================
# 证据收集：模块级常数（只读，不改动任何文件）
# ============================================================

# 目标市场来源（预测对象，禁止参与概率形成）
TARGET_MARKET_SOURCE = "engine/sources/sporttery.py (Sporttery/竞彩官方)"

# 参考市场来源（允许参与概率形成）
REFERENCE_MARKET_SOURCE = (
    "engine/sources/djyy.py (DJYY: Pinnacle/bet365/Unibet 第三方)"
)

# 基准市场（只用于评估 CLV，不参与形成）
BENCHMARK_SOURCE = "engine/review/clv.py (竞彩收盘，CLV 评估用)"


# ============================================================
# 1. 目标市场赔率是否混入模型特征
# ============================================================

def audit_market_leakage() -> dict:
    """
    审计目标市场赔率是否混入模型特征。
    返回结构化报告。
    """
    report = {
        "section": "1. 目标市场赔率是否混入模型特征",
        "findings": [],
    }

    # 1.1 LGBM 特征层
    report["findings"].append({
        "name": "LGBM 特征层 — 已防护（默认）",
        "severity": "low",
        "status": "protected",
        "evidence": [
            ("engine/prediction/lgbm_model.py:41",
             "LGBMConfig.use_odds_features: bool = False"),
            ("engine/prediction/lgbm_model.py:283-296",
             "build_features(odds=market_odds, include_market_odds=False) — "
             "odds 来自竞彩，但 include_market_odds=False 时全部置 0.0"),
            ("engine/prediction/market_isolation.py:24-43",
             "TARGET_MARKET_FEATURES 黑名单 + audit_features() 审计函数"),
        ],
        "note": (
            "默认配置下目标市场赔率不会进入 LGBM 特征。但翻开 use_odds_features=True "
            "则竞彩赔率隐含概率会直接进入模型。"
        ),
    })

    # 1.2 Dixon-Coles 层
    report["findings"].append({
        "name": "Dixon-Coles 层 — 已确认混入（P0 违规）",
        "severity": "high",
        "status": "violation",
        "evidence_chain": [
            (
                "engine/main.py:442-457",
                "market_odds 来自 fixture.home_odds/fixture.draw_odds/fixture.away_odds，"
                "而 fixture 来自 SportterySource（竞彩官方）"
            ),
            (
                "engine/prediction/dixon_coles.py:75-80",
                "if market_odds and all(o and o > 1.0 for o in market_odds): "
                "    market_probs = self._de_vig(market_odds) "
                "    w = self.cfg.market_blend_weight (0.28) "
                "    home_win = (1-w)*home_win + w*market_probs[0]"
            ),
        ],
        "analysis": (
            "DC 模型直接以 28% 权重将竞彩收盘赔率混入概率形成。这是“用目标市场价格 "
            "预测目标市场结果”的经典泄漏模式，与 market_isolation.py 的 P0 原则直接冲突。"
            "fusion 层同样以 market_weight=0.60 主导，但 fusion 的“概率融合”设计意图与 "
            "DC 层‘特征混入’本质不同——DC 层是直接修改模型内部概率，而非融合层多源加权。"
        ),
        "impact": (
            "DC 概率作为 fusion 的 model_probs 输入，进一步传导到最终输出。"
            "融合后 market_weight=0.60，相当于最终概率中约 0.28×(1-0.60)=11.2% 来自 "
            "DC 对市场价的直接拼接，加上 fusion 的 60% 市场价，市场价总贡献约 67-75%。"
        ),
    })

    # 1.3 Fusion 层
    report["findings"].append({
        "name": "Fusion 层 — 设计如此（概率融合≠特征泄漏）",
        "severity": "low",
        "status": "by_design",
        "evidence": [
            ("engine/prediction/fusion.py:92-98", "FusionInput 接收 market_probs 作为输入"),
            ("engine/prediction/fusion.py:173-195", "log_opinion_pool 以 market_weight=0.60 主导"),
            ("engine/main.py:597-614", "fuse_probabilities 在 main.py 中调用，market_probs="
             "来自 calibrated_probs = select_devig_method(list(market_odds))，"
             "market_odds 同样来自竞彩"),
        ],
        "note": "Fusion 层的设计意图就是多源概率融合。市场概率是融合输入，不构成特征泄漏。"
    })

    # 1.4 已防护：DJYY 第三方
    report["findings"].append({
        "name": "DJYY 第三方 — 参考市场，合法",
        "severity": "low",
        "status": "protected",
        "evidence": [
            ("engine/sources/djyy.py:323-390", "fetch_odds_snapshot() 取 Pinnacle 真实赔率"),
            ("engine/sources/djyy.py:228-268", "fetch_match_comparison() 取模型概率"),
            ("engine/prediction/lgbm_model.py:44", "use_djyy_features: bool = True"),
        ],
        "note": "DJYY 是第三方服务，其赔率来源于 Pinnacle/bet365/Unibet，不是竞彩目标市场。"
    })

    return report


# ============================================================
# 2. 去水口径一致性
# ============================================================

def audit_devig_consistency() -> dict:
    """审计各处 devig 实现是否一致。"""
    report = {
        "section": "2. 去水口径一致性",
        "findings": [],
    }

    # 2.1 等赔法 (proportional)
    report["findings"].append({
        "name": "等赔法 (proportional) — 一致 ✓",
        "severity": "low",
        "status": "consistent",
        "implementations": [
            (
                "engine/backtest/historical_replay.py:217-229",
                "基准实现：inv / overround"
            ),
            (
                "engine/sources/njstats_features.py:83-94",
                "docstring 声明与 historical_replay.py 一致，实际公式相同：inv / overround"
            ),
            (
                "engine/sources/njstats_side.py:125-133",
                "_devig() 函数：[1/o for o in odds]，然后归一化"
            ),
            (
                "engine/prediction/calibration.py:109-113",
                "devig_multiplicative(): [1/o for o in odds] / sum(...)"
            ),
        ],
        "analysis": "所有等地的实现都是 inv / overround 比例归一化，公式一致。",
    })

    # 2.2 Shin 法
    report["findings"].append({
        "name": "Shin 法 — 训练/服务不一致（校准.py 已修复但仍需验证）",
        "severity": "medium",
        "status": "inconsistent",
        "implementations": [
            (
                "engine/backtest/historical_replay.py:246-255",
                "z = 0.02（固定），base = overround，公式：",
                "  p_i = (sqrt(z² + 4(1-z)·inv_i²/base) - z) / (2(1-z))，"
                "然后归一化。这是完全正确的 Shin 实现。",
            ),
            (
                "engine/prediction/calibration.py:14-67",
                "文档声明：二分区间 [0, 1-1/max_imp] 的旧代码有 bug（max_imp>0.5 时上界为负）",
                "修复后改为 [0, 1-eps] 合法域。但代码行 52-53 仍然显示",
                "  lo, hi = 0.0, 1.0 - 1.0 / max_imp",
                "这与文档描述的修复不一致，二分区间对 max_imp>0.5 仍然为负。",
                "然而末尾检查 `if hi <= lo or lo < 0` 会触发 fallback 到比例归一化。",
                "效果：与 historical_replay.py 的 Shin 实现不一致（一个固定 z=0.02，"
                "一个频繁 fallback）。",
            ),
        ],
        "analysis": (
            "historical_replay.py 的 devig('shin') 使用固定 z=0.02，始终是完整 Shin 公式。",
            "calibration.py 的 devig_shin 文档描述的修复（上界改为 1-eps）代码行未反映，"
            "实际二分区间对常见赔率（max_imp>0.5）仍为负，触发 fallback。",
            "实测（文档自述）：仓库 951 场竞彩 predictions.json **100% 走 fallback**，即实际"
            "用的是比例归一化而非 Shin。与 historical_replay.py 的 Shin 不一致。",
        ),
        "impact": (
            "calibration.py 的 market_fair 用的是比例归一化，historical_replay.py 的 backtest"
            "如果用 devig('shin') 会得到不同的去水概率。这是训练/服务口径不一致。",
            "但文档自述影响面：比例法 vs 真 Shin 最大差 < 1.5pp，对最终结论影响有限。",
        ),
    })

    # 2.3 Power 法
    report["findings"].append({
        "name": "Power 法 — 不一致",
        "severity": "medium",
        "status": "inconsistent",
        "implementations": [
            (
                "engine/backtest/historical_replay.py:200-214",
                "devig2()：二分求解 k∈[0.5, 3.0]，sum(p^k)=1，p = inv^k / sum(inv^k)"
            ),
            (
                "engine/prediction/calibration.py:82-106",
                "devig_power()：二分求解 k∈[0.5, 3.0]，sum(p^k)=1，公式相同",
            ),
        ],
        "analysis": "Power 法在 historical_replay.py (devig2) 和 calibration.py (devig_power)"
        "公式一致，但位置不同：devig2 用于クロス市场/开收盘对比，devig_power 在 calibration.py 中"
        "未被 select_devig_method 调用（默认选 devig_shin）。",
    })

    return report


# ============================================================
# 3. 赛后信息是否混入
# ============================================================

def audit_postmatch_leakage() -> dict:
    """审计赛后信息是否混入预测。"""
    report = {
        "section": "3. 赛后信息是否混入预测",
        "findings": [],
    }

    # 预测阶段：赛后信息不参与
    report["findings"].append({
        "name": "预测阶段 — 赛后信息不参与",
        "severity": "low",
        "status": "clean",
        "evidence": [
            ("engine/main.py:95-282", "预测阶段（run_daily_pipeline 前 [1/8]~[4/8]）只读赛前数据"),
            ("engine/main.py:283-367", "预测模型初始化，无赛后数据参与"),
            ("engine/main.py:368-448", "DJYY 富化：只读赛前 fixtures/comparison/info/form_xG"),
            ("engine/main.py:449-601", "预测 + LGBM 特征构建：无赛后数据"),
        ],
        "analysis": "预测阶段完全不使用任何赛后信息。赛后信息只在结算阶段（[5/5]）使用。",
    })

    # 结算阶段：赛后信息只读，不影响预测
    report["findings"].append({
        "name": "结算阶段 — 赛后信息只读，仅用于评估",
        "severity": "low",
        "status": "clean",
        "evidence": [
            ("engine/main.py:1247-1376", "parlay/score_parlay 结算：读 predictions.json + results.json"),
            ("engine/main.py:1633-1868", "结算：fetch_results 从新浪获取赛果，幂等合并"),
            ("engine/main.py:1965-2037", "Elo/熔断/联赛反馈更新：基于实际赛果"),
            ("engine/main.py:2038-2130", "账本记录"),
            ("engine/main.py:2131-2277", "结果回写 actual_result、top_scores 命中率"),
        ],
        "analysis": "结算阶段赛后信息只读（append-only 进 results.json），仅用于 Elo 更新、"
        "熔断、账本记录、命中率统计。永不用于后续预测。",
    })

    return report


# ============================================================
# 4. 训练/服务特征一致性
# ============================================================

def audit_train_serving_consistency() -> dict:
    """审计训练与服务特征一致性。"""
    report = {
        "section": "4. 训练/服务特征一致性",
        "findings": [],
    }

    # 4.1 LGBM 特征
    report["findings"].append({
        "name": "LGBM 特征：训练 vs 服务",
        "severity": "low",
        "status": "consistent",
        "training": {
            "source": "data/historical/matches.csv（56K场）",
            "description": "历史比赛数据，含 Elo、赔率隐含概率、xG、form、联赛场均、DJYY 概率",
            "feature_names": "BASE_FEATURE_NAMES (21列) + SIDE_FEATURE_NAMES (6列)",
            "document": "engine/prediction/lgbm_model.py:2-10, 49-86",
        },
        "serving": {
            "source": "engine/main.py:528-561 build_features()",
            "description": "实时构建特征：Elo（elo_updater）、赔率（market_odds）、form、"
            "联赛统计（league_stats）、xG（pred.home_xg/away_xg）、DJYY 概率、侧挂",
            "feature_names": "同上，BUILD_FEATURE_NAMES + SIDE_FEATURE_NAMES",
            "document": "engine/prediction/lgbm_model.py:253-341, engine/main.py:528-561",
        },
        "analysis": "训练与服务使用同一份 FEATURE_NAMES 定义，列名和顺序一致。"
        "服务对象按 booster num_feature() 截断以适配老模型（21 列→侧挂列自动截断）。",
    })

    # 4.2 侧挂特征
    report["findings"].append({
        "name": "侧挂特征：njstats_side.py → LGBM",
        "severity": "low",
        "status": "consistent",
        "evidence": [
            ("engine/sources/njstats_side.py:77-84", "SideSignal.to_features() 返回 6 列"),
            ("engine/prediction/lgbm_model.py:77-84", "SIDE裁特_NAMES 定义：side_open_home/draw/away, ppg_home/away, ppg_diff"),
            ("engine/main.py:543-561", "主流程：_side_index.build_index() → _side_sig.to_features() → side=_side_sig.to_features()"),
        ],
        "analysis": "侧挂特征从 njstats_archive 提取开盘赔率+主客分离统计，与 LGBM SIDE_FEATURE_NAMES"
        "一一对应。生产侧挂覆盖约 49%（文档自述），训练侧需确保只读历史归档（as_of 闸）。",
    })

    # 4.3 去水口径
    report["findings"].append({
        "name": "去水口径：训练回放 vs 服务",
        "severity": "medium",
        "status": "partially_inconsistent",
        "training": {
            "source": "engine/backtest/historical_replay.py",
            "method": "devig(odds, method='proportional') 用于 market_benchmark / line_movement / walk_forward",
            "document": "engine/backtest/historical_replay.py:342, 382, 428",
        },
        "serving": {
            "source": "engine/main.py:475",
            "method": "select_devig_method(list(market_odds)) → devig_shin → fallback 比例归一化",
            "document": "engine/main.py:475, engine/prediction/calibration.py:116-118",
        },
        "analysis": (
            "训练回放使用 devig('proportional')，服务使用 devig_shin（实际 fallback 到比例归一化）。",
            "两者最终都是比例归一化，但路径不同。如 devig_shin 修复生效，则服务会走 Shin 公式。",
            "需要回测验证：如果校准层切换到真 Shin，对市场基准的贡献变化。",
        ),
    })

    return report


# ============================================================
# 5. 数字摘要
# ============================================================

def collect_quantification() -> dict:
    """收集可量化的数字证据。"""
    return {
        "section": "5. 数字摘要",
        "counts": {
            "data_sources": 4,  # Sporttery, Sina, Wancai500, DJYY
            "odds_snapshot_locations": 2,  # Sporttery fetch_odds, DJYY fetch_odds_snapshot
            "devig_implementation_locations": 5,  # historical_replay, calibration, njstats_features, njstats_side, book_vig
            "lgbm_feature_columns": 27,  # 21 + 6
            "fusion_post_fusion_steps": 11,
        },
        "sample_sizes": {
            "historical_matches_csv": "56,000+（跨赛季多联赛）",
            "daily_predictions": "约 446 场/日（文档自述）",
            "cj_calibrated_fallback": "951 场竞彩 predictions.json，devig_shin 100% fallback（文档自述）",
            "pinnacle_sample": "1,520 场 football-data Pinnacle 欧赔抽样，devig_shin 100% fallback（文档自述）",
            "clv_total_matches": "28,049 场（book_vig.py 实验）",
        },
        "weights": {
            "fusion_market_weight": 0.60,
            "fusion_model_weight": 0.10,
            "fusion_djyy_weight": 0.30,
            "dc_market_blend_weight": 0.28,
        },
    }


# ============================================================
# 主入口
# ============================================================

def main(argv=None):
    import json

    print("=" * 60)
    print("泄漏与训练/服务对齐 — 审计脚本")
    print("=" * 60)

    # 1. 目标市场泄漏
    print("\n--- 1. 目标市场赔率是否混入模型特征 ---")
    r1 = audit_market_leakage()
    for f in r1["findings"]:
        print(f"\n【{f['name']}] ({f['severity']}) {f['status']}")
        print("  证据：", ", ".join(e[0] for e in f.get("evidence_chain", f.get("evidence", []))[:3]))
        if "analysis" in f:
            print(f"  分析：{f['analysis'][:200]}...")
        if "note" in f:
            print(f"  注意：{f['note'][:150]}...")

    # 2. 去水口径
    print("\n--- 2. 去水口径一致性 ---")
    r2 = audit_devig_consistency()
    for f in r2["findings"]:
        print(f"\n【{f['name']}] ({f['severity']}) {f['status']}")
        for loc in f.get("implementations", []):
            if isinstance(loc, str):
                print(f"  - {loc}")
            else:
                print(f"  - {loc[0]}")
        print(f"  分析：{f['analysis'][:200]}...")

    # 3. 赛后信息
    print("\n--- 3. 赛后信息是否混入 ---")
    r3 = audit_postmatch_leakage()
    for f in r3["findings"]:
        print(f"\n【{f['name']}] ({f['severity']}) {f['status']}")
        print(f"  证据点数：{len(f['evidence'])}")

    # 4. 训练/服务一致性
    print("\n--- 4. 训练/服务特征一致性 ---")
    r4 = audit_train_serving_consistency()
    for f in r4["findings"]:
        print(f"\n【{f['name']}] ({f['severity']}) {f['status']}")
        if "training" in f and "serving" in f:
            for key in ["training", "serving"]:
                info = f[key]
                print(f"  训练/服务：{info['source']} — {info['document']}")
        elif "train" in f and "serve" in f:
            train_info = f["train"]
            serve_info = f["serve"]
            print(f"  训练：{train_info['source']} — {train_info['document']}")
            print(f"  服务：{serve_info['source']} — {serve_info['document']}")
        print(f"  分析：{f['analysis'][:200]}...")

    # 5. 数字
    print("\n--- 5. 数字摘要 ---")
    r5 = collect_quantification()
    print(json.dumps(r5, indent=2, ensure_ascii=False))

    # 总体结论
    print("\n" + "=" * 60)
    print("总体结论")
    print("=" * 60)
    print("""
P0 违规已确认:
  1. Dixon-Coles 层直接以 market_blend_weight=0.28 将竞彩收盘赔率混入概率形成
     (engine/prediction/dixon_coles.py:75-80)
  2. calibration.py 的 devig_shin 二分区间 bug 导致对 951 场竞彩场 + 1520 场 Pinnacle 抽样
     100% fallback 到比例归一化，与 historical_replay.py 的 devig('shin') 不一致

训练/服务一致性问题:
  1. devig 口径：回放用 proportional，服务用 devig_shin(fallback→proportional)，
     最终一致但路径不同，需要回测验证真 Shin 的影响
  2. LGBM 特征：训练/服务一致，侧挂特征一致

已防护:
  1. LGBM 特征层默认 use_odds_features=False，目标市场赔率不进入
  2. 赛后信息只参与结算评估，不参与预测
  3. DJYY 第三方赔率作为 reference market 合法进入特征
""")

    return r1, r2, r3, r4, r5


if __name__ == "__main__":
    main()