"""已知问题注册表：把静态代码审计的发现变成机器可读的候选源。

【为什么需要这个文件】闭环若只会在配置网格里随机试参数，就永远发现不了
"DC 把比例因子当加法项"、"平局锚点把判平占比当真实平局率" 这类**语义缺陷**。
那些只能靠人读代码发现。本文件把审计结论结构化，闭环每轮自动把它们
转为候选、走同一套裁决流程、生成 PR —— 人工审计一次，之后由机器持续跟进。

【每条 issue 的字段】
  id            稳定标识，用于"已拒绝记忆"去重
  severity      blocking | high | medium | low
  layer         model | fusion | strategy | data | infra
  kind          config | code | data | process
  detect_by     该问题能被哪种通道发现（replay / backtest / invariant / audit）
  candidate     生成候选时用的 spec（见 proposer.issue_candidates）
  evidence      支撑该结论的实测数字（会被原样写进 PR 正文供复核）
  auto_mergeable  True 表示这是**事实性错误**，不依赖统计显著性即可修；
                 False 表示需通过 verifier 的统计门槛
  verify        自动验证方式

【设计原则】auto_mergeable=True 的条目绕过统计门槛，但仍要求 replay 忠实
与测试通过。理由：量纲错误不是"效果差一点"，而是"定义错了"，让数据裁决它
等于把正确性交给运气。
"""

from __future__ import annotations

KNOWN_ISSUES: list[dict] = [
    # ---------------------------------------------------- P0 模型层语义缺陷
    {
        "id": "DC-ATTACK-LOG-DIMENSION",
        "severity": "blocking",
        "layer": "model",
        "kind": "code",
        "title": "Dixon-Coles 将 attack/defense 比例因子直接相加，且防守项符号相反",
        "detail": (
            "team_ratings.json 中 attack/defense 是以 1.0 为中心的比例因子"
            "（实测中位数 0.84，范围 0.11-2.05）。dixon_coles._expected_goals "
            "把它们直接作为加法项写入 log 域：\n"
            "    - away.defense * cfg.defense_weight\n"
            "而 monte_carlo._expected_goals 对同一组字段用 math.log()，是正确的。\n"
            "两个后果：(1) 量纲错误——应加 log(0.84)≈-0.17，实际加了 +0.84；"
            "(2) 符号错误——defense<1 表示防守好，DC 却因此抬高对手 xG。"
        ),
        "detect_by": "patch",
        "invariant_id": "DI-ATTACK-LOG-DIMENSION",
        "candidate": {
            "type": "code_fix",
            "target": "engine/prediction/dixon_coles.py",
            "fix": "log_form",
            "patch_ref": "DC-ATTACK-LOG-DIMENSION",
            "replaces": {
                "home.attack * cfg.attack_weight": "math.log(max(0.3, home.attack)) * cfg.attack_weight",
                "- away.defense * cfg.defense_weight": "+ math.log(max(0.3, away.defense)) * cfg.defense_weight",
                "+ away.attack * cfg.attack_weight": "+ math.log(max(0.3, away.attack)) * cfg.attack_weight",
                "- home.defense * cfg.defense_weight": "+ math.log(max(0.3, home.defense)) * cfg.defense_weight",
            },
        },
        "evidence": {
            "xg_total_mean_dc": 3.22,
            "xg_total_mean_mc": 2.42,
            "real_euro_league_avg": 2.8,
            "dc_total_ge_4.5_frac": 0.094,
            "mc_total_ge_4.5_frac": 0.019,
            "backtest_brier_improvement": 0.0175,
            "backtest_cuts": ["2023-08-01", "2024-08-01", "2025-08-01"],
        },
        "auto_mergeable": True,
        "verify": "model_backtest.walk_forward_eval：log_form 变体需在 >=2 个时间切分上优于 buggy 变体",
        "side_effects": [
            "MC 的 xg_calibration=0.75 补丁是为 DC 的高估而存在的。"
            "修好 DC 后该补丁可能不再必要，需一并复核；"
            "若同时撤销，需重新校准（否则会双重下调）。"
        ],
    },
    {
        "id": "DEAD-FEATURES-INJURY-REST",
        "severity": "medium",
        "layer": "model",
        "kind": "config",
        "title": "injury 与 rest_days 恒为默认值，两个权重从未生效",
        "detail": (
            "对全部 1374 支球队统计：injury 全部为 0.0，rest_days 全部为 3"
            "（TeamRating 默认值）。因此 dixon_coles 中的 injury_weight 与 rest_weight "
            "从未对任何一场比赛产生过影响 —— DC 声明的 8 个特征实际只有 5 个生效。"
        ),
        "detect_by": "invariant",
        "invariant_id": "DI-DEAD-FEATURES",
        "candidate": {
            "type": "config",
            "changes": {"prediction.injury_weight": 0.0, "prediction.rest_weight": 0.0},
            "note": "参数归零以诚实反映实际生效特征；或改为真正接入数据源",
        },
        "evidence": {
            "teams_checked": 1374,
            "injury_nonzero": 0,
            "rest_days_nondefault": 0,
            "effective_features_of_8": 5,
        },
        "auto_mergeable": True,
        "verify": "invariant：ratings 表中 injury/rest_days 的方差为 0 时，相关权重应显式为 0",
        "side_effects": [
            "更根本的修法是真正接入伤停与赛程密度数据，而非把权重归零。"
            "但那属于新增数据源，不应与本次修正混在一个 PR。"
        ],
    },
    # ---------------------------------------------------- P0 融合层语义缺陷
    {
        "id": "DRAW-ANCHOR-JUDGMENT-AS-RATE",
        "severity": "blocking",
        "layer": "fusion",
        "kind": "code",
        "title": "draw_baseline 存的是判平占比，却被当作真实平局率使用，且自我强化",
        "detail": (
            "league_params.json 的字段注释写「该联赛实际平局率」，"
            "但其值来自 record_draw_result() 累加的 draw_predictions/draw_hits，"
            "即**系统判平的场次占比**，而非实际平局率。\n"
            "融合步骤 7 用 target_d = draw_baseline × draw_strength 把平局概率"
            "抬到这个值。巴甲 draw_baseline=0.60，但巴甲真实平局率是 0.316。\n"
            "自我强化机制：判平越多 → draw_baseline 越高 → 抬得越高 → 判得越多。"
            "结果：巴甲每场平局概率被钉死在 0.5025，与该场实际情况无关。"
        ),
        "detect_by": "audit",
        "candidate": {
            "type": "code_fix",
            "target": "engine/prediction/fusion.py",
            "fix": "gate_draw_baseline_on_sample_size",
            "detail": "把 step7 改为用真实平局率（来自 league_matrix 或历史赛果），"
                      "并要求样本量 n>=100、且与市场平局概率的偏离超过阈值才触发",
        },
        "evidence": {
            "league": "巴甲",
            "draw_baseline_stored": 0.60,
            "actual_draw_rate": 0.316,
            "pinned_final_draw": 0.5025,
            "final_hit": 0.316,
            "market_hit": 0.632,
            "final_brier": 0.6004,
            "market_brier": 0.4918,
            "brier_gap": 0.1086,
            "rows_affected": 19,
        },
        "auto_mergeable": True,
        "verify": "invariant + audit：融合输出中不得出现与输入无关的常数平局概率；"
                  "触发的联赛必须有 n>=100 的真实平局率样本",
        "side_effects": [
            "修复后巴甲 draw_baseline 应改为真实平局率（约 0.25-0.32 区间，"
            "随赛季更新），而非 0.60。",
            "美职联同样受影响：锚定 0.55 vs 实际 0.234，误差 -0.316。",
        ],
    },
    {
        "id": "LEAGUE-DRAW-ANCHOR-HARDCODED",
        "severity": "high",
        "layer": "fusion",
        "kind": "data",
        "title": "LEAGUE_DRAW_ANCHOR 硬编码且含事实错误",
        "detail": (
            "fusion.py 的 LEAGUE_DRAW_ANCHOR 表为硬编码常量：\n"
            "    美职联 0.55 / 葡超 0.50 / 巴甲 0.46 / 芬超 0.30\n"
            "对照 league_matrix.json 的真实平局率：\n"
            "    美职联 实际 0.234（n=269）→ 误差 -0.316\n"
            "    葡超   实际 0.556（n=9）  → 样本量过小，不可信\n"
            "    巴甲/芬超 league_matrix 中不存在\n"
            "该表以 w=0.3 混入，意味着每场 MLS 平局概率被人为抬高约 0.095。"
            "其消融判定为 full 段 t=-1.93「留」，但 val 段 t=+0.86（已变差）——"
            "典型的多重比较假阳性（在 10 个开关上用同一份数据既选又验）。"
        ),
        "detect_by": "invariant",
        "invariant_id": "DI-DRAW-BASELINE-SEMANTICS",
        "candidate": {
            "type": "config",
            "changes": {"fusion.post_fusion.league_draw_anchor": False},
            "note": "改为从 league_matrix 动态读取 draw_pct/100，并要求 n>=100",
        },
        "detect_by": "invariant",
        "invariant_id": "DI-DRAW-BASELINE-SEMANTICS",
        "candidate": {
            "type": "config",
            "changes": {"fusion.post_fusion.league_draw_anchor": False},
            "note": "改为从 league_matrix 动态读取 draw_pct/100，并要求 n>=100",
        },
        "evidence": {
            "美职联_anchor": 0.55, "美职联_actual": 0.234, "美职联_n": 269,
            "葡超_anchor": 0.50, "葡超_actual": 0.556, "葡超_n": 9,
            "full_t": -1.93, "val_t": 0.86,
        },
        "auto_mergeable": False,
        "verify": "verifier 统计门槛；且需确认动态读取方案本身不劣化",
    },
    {
        "id": "COMBO-BOOST-ENTROPY-REDUCTOR",
        "severity": "medium",
        "layer": "fusion",
        "kind": "code",
        "title": "combo_boost 无条件给当前最大项加分，是熵减器而非信号",
        "detail": (
            "fusion.py 步骤 3：\n"
            "    best = max([('H',h),('D',d),('A',a)], key=lambda x: x[1])\n"
            "    amt = min(combo_boost, cfg['combo_boost_cap'])  # cap=0.03\n"
            "它把概率加给**已经是最大值**的方向，不引入任何新信息，只把分布往峰值挤。\n"
            "实测 2026-10-09 的 12 场全部触发（注入量 0.05-0.275，cap 后统一 +0.03）。\n"
            "后果是校准曲线非单调扭曲：0.4 档偏差 -0.054，0.7 档偏差 -0.076（反向最大）。"
            "消融判定 t=-0.97（不显著）、verdict「留」—— 但它的机制本身就不是信号。"
        ),
        "detect_by": "invariant",
        "invariant_id": "DI-COMBO-BOOST-ENTROPY",
        "candidate": {
            "type": "config",
            "changes": {"fusion.post_fusion.combo_boost": False},
        },
        "evidence": {
            "fire_rate": 1.0,
            "matches_observed": 12,
            "cap": 0.03,
            "raw_inject_range": [0.05, 0.275],
            "ablation_t": -0.97,
            "calibration_distortion": {"band_0.4": -0.054, "band_0.7": -0.076},
        },
        "auto_mergeable": False,
        "verify": "verifier 统计门槛；另加 invariant：贡献恒为 0 的步骤应关闭",
    },
    {
        "id": "SYNTHETIC-ODDS-NO-MARKET-ANCHOR",
        "severity": "high",
        "layer": "fusion",
        "kind": "code",
        "title": "合成赔率场次丢失市场锚，完全裸奔",
        "detail": (
            "当数据源缺失真实赔率时，main.py 标记 odds_synthetic=True 并合成赔率，"
            "但 market_fair 置 None，导致 fusion 走 fuse_model_only 分支"
            "（trace 仅 base_model → combo_boost，完全没有市场信息参与）。\n"
            "实测近 12 天 116 场中有 8 场如此（6.9%）。"
            "这些场次的概率完全来自高估 3.22 球的裸 DC（见 DC-ATTACK-LOG-DIMENSION）。"
        ),
        "detect_by": "invariant",
        "invariant_id": "SYNTHETIC-ODDS-NO-MARKET",
        "candidate": {
            "type": "code_fix",
            "target": "engine/prediction/fusion.py",
            "fix": "derive_market_from_synthetic_odds",
            "detail": "让 market_fair 从合成的 home/draw/away_odds 去水推导，"
                      "而不是置 None；并在 ledger 中标记 market_is_synthetic",
        },
        "evidence": {
            "matches_12d": 116, "synthetic_odds": 8, "synthetic_no_market_fair": 8,
            "rate": 0.069,
        },
        "auto_mergeable": True,
        "verify": "invariant：odds_synthetic=True 的场次，trace 必须包含 fuse_two_way 或 fuse_three_way",
    },
    {
        "id": "MODEL-REPLAY-UNREPRODUCIBLE",
        "severity": "high",
        "layer": "infra",
        "kind": "data",
        "title": "账本未落盘 per-match 模型输入，导致模型层无法离线复现",
        "detail": (
            "review_ledger.jsonl 只落盘融合链的输入输出（model_raw / market_fair / final_prob），"
            "不含 attack/defense/form/elo。而 team_ratings.json 里的 attack/defense/form "
            "被 elo_updater 持续就地改写，是**当前值**而非预测时的历史值。\n"
            "后果：用当前值重放历史场次，实测忠实率仅 40.5%（corr 0.28 DC / 0.58 MC）。\n"
            "这直接导致 ablation_replay 只能裁决融合层开关，模型层缺陷至今没有自动化裁决通道。"
        ),
        "detect_by": "invariant",
        "candidate": {
            "type": "code_fix",
            "target": "engine/main.py",
            "fix": "persist_model_inputs",
            "detail": "在 predictions.json 落盘 per-match 的 "
                      "attack_home/defense_home/form_home/attack_away/defense_away/form_away/"
                      "injury/rest_days，供离线重放",
        },
        "evidence": {
            "model_replay_fidelity": 0.405,
            "corr_dc": 0.28,
            "corr_mc": 0.58,
            "join_rate_match_id": 1.0,
            "target_fidelity": 0.95,
        },
        "auto_mergeable": True,
        "verify": "invariant：补齐落盘后 model_replay_fidelity 应从 0.405 升至 >=0.95",
        "side_effects": ["这是解锁模型层自动裁决的前置条件，优先级应高于任何模型改动"],
    },
    # ---------------------------------------------------- P1 决策时点
    {
        "id": "PREDICT-TIMING-TOO-EARLY",
        "severity": "high",
        "layer": "process",
        "kind": "process",
        "title": "决策时点过早，未利用收盘线",
        "detail": (
            "项目 horizon 表显示「开赛前90分钟」档 total EV = -122.51u，"
            "而「收盘市场」档 Brier 0.5501、命中率 69.8%。\n"
            "独立验证（football-data 7904 场，开盘/收盘均价）：\n"
            "    开盘 Brier 0.5670 命中率 54.48%\n"
            "    收盘 Brier 0.5646 命中率 55.04%\n"
            "即单纯把决策推迟到临场，白拿 +0.0024 Brier / +0.56pp 命中率。"
        ),
        "detect_by": "backtest",
        "candidate": {
            "type": "config",
            "changes": {"pipeline.decision_time": "T-30~60min"},
            "note": "主流程从赛前一天挪到赛前 30-60 分钟；赛前一天只做盘口/阵容抓取",
        },
        "evidence": {
            "open_brier": 0.5670, "close_brier": 0.5646,
            "open_hit": 0.5448, "close_hit": 0.5504,
            "timing_gain_brier": 0.0024,
            "horizon_90min_ev": -122.51,
        },
        "auto_mergeable": False,
        "verify": "需工程改造 + 至少一个完整比赛周期的实盘验证，不能靠历史回放裁决",
    },
    # ---------------------------------------------------- P1 目标函数
    {
        "id": "NORTH-STAR-METRIC-HITRATE",
        "severity": "high",
        "layer": "strategy",
        "kind": "process",
        "title": "北极星指标是命中率而非 ROI，导致在负 EV 空间里做更精确的估计",
        "detail": (
            "账本实盘：34 注已结算，pnl 合计 -1129.21，ROI -100%。\n"
            "EV 校准体检显示没有任何 edge 档位为正：\n"
            "    edge [0.00,0.02): +9%  (n=6)\n"
            "    edge [0.02,0.05): -45% (n=15)\n"
            "    edge [0.05,0.10): -3%  (n=19)\n"
            "    edge [0.10,+inf): -20% (n=66)\n"
            "串关同样踩在随机基线上：2串1 实际 24%（基线 25%），3串1 实际 12%（基线 12.5%），"
            "同时吃两次抽水（实测 8 联赛平均 overround 5.36%）。\n"
            "把命中率当目标，会激励系统在负 EV 空间里提高精确度 —— 亏损更高效。"
        ),
        "detect_by": "audit",
        "candidate": {
            "type": "process",
            "fix": "switch_north_star_to_ev",
            "detail": "主指标改为分层 EV/ROI；单场用 LogLoss/RPS（已有 rps_final 字段）；"
                      "串关改用每注期望值",
        },
        "evidence": {
            "settled_bets": 34, "pnl": -1129.21,
            "edge_buckets": {"[0,0.02)": 0.09, "[0.02,0.05)": -0.45,
                             "[0.05,0.10)": -0.03, "[0.10,inf)": -0.20},
            "parlay_2_hit": 0.24, "parlay_2_baseline": 0.25,
            "parlay_3_hit": 0.12, "parlay_3_baseline": 0.125,
            "avg_overround": 0.0536,
        },
        "auto_mergeable": False,
        "verify": "无法用历史回放验证（需前瞻实盘）。属决策层变更，由人工判断。",
    },
    # ---------------------------------------------------- P2 数据覆盖
    {
        "id": "LEAGUE-COVERAGE-GAP",
        "severity": "medium",
        "layer": "data",
        "kind": "data",
        "title": "历史数据覆盖 19 项赛事，仪表盘投注 35 个联赛",
        "detail": (
            "matches.csv 覆盖 19 项赛事（五大联赛 + 巴甲/阿甲 + 欧战）。\n"
            "而仪表盘实际投注 35 个联赛。含完整历史（含赔率）的仅 5 大联赛 + 巴甲。\n"
            "日职、K联、MLS、沙特、墨西哥、瑞典、挪超、苏超等球队在 team_ratings.json 里"
            "大量是 Elo=1500 的空壳，只能靠 elo_updater 从上线起慢慢积累。\n"
            "这解释了 shrinkage_dc challenger 为何跑出 Brier 0.7126 —— "
            "它被拿去评估一个它根本没见过的联赛组合，是评估集错配，不是模型缺陷。\n"
            "另：football_data 中 BRA/JPN/USA/FIN/NOR/RUS/SWE 七个文件"
            "只有 25 列（全是收盘赔率，无开盘赔率、无射门数据），未并入 matches.csv。"
        ),
        "detect_by": "audit",
        "candidate": {
            "type": "data",
            "fix": "expand_historical_coverage",
            "detail": "把 *_new.csv 的 25 列收盘赔率并入历史库；"
                      "为缺失联赛建立分级先验（用相邻联赛参数做跨联赛映射）",
        },
        "evidence": {
            "matches_csv_comps": 19,
            "dashboard_leagues": 35,
            "full_history_comps": 6,
            "challenger_brier": 0.7126,
            "files_25col_only": 7,
        },
        "auto_mergeable": False,
        "verify": "需数据工程，非单次实验可裁决",
    },
    # ---------------------------------------------------- P2 统计基建
    {
        "id": "ABLATION-SELECTION-BIAS",
        "severity": "high",
        "layer": "infra",
        "kind": "process",
        "title": "消融用同一份数据既选又验，多重比较产生假阳性",
        "detail": (
            "ablation_replay.py 在 full 样本上选开关、在 val 样本上验证。"
            "在 10 个开关上做 best-of-10，val 段必然出现假阳性。\n"
            "实证：league_draw_anchor 的 full 段 t=-1.93 判「留」，"
            "但 val 段 t=+0.86（已变差）仍被保留。\n"
            "combo_boost 同样：t=-0.97 不显著却 verdict「留」—— "
            "判定规则是「不显著就留」，这等于把噪声当默认值固化。"
        ),
        "detect_by": "audit",
        "candidate": {
            "type": "process",
            "fix": "walk_forward_selection_with_fdr",
            "detail": "改为 walk-forward 选择（每个决策日只用该日之前的数据选开关）；"
                      "加 Benjamini-Hochberg FDR 校正；"
                      "判定改为「留」需要 |Δ| > min_effect 且 p < alpha",
        },
        "evidence": {
            "n_switches": 10,
            "league_draw_anchor_full_t": -1.93,
            "league_draw_anchor_val_t": 0.86,
            "combo_boost_t": -0.97,
        },
        "auto_mergeable": False,
        "verify": "本闭环的 verifier 已实现 walk-forward + BH-FDR + min_effect，"
                  "可直接作为上游 ablation_replay 的参考实现",
    },
    {
        "id": "LEDGER-MULTI-CHAIN-CONTAMINATION",
        "severity": "high",
        "layer": "infra",
        "kind": "data",
        "title": "账本跨多代融合链混放，重放旧链行会系统性误判",
        "detail": (
            "review_ledger.jsonl 横跨融合链重构（chain=v1 425 场 / chain=v2 517 场）。"
            "用今天的代码重放旧链的行，字段语义不同：\n"
            "    混放两链 replay vs 生产 final 平均平方偏差 = 0.018（Brier 量级的 6%）\n"
            "    单独重放当前链 = 0.0001\n"
            "若不限定链，任何基于重放的裁决都带着 6% 的系统偏差。"
        ),
        "detect_by": "invariant",
        "invariant_id": "DI-LEDGER-MULTI-CHAIN",
        "candidate": {
            "type": "code_fix",
            "target": "scripts/ablation_replay.py",
            "fix": "scope_by_chain",
            "detail": "重放前按 chain 字段过滤；chain 字段缺失的旧行需迁移或标记废弃",
        },
        "evidence": {
            "v1_rows": 425, "v2_rows": 517,
            "fidelity_mixed": 0.018, "fidelity_v2_only": 0.0001,
        },
        "auto_mergeable": True,
        "verify": "invariant：replay 忠实率需达标（本闭环自检已实现，阈值 0.95）",
    },
    {
        "id": "PREDICTION-TIMING-BUCKET-NO-ASOF",
        "severity": "low",
        "layer": "infra",
        "kind": "data",
        "title": "预测时点分桶无有效样本（历史账本缺 as_of）",
        "detail": (
            "【2026-10-09 已核实为已修复的过时条目，保留作历史留痕】"
            "复核发现：review_ledger.jsonl 的 v2 链 517 行 as_of 全部非空（517/517），"
            "post_match.py 自 2026-08-16 起就把 predictions.json 的 as_of/kickoff "
            "同步落盘（MatchReview 第 497-498 行）。本条 issue 的 root cause 已经消失。"
            "真正的时点分桶空洞（开赛前 24h 桶为 0）不是数据缺失，而是 PREDICT-TIMING-TOO-EARLY "
            "决策时点过早（as_of 全在 T-24h 之前）——属 process 类，见该条目。"
        ),
        "detect_by": "audit",
        "candidate": {
            "type": "process",
            "fix": "already_fixed_no_action",
            "detail": "无需动作。时点分桶空洞由 PREDICT-TIMING-TOO-EARLY 覆盖。",
        },
        "evidence": {
            "v2_as_of_nonempty": "517/517", "as_of_since": "2026-08-16",
            "real_cause": "decision_time_too_early",
        },
        "auto_mergeable": False,
        "verify": "审计：本条目已核实为历史遗留，不再自动提候选",
    },
]


def by_id(issue_id: str) -> dict | None:
    for it in KNOWN_ISSUES:
        if it["id"] == issue_id:
            return it
    return None


def by_layer(layer: str) -> list[dict]:
    return [i for i in KNOWN_ISSUES if i["layer"] == layer]


def severity_rank(s: str) -> int:
    return {"blocking": 0, "high": 1, "medium": 2, "low": 3}.get(s, 9)


def summary() -> dict:
    out = {}
    for i in KNOWN_ISSUES:
        out[i["severity"]] = out.get(i["severity"], 0) + 1
    return {
        "total": len(KNOWN_ISSUES),
        "by_severity": out,
        "by_layer": {l: len(by_layer(l)) for l in
                     ("model", "fusion", "strategy", "data", "infra", "process")},
        "auto_mergeable": sum(1 for i in KNOWN_ISSUES if i.get("auto_mergeable")),
    }
