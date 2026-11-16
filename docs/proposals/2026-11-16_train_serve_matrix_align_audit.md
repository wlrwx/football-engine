# 训练矩阵 × 服务口径 对齐审计（只读量化）

- 审计基线：上游 `wlrwx/football-engine` main `c27fac156`；本 PR 已 rebase 到
  `9a0d28617`（一个 duty 快照提交），两条基线上重跑本审计，样本数（1511/1500）与
  15 条判定**逐条一致** → 下面的数字不依赖某一次快照的巧合。
  样本快照：`review_ledger.jsonl` **1511 行**（2026-07-19 ~ 2026-11-15，115 天）→
  生产函数重建出的 LGBM 训练矩阵 **1500 行 × 27 列**（11 行账本在 daily 里配不上）。
  落盘预测记录 1519 行。
- 审计方式：只读量化。不改生产代码、配置、状态文件；不改任何门槛/判据/上界/校准参数；
  不以 docstring、注释或文档措辞作证据，一律回到**取数代码 / 开关 / 消费方 / 落盘值**。
- 可复现检查：`scripts/train_serve_align_audit.py` + `tests/test_train_serve_align_audit.py`

  ```bash
  ./.venv/bin/python -m scripts.train_serve_align_audit            # 控制台判定
  ./.venv/bin/python -m scripts.train_serve_align_audit --json      # 机器可读报告
  ./.venv/bin/python -m pytest tests/test_train_serve_align_audit.py -q   # 29 passed
  ```

  15 条判定、退出码 1 = 存在 `violation`（本轮 6 条）。脚本不写任何文件。
- **断言口径**（沿用 `leak-audit-independent-review.md`）：测试断言**判定结论与判定规则**，
  不断言计数。本文所有数字都是本次快照，只出现在 `counts_for_humans` 里供人读；
  样本变多、代码挪行、名单加字段不会让 CI 变红，只有判定被推翻才红。
  **每条判定都做了变异注入验证**：把 `diff_columns`、`max_disagreement`、`probe_guards`、
  `scan_blend_consumers`、`paired_brier_shift`、`caliber_switch_days`、时点分桶、
  口径普查分类器、以及「booster 子进程失败必须降级成 skipped」这条降级路径逐个改坏，
  对应测试均转红（改坏普查分类器时，最初版本的测试**漏掉了**，
  于是把复算范围从"开头两天"改成"首尾各一天"才抓住 —— 见
  `test_anchor_caliber_switch_is_recomputable_from_the_census`）。
- **CI 平台差异（本 PR 第一轮 CI 实测，2026-11-16）**：`test (windows-latest)` 上
  `lgb.Booster(model_file=…)` 在**原生层** `Fatal Python error: Aborted`
  （`lightgbm/basic.py:3800`），Python 侧 `try/except` 抓不住，会把整个 pytest 会话打死
  （同一 head 的 `test (ubuntu-latest)` 全绿，所以只红 windows）。
  修法：booster 判定改到**子进程**里算（`python -m scripts.train_serve_align_audit
  --booster-json`），子进程崩只降级成 `skipped_*` 判定，其余 14 条照跑；
  模型文本走 `Booster(model_str=MODEL_FILE.read_text(...))` —— `read_text()` 的
  universal-newlines 已把 CRLF 归一成 LF，本地造 CRLF 副本验证过「27 特征 / 900 棵树」
  照常加载，所以 Windows 检出换行差异不再是理由。另加一条 AST 回归锁
  （`test_booster_is_loaded_in_a_subprocess_with_model_str`）禁止再出现
  `Booster(model_file=`，注释里提到不算违规（只看 Call 节点）。
- 与已有 `scripts/leak_audit_check.py`（PR #80）的分工：那份查**代码口径**
  （黑名单命中位置、开关默认值、两处调用签名差）；本份查**落到真实样本上的结果**
  （重建矩阵逐列量化、时点可验证性、输入可复现性、口径重复）。
  并把它留下的两条 `UNVERIFIED_NOTES`（`scripts/leak_audit_check.py:455-461`）核完：
  **DC 层 market_blend** → 见 #13；**devig 方法族之差** → 见 #11/#15。
- 与 `docs/proposals/dc-market-blend-and-devig-review.md` + `tests/test_dc_market_blend_review.py`
  （22 项，已合并）的关系：**不重开它已裁定的三件事**——
  (a) DC 层 `market_blend_weight` 活值 0.0 ⇒ 目标市场赔率不掺进模型腿（#13 只是独立复算 + 接线守卫）；
  (b) 「回放 `devig('proportional')` vs 服务 `devig_shin`」**不构成同对象漂移**（回放基准的对象是
  football-data.co.uk 参考盘收盘，不是竞彩）⇒ 本审计不做第二次该指控，#15 只量**同对象**
  （竞彩 HAD）上的口径差与落盘锚自身漂移；
  (c) `scripts/ablation_replay.py:155` 直接读冻结 `market_fair`、不重新去水 ⇒ 生产开关裁决无漂移。
  本审计对它**唯一的补充/收窄**在 #15：那句「直接用生产冻结的 **Shin** 去水值」只对
  2026-10-15 起的行成立——之前的冻结值实测是 proportional（1379 行里只有 447 行是 Shin），
  所以跨该日的配对结论混了两份锚。实测该混合在配对 Brier 上检不出（t=-0.43）
  ⇒ 既有结论不被推翻，只是措辞要加日期限定。

## 结论先行（三问三答）

1. **目标市场（竞彩/站点）赔率有没有混进模型特征？——实测量：没有。**
   4 列竞彩赔率派生特征在 1500/1500 行取值恒 0.0、现网 booster 对这 4 列分裂增益 0.0000%；
   站点价 `site_fair` 在 `to_features()` 输出里出现 0 次；结算回写字段（19 个键 /
   26529 个单元改成非法值）与目标市场价格字段（11 个键整列改 1.05）重建矩阵后
   **0 个特征单元**变化；DC 模型内部那步市场混合的活值
   `config/prediction.json["prediction"]["market_blend_weight"] = 0.0`。
2. **但"没混进来"目前是结果，不是保护。** 三处保护是虚的：
   `market_isolation` 的三张名单只覆盖 27 列中的 **4 列**（23 列静默，含 6 个 `side_*`
   和增益第一的 `handicap`）；侧挂唯一的防未来信息闸门**整体空转**
   （597 个真实 `as_of` 样本被生产 `_parse_ts` 解析成功 **0 个**）；
   `handicap` 把**竞彩让球线**与**Bet365 亚盘线**混在同一列且无来源标记。
3. **训练与服务一致吗？——构造函数同一份，输入不同一份，而且评卷锚中途换过口径。**
   训练侧回放落盘值，elo 落盘被 `round(,1)` → 1500/1500 行不可逐位复现
   （实测概率影响：20 轮 ±0.05 抖动 p95 |Δp|=0.00158、max 0.0917、argmax 翻转 0/30000）；
   `lgbm_inputs` 冻结了 form/先验/xG 却不含 elo，与 `engine/main.py:526`
   「训练侧原样回放，保证两端逐位同参」的注释不符。
   更需要注意的是**同一个口径在两处（实为多期）各写一份且值不同**：
   服务腿与落盘锚 `market_fair` 走 Shin 去水，DC 内混与 CLV/侧挂走等赔 proportional
   （同一份竞彩赔率被去水两次、口径不同；**不指控**「回放 vs 服务漂移」——回放基准的
   对象是参考盘收盘，那条已被 `dc-market-blend-and-devig-review.md` 以对象不同否掉）；
   同对象两份逐档概率 mean|Δ|=0.0125、max 0.0525；而落盘 `market_fair` 自己的口径
   **在 2026-10-15 换过一次**（之前 100% 可复算为 proportional，之后 Shin），
   记录里没有任何字段标明用了哪一种 → 账本 `delta_vs_market`、EV、市场基线跨该日不同质。
   配对 Brier 实测 Δ=-0.000212（SE 0.000487，t=-0.43，n=1379）→ **低于检测下限，
   不需要作废样本**，但这是"量级刚好被稀释掉"，不是"口径一致"。

## 判定摘要

`status`：clean=无问题；by_design=有意如此（附证据）；violation=缺陷，需人裁决。
"实害"= 本轮在真实样本上测到的影响，不是潜在风险。
表首列是本文阅读顺序号（正文交叉引用用它）；**机器侧口径是每行给出的 finding id**，
脚本 `--json` 的输出顺序与本文编号不同，且第 2 行只是 #1 的子项 ——
15 条判定分布在 #1–#16 行（`booster_gain_attribution` 在最后一行）。

| # | 判定 | status | sev | 关键数字（本次快照） | 证据（文件:行） | 已证实 / 推测 |
|---|---|---|---|---|---|---|
| 1 | `matrix_target_market_isolation` 目标市场派生列在真实矩阵里全 0 | clean | info | 4/4 列 `n_distinct=1`、`n_nonzero=0`；booster 增益 0.0000% | `engine/prediction/lgbm_model.py:41,285-291`、`config/prediction.json[lgbm].use_odds_features=false` | 已证实 |
| 2 | `matrix_target_market_isolation`（同一判定的子项）——名单只覆盖 4/27 列，其余静默 | by_design | low | 27 列中 23 列不在任何名单（含 `side_open_*`、`handicap`） | `engine/prediction/market_isolation.py:26-63,101-143` | 已证实（覆盖缺口） |
| 3 | `matrix_post_match_insensitivity`——训练矩阵对结算回写字段不敏感 | clean | info | 19 字段 / 26529 单元改非法值 → 0 个特征变化 | 回写 `engine/main.py:2183-2242`（另有一处 1463-1475）；读取 `engine/learning/lgbm_shadow.py:90-105` | 已证实（正对照：同机制改 `handicap` → 报出变化） |
| 4 | `matrix_price_field_insensitivity`——训练矩阵对目标市场价格字段不敏感 | clean | info | 11 个价格字段整列改 1.05 → 0 个特征变化 | `engine/main.py:711-715`、`lgbm_shadow.py:90-105` | 已证实 |
| 5 | `matrix_time_point_provenance`——时点可验证性：2 行开赛后写回、261 行无法证明赛前 | violation | low | 1237 赛前 / **2 开赛后**（+422 分钟、+152 分钟，均 2026-09-30）/ 21 缺 as_of / 240 两端都缺 | `engine/main.py:147-154`（`if _ko:` → kickoff 空串直接放行）、`:836`（as_of） | 已证实；1519 行里 72 行 kickoff 为空 |
| 6 | `side_snapshot_asof_gate`——侧挂防未来闸门整体空转 | violation | **medium** | 597 个真实 as_of 被 `_parse_ts` 解析成功 **0 个**；实害 0/29 | `engine/sources/njstats_side.py:81-88,104-111`、`engine/main.py:836`（`isoformat` 带 `T`+`+08:00`） | 已证实（闸门死）；实害测到 0（`would_block=0`） |
| 7 | `side_index_pair_key`——侧挂索引归并键不含场次 | violation | low | 2937 队对中 **90 对**跨多个开赛时间；2353 槽里 2095 个 kickoff 空 | `engine/sources/njstats_side.py:183-240`（`slot()` setdefault 保留首条 kickoff，`open_fair`/`snapshot_at` 被后写覆盖） | 已证实；实测受影响训练行 **1/29** |
| 8 | `matrix_dead_columns`——死列分两类：闸门关死（预期）/ 接线失效（缺陷） | violation | low | `odds_*`(4) 属前者；`is_knockout`、`rank_diff` 属后者，增益均 0.0000% | `engine/main.py:532-535,545-561`、`engine/sources/djyy.py:213`、`engine/sources/manager.py:241-366` | 已证实：353 份 `lgbm_inputs` 里 `rank_home` 非空 **0 份**；`main.py:532-535` 注释声称已修，结果未变 |
| 9 | `matrix_handicap_line_provenance`——`handicap` = 竞彩线优先 / 亚盘线兜底，同列无来源标记 | by_design | low | 9 种取值、1455/1500 非 0；唯一非整数线 0.25（2026-07-20 芬超，`handicap_*_odds` 全空 → 兜底路径） | `engine/sources/manager.py:348`、`engine/sources/sporttery.py:149-168`、`engine/prediction/lgbm_model.py:299`（`handicap or 0.0` 把"无盘"与"平手盘"并成一个取值） | 已证实（混合取源、名单不覆盖、无来源字段）；**"竞彩让球线算不算目标市场赔率"= 判据口径问题，需人裁决** |
| 10 | `serve_snapshot_quantization`——elo 落盘量化 → 输入不可逐位复现 | by_design | low | 1500/1500 行；`elo_home/away` 各 ≤0.05、`elo_diff` ≤0.1 | `engine/main.py:546`（未取整入参）、`:819-820`（`round(elo,1)` 落盘）、`lgbm_shadow.py:91-92`（读回落盘值） | 已证实（偏移）；实测对方向无影响（p95 0.00158 / max 0.0917 / 0 翻转） |
| 11 | `devig_caliber_duplication`——去水**同一口径** 7 处各写一份（等价性） | by_design | low | 8393 条真实三元组 max\|Δ\|=2.2e-16；非法赔率守卫 **3 种行为**（None / raise / 不设防） | `njstats_side.py:125`、`njstats_features.py:83`、`clv.py:45`、`book_vig.py:110`、`historical_replay.py:217`、`calibration.py:109`、`scripts/close_calibration.py:44`；第 8 份内联在 `lgbm_model.py:285-291`（守卫放宽成 `o>0`，overround 定义也不同） | 已证实（等价性靠注释维持，`njstats_features.py:83-84` 明写"口径与 historical_replay 一致"却无测试兜着）；实害 0（0/8393 条落在 (0,1]） |
| 12 | `league_name_caliber_duplication`——联赛名两张别名表互不串联 | violation | low | 11 个名字分歧；`韩职` 在 `LEAGUE_ALIASES` 不在 `LEAGUE_NAME_MAP` → 先验查不到；598/1519 行落默认值；矩阵命中率 13.5%（1298/1500 行为 2.5） | `engine/pipeline/helpers.py:25,29`、`engine/learning/freshness.py:31`、`engine/main.py:467`（写归一名）与 `:529`（查原始名）、`engine/prediction/league_stats.py:39-49` | 已证实：8/353 行冻结先验今天复现不出来（全是 `K1联赛`，冻结 2.5 vs 现查 2.34）；`data/league_matrix.json` 在本分支只有 1 个提交 `07247f8a9`(2026-11-04) 且含 `韩K联` → 差异由**键名不匹配**解释，不是矩阵漂移 |
| 13 | `market_blend_weight_duplication`——`market_blend_weight` 写了 5 处，只有 config 那份是活的 | by_design | info | 活值 0.0；`data/state/league_params.json` 45 个联赛被 `adapt_all()` 持续改写到 0.1~0.5，其 getter **0 个调用方** | 唯一消费点 `engine/prediction/dixon_coles.py:21,75-78`；构造点 `engine/main.py:283`；未接线说明 `engine/learning/league_params.py:347,366,376`（2026-10-14 E5 的量化决定） | 已证实（当前不掺，E5 决定不接线）；**推测**：谁顺手接上，竞彩赔率就以 ≤0.5 权重进模型腿，而 `audit_features()` 看不见这条通道（它不是特征名）→ 本审计已把它做成守卫 |
| 14 | `side_feature_name_parity`——侧挂特征名两处定义 | clean | info | 6 列名在两文件各写一份，当前一致；`site_fair` 不进 `to_features()` | `engine/prediction/lgbm_model.py:77-84`、`engine/sources/njstats_side.py:90-101`、`lgbm_model.py:219-227`（`predict_single` 按列号截断，错位**不报错**） | 已证实（当前一致；失败模式是静默错位） |
| 15 | `devig_method_family_divergence`——**同一份竞彩赔率被去水两次口径不同 + 落盘市场锚 `market_fair` 中途换口径** | violation | low | 逐日普查：2026-10-14 及以前 100% 复算为 proportional、2026-10-15 起为 Shin（447/1379 行）；无混合日；同对象（竞彩 HAD）Shin-vs-proportional 逐档 mean\|Δ\|=0.0125 / max 0.0525；「回放 vs 服务」不作同对象指控（对象不同，既有复核已裁） | `engine/prediction/calibration.py:116-118`（`select_devig_method` 恒返回 `devig_shin`）、`engine/main.py:475,733-735`（落盘只有 `[round(x,4) …]`，无口径标记）、`engine/review/clv.py:45`、`engine/backtest/historical_replay.py:217` | 已证实（口径不同 + 换过 + 无标记）；配对 Brier Δ=-0.000212（t=-0.43，n=1379）→ **低于检测下限，无需作废样本**；归因到 commit 做不到：本仓库历史在 `07247f8a9` 被压成单提交 |
| 16 | `booster_gain_attribution`——现网 booster 的重要性与 elo 量化实害 | clean | info | 27 列 / 900 棵树；零增益 12 列 = 4 个 `odds_*` + `is_knockout` + `rank_diff` + 6 个 `side_*`；elo 各抖 ±0.05 × 20 轮 → p95\\|Δp\\|=0.001577、max 0.091667、argmax 翻转 0/30000 | `data/models/lgbm_model.txt` + `engine/prediction/lgbm_model.py:60-84`（列序） | 已证实（在子进程里量，见上面「CI 平台差异」一条） |

补充数字（只读快照，不参与断言）：`lgbm_inputs` 覆盖率 353/1500（23.5%，最早 2026-10-22）；
`form_home` 527/1500；`djyy_model_prob` 404/1500；xG 1500/1500；elo 1499/1500（1 行缺键）；
侧挂特征只覆盖 29/1500 行（集中在 2026-10-12 ~ 10-14 采集窗口，36 份快照，
191/2353 槽有开盘去水值）；booster 27 列 / 900 棵树，增益前三
`handicap` 18.236% / `elo_diff` 12.748% / `xg_away` 11.861%，零增益 12 列
（4 个 `odds_*` + `is_knockout` + `rank_diff` + 6 个 `side_*`）；
`data/state/lgbm_status.json`：ready=false、`delta_vs_fusion_v2=-0.02405`（t=-2.59）、
`delta_vs_market=-0.02385`（t=-2.35）、quarantined_rows=20。

## 这些发现里，哪些"现在就能改"、哪些不能

红线：E8/E10 正在按冻结判据跑样本，**不改模型特征、门槛、上界、校准参数**；
`engine/backtest/`、`engine/prediction/calibration.py`、账本与状态文件禁触。

- **不能自行改**（都要动模型输入或特征列，落在红线上）：#6 闸门修复、#8 死列处置、
  #10 elo 精度、#12 别名表串联。任一条都会改变 shadow 训练矩阵取值，而该模型正在
  E8/E10 观察窗内每周重写 `data/models/lgbm_model.txt`（虽然
  `fusion.post_fusion.lgbm_blend=false` 不进生产概率，改前改后仍然不可比）。
- **纯观测、不改判定**（列出供裁决，本 PR 未实现）：
  #15 落盘加 `market_fair_method` 标记、#9 加 `handicap_source` 标记、
  #7 `slot()` 键里带开赛日。三条都只增加可归因性，不动任何数值口径；
  是否值得在冻结期动 `engine/`，请人定。
- **不需要动作的负面结论**：#1/#3/#4/#11/#13/#14（没发现问题或已证实为有意设计），
  已转成可复现守卫测试。
- **#15 是否要重开评估**：不需要。换口径前后的配对 Brier 差 t=-0.43，
  在 n=1379 上远低于 `lgbm_shadow` 现用门槛（`min_z`/`min_improvement`），
  因此 E8/E10 已结算样本不因它作废；但任何**跨 2026-10-15 的市场基线对比**
  都必须显式说明用的是哪一份去水。

## 预登记草案（未登记；登记是人的动作，本 PR 不碰 `docs/EXPERIMENTS.md`）

按 AGENTS.md「实验先登记」，下面三条是可直接抄进表的草案文本。
判定标准一经登记不许再改。

### 草案 A：侧挂闸门修复的逐位不变性（低风险，验证"修了没影响"）

- 假设：把 `_parse_ts` 扩到 `datetime.fromisoformat`（或落盘改空格格式）后，
  现有 1500 行训练矩阵**逐位不变**（依据：`would_block=0`、未来快照行=0）。
- 分支：本分支后续 commit。
- 主指标：修复前后 `build_training_rows` 输出 27 列的单元级最大差（期望 0）。
- 样本量：全量 1500 行，无需新样本。
- 判定日：改动当天（CI）。
- 判定标准：最大差 = 0 ⇒ 可进 main；> 0 ⇒ 视为特征变更，等 E8/E10 收样本结束再做。

### 草案 B：`rank_diff` / `is_knockout` 死列处置（需 E8/E10 之后）

- 假设：两列（27 列中的 2 列，增益 0.0000%）删除或真正接线，对 holdout Brier 的
  影响小于现有检测下限（≈0.001 @ n=444）。
- 分支：`agent/lgbm-dead-columns-*`。
- 主指标：`delta_vs_fusion_v2` 与 holdout Brier 的配对差（同 seed、同 split、同隔离表）。
- 样本量：`usable_rows=1480`、`holdout_n=444`。
- 判定日：E8/E10 冻结期结束后第一个训练周期。
- 判定标准：|ΔBrier| < 0.001 且 argmax 翻转 < 0.5% ⇒ 允许删列（收窄契约）；
  否则保留并补真实取数（`rank_home` 目前没有可用数据源提供）。

### 草案 C：单一联赛名口径的影响测量（只测不改判据）

- 假设：把 `_canon_league` 与 `LEAGUE_NAME_MAP` 串成一条链后，历史 1500 行里
  `league_avg_goals` 取默认值的比例从 **86.5%（1298/1500）** 显著下降，
  但对已结算账本的配对回放 Brier 影响落在 E5 已证明的"检测下限之下"量级。
- 分支：`agent/league-name-single-caliber-*`。
- 主指标：默认先验命中率；次指标：配对回放 ΔBrier（`scripts/ablation_replay.py` 同款配对）。
- 样本量：1500 行 / 46 个联赛名。
- 判定日：登记后 1 个结算周期。
- 判定标准：只出量化结论，**不以 ΔBrier 作为改判据的依据**
  （E5 教训：0.05 权重槽位上的效应需 n≈952 场 ≈3.2 年才检得出）。

### 草案 D（可选，纯观测）：市场锚口径标记

- 假设：给落盘记录加 `market_fair_method`（"shin"/"multiplicative"）不改任何概率、
  任何判定；此后跨期对比不再需要靠复算反推口径。
- 主指标：字段覆盖率（期望新样本 100%）；配对 Brier 差必须恒为 0。
- 判定标准：加字段前后同一批未结算场次的 `final_prob` 逐位相同；否则视为口径变更回退。

## 拔除影响评估

- 本 PR 新增 3 个文件：`scripts/train_serve_align_audit.py`、
  `tests/test_train_serve_align_audit.py`、本文。**删掉这 3 个文件即完全拔除。**
- 读写状态：只读 `data/state/review_ledger.jsonl`、`data/daily/*/predictions.json`、
  `data/state/njstats*/`、`data/models/lgbm_model.txt`、`config/*.json`、
  `data/league_matrix.json`、`data/state/league_params.json`；**不写任何文件**
  （变异实验写 `tempfile` 临时目录，进程退出即销毁）。
  不碰 `data/state/challenge_*`、`data/state/review_ledger.jsonl`（测试里只对账本做
  sha256 前后比对以证只读）、`engine/backtest/`、`engine/prediction/calibration.py`、
  `docs/EXPERIMENTS.md`。
- 副作用：会 `import` 生产模块并调用 `build_training_rows()` / `njstats_side.build_index()`；
  为了读 booster 重要性，还会用 `sys.executable` **再启一个自己的子进程**
  （`--booster-json`，只读、同样不写盘；子进程失败只让那一条判定变 skipped）
  （读文件、算特征），与 weekly shadow 训练读同样的路径；`shadow_train` **不调用**，
  因此不会重写 `data/models/lgbm_model.txt` 或 `lgbm_status.json`。
- 对调度：未改 `.github/workflows/*`；脚本不在任何 workflow 里，不会被自动执行，
  不影响 daily-forecast / ci-sync / health-check / njstats_duty。
  仓库门禁 `pytest tests/ -q` 会收集本测试文件（约 3 秒，纯只读；
  没有 lightgbm 的机器上 booster 那一条自动 skip，Windows CI 同样安全：
  不依赖 `lightgbm` 之外的可选依赖，不读符号链接、不写盘）。
- 回退步骤：`git revert <本 PR 的合并提交>`，或直接删上述 3 个文件；无数据、无状态需要恢复。

## 附：报告片段（本次真实输出）

```
【侧挂快照的未来信息闸门（NJStats → side_*）】gate_dead  (severity=medium, violation)
  asof_format: {'samples': 597, 'parsed_by_production_gate': 0,
                'example': '2026-10-06T17:26:39+08:00'}
  计数：{'rows': 1500, 'rows_with_side_features': 29, 'would_block_with_strict_gate': 0,
        'foreign_fixture_open_price_rows': 1, 'index_slots': 2353, 'slots_with_open_fair': 191}

【训练行的时点可验证性（预测时刻 vs 开赛时刻）】post_kickoff_rows_present  (severity=low, violation)
  buckets: {'before_kickoff': 1237, 'after_kickoff': 2, 'no_asof': 21,
            'no_kickoff': 0, 'no_asof_or_kickoff': 240}
  post_kickoff_rows: [{'date': '2026-09-30', 'match_id': '2026-09-30_周三001',
                       'as_of': '2026-09-30T21:02:12+08:00',
                       'kickoff': '2026-09-30 14:00:00', 'minutes_after_kickoff': 422.2}, …]

【去水方法族之差：服务 Shin vs 回放 proportional】anchor_caliber_switched_mid_sample  (severity=low, violation)
  per_leg_abs_delta: {'mean': 0.012537, 'max': 0.052466}
  persisted_market_fair_is_shin: {'rows': 1379, 'match_shin': 447}
  caliber_switch: {'last_proportional_day': '2026-10-14', 'first_shin_day': '2026-10-15',
                   'mixed_caliber_days': []}
  paired_brier: {'n': 1379, 'mean_delta': -0.000212, 'se': 0.000487, 't': -0.43,
                 'abs_median': 4.6e-05, 'abs_max': 0.105122}
```
