# 复核：DC 层 market_blend 是否把竞彩赔率拼进模型概率 + devig 口径

- 复核基线：上游 `wlrwx/football-engine` main `3455788`（2026-11-04）。
- 触发：dykily/football-engine#5「高优先复核」
  （<https://github.com/dykily/football-engine/issues/5>），对象是
  `scripts/leak_audit_check.py:455-461`
  `UNVERIFIED_NOTES` 里两条「本次未复核，待核」的观察点。
- 复核方式：只读。追**取数代码 / 配置值 / 消费方**，不以命名、注释、docstring
  作证据；行为测试只喂假数据或只写 pytest `tmp_path`。
- 可复现检查：`tests/test_dc_market_blend_review.py`（22 项）。运行：
  `.venv/bin/python -m pytest tests/test_dc_market_blend_review.py -q`
- 断言口径：断言**判定结论与判定规则**，不钉硬编码计数、不钉行号；相对路径
  一律 `as_posix()`。本文的行号是复核当时的快照，只供人读。
- **本 PR 不改任何生产代码、配置或状态文件**；`engine/backtest/` 与
  `engine/prediction/calibration.py` 属禁触路径，只读分析。

## 判定摘要

| 待核项 | 判定 | 一句话理由 |
|--------|------|-----------|
| 1. DC 层 `market_blend_weight` 把竞彩赔率拼进模型概率 | **不是真违规**（info，路径不活） | 来源确实是 target（竞彩 HAD 优先），但生产权重 `0.0`，喂任意 target 赔率输出概率逐位不变；三条判据只满足一条 |
| 2. 回放 `devig('proportional')` vs 服务 `select_devig_method→devig_shin` | **不构成同对象漂移**（info，措辞修正） | 两套口径作用于**不同市场对象**（回放=参考盘收盘基准 / 服务=竞彩目标盘融合输入）；裁决融合开关的回放直接用生产冻结的 `market_fair`，不重新去水 |

判定纪律（issue 原文）：只有「来源是 target + 路径在产 + 可复现」三条同时成立
才给 high/violation；路径不活封顶 info（`scripts/leak_audit_check.py:71-75`
`cap_severity`）。下面逐条给代码事实。

---

## 待核 1：DC 层 market_blend

### 问 1：来源是哪个市场 —— **target（竞彩官方），不是 reference**

完整取数链（每一跳都是代码，不是命名）：

1. 生产入口：`.github/workflows/daily-forecast.yml:114` `python -m engine.main --date "$DATE"`。
2. `engine/main.py:442-446`：
   ```python
   market_odds = None
   if fixture.home_odds and fixture.draw_odds and fixture.away_odds:
       if all(o > 1.0 for o in (fixture.home_odds, fixture.draw_odds, fixture.away_odds)):
           market_odds = (fixture.home_odds, fixture.draw_odds, fixture.away_odds)
   ```
   `engine/main.py:459-464` 把同一个变量喂进 `model.predict(market_odds=market_odds)`。
3. `fixture` 的三项赔率来自合并层 `engine/sources/manager.py:343-346`：
   ```python
   # 融合: 体彩优先，Bet365 兜底
   final_h = sf.home_odds or b365_had["h"]
   ```
   降级分支同口径（`manager.py:437-440`：`st_had.get("h") or b365_had["h"]`）。
   即**竞彩优先、Bet365 只补缺失**，不是参考盘优先。
4. `sf.home_odds` 的取数代码 `engine/sources/sporttery.py:149,162-164`：
   ```python
   had = item.get("had", {})
   home_odds=self._safe_float(had.get("h")),
   draw_odds=self._safe_float(had.get("d")),
   away_odds=self._safe_float(had.get("a")),
   ```
   端点 `sporttery.py:28`
   `https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry`
   = 中国体育彩票官方竞彩计算器接口，`had` = 竞彩胜平负盘。**这是预测对象本身
   （target），按 `engine/prediction/market_isolation.py:6-9,26-43` 的口径禁止参与
   概率形成。**
5. 唯一 fallback 是参考市场：`engine/main.py:447-457` 在国内源缺失时用
   `djyy_pre["pinnacle_odds"]`（Pinnacle = reference，允许）。

行为复现：`test_sporttery_fixture_odds_are_the_target_market_had` 用假 API 响应
（`had={h:2.10,d:3.40,a:3.80}`、`hhad` 另一组值）跑 `fetch_fixtures`，断言 Fixture
三项赔率 == `had`、让球盘取的是 `hhad`、端点含 `sporttery`；
`test_production_model_input_is_fixture_odds_with_pinnacle_fallback` 用 AST +
定义式闭包钉住「首选 fixture、兜底可追到 `pinnacle_odds`、同一变量喂进 predict」；
`test_merged_fixture_prefers_sporttery_over_bet365` 钉住合并层的 `体彩 or Bet365`。

### 问 2：路径是否在产 —— **分支会执行，但对输出恒无影响（权重 0）**

- **配置值**：`config/prediction.json:11` → `"market_blend_weight": 0.0`。
- **生产构造方式**：`engine/main.py:283-284`
  ```python
  dc_cfg = DixonColesConfig(**{k: v for k, v in pred_cfg.get("prediction", {}).items()
                                if k in DixonColesConfig.__dataclass_fields__})
  ```
  `pred_cfg = load_config("prediction")`（`main.py:102`），`load_config` 只读
  `config/prediction.json`（`engine/pipeline/helpers.py:15-19`），无环境变量覆盖。
  因此生产用的是 **0.0**，不是 dataclass 默认值 `0.28`（`dixon_coles.py:21`）。
  `main.py:300-304` 把该 cfg 交给 `EnsembleModel`。
- **分支本体**：`engine/prediction/dixon_coles.py:75-80`
  ```python
  if market_odds and all(o and o > 1.0 for o in market_odds):
      market_probs = self._de_vig(market_odds)
      w = self.cfg.market_blend_weight
      home_win = (1 - w) * home_win + w * market_probs[0]
  ```
  没有独立开关，唯一的失效手段就是 `w == 0`。行为测试
  `test_blend_branch_executes_but_is_output_inert` 用 spy 证明 `_de_vig` 在生产
  配置下**确实被调用**（分支在产会走到），但输出与 `market_odds=None` 逐位相同。
- **有没有别的活入口把非零权重带进来**（逐个查，全部不活）：
  | 位置 | 事实 | 判定 |
  |------|------|------|
  | `scripts/elo_backfill_replay.py:92` | `DixonColesModel(DixonColesConfig())` = 默认 0.28，但 `:80` 唯一 predict 调用传 `market_odds=None` | 不活 |
  | `evolution/harness/model_core.py:38` | `DEFAULT_PRED["market_blend_weight"]=0.28`，`evolution/` 全量 AST 扫描无任何读取该键的代码 | 死键 |
  | `evolution/demo/base_prediction.json:11` | demo 配置 0.28，非生产 config | 不在产 |
  | `data/state/league_params.json` | 联赛级 `market_blend_weight` 有 0.1~0.5 的非零值，但取值函数 `engine/learning/league_params.py:376` `get_market_blend` 在 `engine/`+`scripts/` **零调用方** | 死状态 |
  | 自动写回 config | `engine/main.py:2438-2447` 结算只写回 `rho`；`scripts/`、`evolution/` 无任何代码写 `market_blend_weight` | 不会被自动改回非零 |

复现：`test_production_dc_blend_weight_is_zero`、
`test_production_dc_config_comes_from_config_not_dataclass_defaults`、
`test_league_level_market_blend_is_not_wired`、
`test_only_default_config_dc_script_feeds_no_odds`、
`test_evolution_harness_blend_key_is_dead`。

### 问 3：是否可复现 —— **不可复现（生产配置下输出对 target 赔率不敏感）**

`test_dc_probabilities_do_not_follow_target_odds_under_production_config`：
固定评级，喂三组明显不同的 target 赔率
`(2.10,3.40,3.80)` / `(1.13,6.30,12.0)` / `(4.80,3.90,1.72)`，
按生产方式构造的 DC 输出胜平负与 xG **逐位相同**（与 `market_odds=None` 也相同）。
`test_ensemble_probabilities_do_not_follow_target_odds_under_production_config`
在生产入口 `EnsembleModel(DC+MC)` 上得到同样结论。

防假绿（证明不是"分支根本不存在"或"测试喂不进赔率"）：
`test_nonzero_blend_really_pulls_probabilities_toward_target_odds` 把权重显式设为
0.28 后，同一分支确实改变概率，且每个分量都朝该组 target 赔率的比例去水值移动
（`g ≈ (1-w)·p + w·fair`），极端热门盘的主胜概率显著高于均衡盘。

变异验证（本地临时改 `config/prediction.json` 跑完立即 `git checkout --` 还原，
未提交任何配置改动）：

| 变异 | 期望 | 实测 |
|------|------|------|
| `market_blend_weight` 0.0 → 0.28 | 必须变红（真回归） | **5 failed**：`test_production_dc_blend_weight_is_zero`、`test_dc_probabilities_do_not_follow_target_odds_under_production_config`、`test_ensemble_probabilities_do_not_follow_target_odds_under_production_config`、`test_blend_branch_executes_but_is_output_inert`、`test_same_object_double_devig_exists_but_is_masked_by_zero_blend` |
| 还原后 | 全绿 | 22 passed |

### 判定

三条判据：来源 target **成立**；路径在产 **不成立**（权重 0，输出恒不变）；
可复现 **不成立**。按 `cap_severity` 纪律封顶 **info**，**不是 violation**。
原观察点「把竞彩赔率拼进模型概率」描述的是**代码里存在但被配置关掉的机制**，
不是当前生产行为。

### 交人裁决的风险点（本 PR 只记录，不实施）

1. **既有回滚步骤会制造真违规**：`docs/DOUBLE_MARKET_BLEND_20261023.md:26` 写
   「回滚：把 `market_blend_weight` 改回 `0.28` 即可」。按本次复核，一旦照做，
   三条判据同时成立（target + 在产 + 可复现）= 真市场隔离违规，且重新双重计入
   市场。新增测试会在这种情况下变红。是否保留该回滚路径属产品/风控决策。
2. **联赛级死状态易误读**：`data/state/league_params.json` 里 0.1~0.5 的非零
   `market_blend_weight` 没有任何消费方（`get_market_blend` 零调用）。建议要么
   删键要么显式标注未接线，避免下次审计再把它当成"在产"。
3. **另一条 target→模型路径（与 DC 主张不同，无权重开关）**：
   `engine/prediction/monte_carlo.py:81-99`，当球队评级为默认值
   （`elo==1500 and attack==1.0`）时用 `_xg_from_odds(market_odds)`（`:182-197`，
   比例去水）反推 xG。生产评级缺失的球队会落到默认值
   （`engine/learning/elo_updater.py:94-99`），因此这条路径**没有 w=0 之类的
   掩蔽**。行为测试 `test_mc_odds_to_xg_branch_needs_default_ratings` 钉住：
   非默认评级下概率对赔率逐位不变；默认评级下不同 target 赔率给出不同概率。
   本次未统计生产命中频率（需跑生产数据，超出只读复核范围）。是否允许属裁决。
4. **目标盘仍经融合市场通道进入最终概率（既有设计，非 DC 内混）**：
   `engine/main.py:473-475` 对同一份竞彩赔率做 Shin 去水得 `calibrated_probs`，
   `:597-599` 作为 `market_probs` 进入 `fuse_probabilities`，
   `config/prediction.json:53` `fusion.market_weight = 0.6`。这是
   `docs/DOUBLE_MARKET_BLEND_20261023.md` 确立的「市场只在融合层进入一次」设计。
   `market_isolation.py` 的黑名单按**特征键**判违规，不覆盖融合通道；该设计是否
   符合市场隔离方法论属产品裁决，不在本次两条待核项范围内。

---

## 待核 2：devig 口径回放与服务不一致

### 函数级字面主张成立

- 回放：`engine/backtest/historical_replay.py:217-229` `devig(odds, method="proportional")`
  默认比例去水；调用点硬编码 `"proportional"`（`engine/backtest/gate_test.py:123,136-137`、
  `cross_market.py:301`、`residual_market.py:110-111`、`gate_reachability.py:109`），
  CLI 可改（`historical_replay.py:556`），持久化默认
  `data/state/historical_replay.json:3` 也是 `"proportional"`。
- 服务：`engine/main.py:475` `select_devig_method(list(market_odds))`；
  `engine/prediction/calibration.py:116-118` 是**无条件别名**：
  ```python
  def select_devig_method(odds: list[float]) -> list[float]:
      return devig_shin(odds)
  ```
  没有判据、没有阈值、没有 config 开关（AST 断言函数体内无 `If`/`IfExp`）。
  `devig_shin`（`calibration.py:14-79`）二分求 z，与回放那份固定 `z=0.02`
  （`historical_replay.py:246-255`）**不是同一实现**。

### 但两者作用于不同对象、不同用途 —— 不构成回放/生产漂移

| | 函数/口径 | 赔率对象 | 用途 |
|---|---|---|---|
| 回放基准 | `devig(..., "proportional")` | football-data.co.uk **参考盘收盘**：`AvgCH/AvgCD/AvgCA` → `PSCH/PSCD/PSCA`（Pinnacle）→ `B365CH/...`（`historical_replay.py:161-165`） | 市场基准/要战胜的对手、EV 闸门可达性 |
| 服务 | `select_devig_method → devig_shin` | **竞彩 HAD（target）**，Pinnacle 仅兜底（`main.py:443-457`） | 融合层市场通道输入 `market_fair` |

行为复现：`test_replay_benchmark_object_is_reference_close_not_target` 用临时 CSV
证明 loader 的 `close` 依次取 Avg → Pinnacle → Bet365，且塞入假设的
`SportteryH/D/A` 列**完全不影响**结果（对象里根本没有目标盘）。

**裁决融合开关的回放不重新去水**：`scripts/ablation_replay.py:155`
`market = tuple(r["market_fair"]) if r.get("market_fair") else None`，直接用预测时
冻结的 Shin 去水值（`main.py:733-735` 落盘），全文件无任何 devig 调用/导入
（AST 断言）。`evolution/harness/fusion_core.py`、`engine/backtest/walk_forward.py`
同样读冻结 `market_fair`。因此**生产开关裁决不存在口径漂移**，不要去"修"
`ablation_replay.py`。

### 真正的同对象不一致在服务内部（被 w=0 掩蔽）

同一份 `market_odds` 在服务里被去水两次、口径不同：
`dixon_coles.py:76` + `:214-218` 用**比例**去水做内混，`main.py:475` 用 **Shin**
去水做 `market_fair`。`test_same_object_double_devig_exists_but_is_masked_by_zero_blend`
证明两者对同一组赔率结果不同，而 DC 内混被 `market_blend_weight=0.0` 掩蔽
（问 3 的逐位不变断言）。MC 的 `_xg_from_odds`（`monte_carlo.py:182-197`）也是
比例去水，默认评级时活（见待核 1 风险点 3）。

口径差的量级（本地实测，最大分量差）：

| 赔率 | Shin vs 比例 | 服务 Shin vs 回放 Shin(z=0.02) |
|------|--------------|-------------------------------|
| (2.10, 3.40, 3.80) | 0.0032 | 0.0006 |
| (1.75, 3.60, 4.80) | 0.0088 | 0.0028 |
| (1.39, 4.90, 4.85) | 0.0289 | 0.0203 |
| (1.13, 6.30, 12.0) | **0.0422** | **0.0297** |

即 `calibration.py:41-42` 声称的「比例 vs 真 Shin max 0.01482」在其样本口径外
低估了极端热门（1.13 处 4.2pp）。测试只断言「差距随热门强度增大」这一结论
（`test_shin_vs_proportional_gap_grows_with_favourite_strength`），不钉具体数值。

### 判定与提案（不实施）

- 判定：原句在**函数层面为真**，但作为「回放与生产口径漂移」**不成立**——
  对象不同、用途不同，且真正裁决生产开关的回放用冻结值。封顶 **info**。
- 两侧都在禁触路径（`evolution/harness/patch_engine.py:47-57` `FORBIDDEN` 含
  `engine/backtest/` 与 `engine/prediction/calibration.py`），本 PR 只读。
- 若将来要统一口径：需人工 review；注意现有测试分别钉住两侧
  （`tests/test_book_vig.py:141-153` 以 0.05 容差接受两份 Shin 不等价、
  `tests/test_historical_replay_and_gate.py:42-49` 钉比例法、
  `tests/test_njstats_features.py:36-47` 钉 njstats 与回放默认口径一致），
  单方面"统一"会改考卷。建议先明确「基准盘口径」与「目标盘融合口径」是否
  本就应当不同，再决定要不要动代码。

---

## 验证输出（本地，cwd=工单目录，解释器=生产 venv）

```
$ /Users/dykily/football-engine/.venv/bin/python -m pytest tests/test_dc_market_blend_review.py -q
......................                                                   [100%]
22 passed in 0.55s

$ /Users/dykily/football-engine/.venv/bin/python -m pytest \
    tests/test_dc_market_blend_review.py tests/test_prediction_semantics.py \
    tests/test_dixon_coles_dimension.py tests/test_draw_baseline_semantics.py \
    tests/test_leak_audit.py tests/test_leak_audit_independent_review.py \
    tests/test_book_vig.py tests/test_historical_replay_and_gate.py -q
........................................................................ [ 67%]
...................................                                      [100%]
107 passed in 3.56s

$ /Users/dykily/football-engine/.venv/bin/python -m ruff check --select F engine scripts tests evolution
All checks passed!
```

## 与既有守卫的关系

`tests/test_prediction_semantics.py:21-32` `test_market_is_counted_once` 断言的是
「DC 内混与 fusion 市场权重不同时非零」，**允许** `blend=0.28 + fusion_market=0`
的组合；本次新增的断言更强：生产配置下模型概率对 target 赔率**逐位不敏感**，
直接钉住市场隔离结论，配置一旦翻回非零即红。

`scripts/leak_audit_check.py` 的 `UNVERIFIED_NOTES` 未改（它是 PR #80 当时的历史
记录，只进报告、不参与判定）。两条观察点的复核结论以本报告 + 新测试为准；
是否把它们升级为审计脚本的正式 findings 属下一步决策，本 PR 不改审计输出语义。

## 局限

- 运行时实际赔率受 WAF/降级影响（体彩 → 新浪 → Bet365 → DJYY Pinnacle），
  仓库内无法核验某一轮实际用了哪家；判定依据是取数代码的优先级 + 配置值 +
  行为测试三项独立事实。
- MC「默认评级 → 赔率反推 xG」在生产的命中频率未统计（需跑生产数据）。
- 本复核只读，未改任何生产代码/配置/状态；回退 = revert 本 PR 单一提交。
