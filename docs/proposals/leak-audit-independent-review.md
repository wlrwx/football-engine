# 独立复核：PR #80 泄漏审计 6 条结论（盲测）

- 复核基线：上游 `wlrwx/football-engine` main `1bb3ba2`（2026-11-04）。
- 复核方式：盲测。不假设原审计任何一条成立，逐条追**取数代码 / 开关 / 消费方**；
  不以 docstring、注释或文档措辞作证据。
- 可复现检查：`tests/test_leak_audit_independent_review.py`（只读，不改任何生产
  行为）。运行：
  `.venv/bin/python -m pytest tests/test_leak_audit_independent_review.py -q`
- **断言口径（2026-11-04 按 review 意见重写）**：测试断言**判定结论**，不断言
  计数。本文里的「14 字段 / 29 处 / 17+12 / 16 参 15 参 14 参」都是复核当时的
  **快照数字，只供人读**；代码演进（黑名单加字段、挪行、`build_features` 加带
  默认值的参数）不会让 CI 变红，只有判定被推翻才红。机器侧断言的是：
  - 黑名单命中**全部落在允许位置**（黑名单/特征名表定义、受闸赋值、`__main__`
    自检、`tests/`），且生产特征对目标市场赔率**不敏感**；
  - 没有任何代码把 `use_odds_features` / `include_market_odds` 设成 `True`；
  - **规则**：config 开关为 false ⇒ `sina_odds_movement` 不得改变概率、不得进
    trace（因此该发现的 severity 不得高于 info，规则在审计脚本侧同样落实）；
  - 概率形成不**消费** `series` 键（按 `x["series"]` / `.get("series")` 判定，
    不按"提到该词"判定）；
  - 调用点未传的参数一律有签名默认值，且训练/服务在共同输入下**特征逐位相同**。
- 本复核由自动化 agent 完成；未改动任何生产代码、配置或状态文件。

## 判定摘要

| # | 原结论 | 判定 | 一句话理由 |
|---|--------|------|-----------|
| 1 | 黑名单 14 字段 29 处命中，全在两文件，均预期 | **成立**（措辞修正） | 命中全部落在定义/特征名表/受闸赋值/自检演示，生产特征不带市场信息；lgbm 命中不是"参数注释" |
| 2 | `use_odds_features` 默认 False，无处设 True | **成立** | 默认值、两处 config、生产/训练调用全为 False |
| 3 | `sina_odds_movement` 用新浪竞彩水位调概率，high 违规 | **不成立** | 取数代码选 Bet365/Pinnacle（参考市场）；且生产开关 false，路径不活 |
| 4 | `series` 仅用于结算验证，未进入概率形成 | **成立**（措辞修正） | 未进入概率形成属实；实际消费者是页面展示+落盘，找不到结算验证读取代码 |
| 5 | `njstats_features` 输出 opening/opening_fair，未混竞彩收盘 | **成立** | 代码只读 `openingHome/Draw/Away`，不读任何 closing 字段 |
| 6 | `build_features` 签名与 main.py 调用 16 参完全一致 | **不成立**（按字面） | 签名 16 参，生产调用只传 15 参（`is_knockout` 未传）；两端同默认值，无实际特征偏移 |

市场隔离口径（代码事实，非注释）：`engine/prediction/market_isolation.py`
`audit_features`（121-139 行）对 `TARGET_MARKET_FEATURES`（26-43 行）键报
high 违规、对 `BENCHMARK_ONLY_FEATURES`（59-63 行）报 medium、对
`REFERENCE_MARKET_FEATURES`（46-56 行，含 `pinnacle_*`）只记入
`used_reference_features`，不报违规——即参考市场派生值**允许**进入概率形成。

---

## 1. 黑名单扫描 —— 成立（措辞修正）

独立重扫 `engine/` 全量 `.py`（词边界正则，14 字段来自
`TARGET_MARKET_FEATURES`）：**29 处命中**，分布：

- `engine/prediction/market_isolation.py` **17 处**：模块 docstring 1 处（:11）、
  黑名单定义 14 处（:28-42）、`__main__` 自检演示 2 处（:204-205）。
- `engine/prediction/lgbm_model.py` **12 处**：`BASE_FEATURE_NAMES` 4 处
  （:53-56）、`build_features` 内赋值 8 处（:288-296）。

**措辞修正**：原审计称 lgbm 命中为「`build_features` 参数注释」——不确。
:53-56 是特征名列名，:288-296 是**真实赋值代码**：

```python
# lgbm_model.py:285-296
if include_market_odds and odds and all(o and o > 0 for o in odds):
    ...
    features["odds_home_impl"] = raw_probs[0] / overround
    ...
else:
    features["odds_home_impl"] = 0.0
    ...
```

「均为预期出现」的结论仍成立：赋值受 `include_market_odds` 闸控制，该闸
默认 False 且生产中恒 False（见第 2 条），四个字段恒 0.0，无信息泄漏。
market_isolation 侧的"测试"实为 `__main__` 演示（:199-211），不在 tests/。

复现：`test_claim1_every_blacklist_hit_is_in_an_allowed_location`、
`test_claim1_classifier_rejects_an_ungated_production_assignment`（防假绿）、
`test_claim1_production_features_carry_no_target_market_information`。
上面的 17/12/29 是快照数字，不是断言。

## 2. `use_odds_features` 开关 —— 成立

- `engine/prediction/lgbm_model.py:41`：`use_odds_features: bool = False`
- `config/prediction.json:87`：`"use_odds_features": false`
- `evolution/demo/base_prediction.json:86`：`false`
- 生产接线 `engine/main.py:241-242` 由 config 构造 `LGBMConfig`，
  `main.py:559`：`include_market_odds=lgbm_cfg.use_odds_features`
- 训练侧 `engine/learning/lgbm_shadow.py:103`：`include_market_odds=False`（硬编码）
- AST 全量扫描 `engine/ scripts/ tests/ evolution/`：没有任何
  `use_odds_features=True` / `include_market_odds=True` 的关键字传参或赋值。

复现：`test_claim2_no_code_turns_the_odds_gate_on`、
`test_claim2_configured_gate_keeps_target_odds_out_of_features`。

## 3. `sina_odds_movement` high 违规 —— 不成立

该步确实存在（`engine/prediction/fusion.py:307-325`，compression
>1.05/<0.95 时 ±0.02 后归一化），但原结论的两个前提都被代码事实推翻：

**(a) 信号源是参考市场，不是竞彩官方赔率。** 追到取数代码
`scripts/fetch_sina_odds.py`：

```python
# :100-103
bet365 = next((c for c in euro if c.get("companyId") == "7"), None)
pinnacle = next((c for c in euro if c.get("companyId") == "9"), None)
first = euro[0] if euro else None
primary = bet365 or pinnacle or first
# :142-144  compression = 该公司 初盘/即时
"home": round(ini["home"] / cur["home"], 3) if cur["home"] > 0 else 1.0,
```

比赛清单来自竞彩接口（:55 `footballMatchListJczq`），但赔率取自新浪聚合的
**18 家欧赔公司**（:65-70 `footballMatchOddsEuro`），优先 Bet365/Pinnacle。
行为测试证实：即使 euro 列表里混入一家"竞彩官方"，`extract_odds_snapshot`
仍选 Bet365/Pinnacle 计算 movement/compression。按 market_isolation 的代码
口径（见上），参考市场信号允许进入概率形成，不构成目标市场违规。

**(b) 生产路径不活。** `config/prediction.json:68`：
`"sina_odds_movement": false`。`fusion.py:153-154` 用传入的 post_fusion
覆盖默认值，`main.py:593` 从 config 取开关。行为测试：给足
compression=1.2/0.8 的强信号，按生产开关跑 `fuse_probabilities`，
概率逐位不变、trace 无该步。没有任何优化器写 post_fusion
（`fusion_optimizer.py:333` 的全 False 只用于反事实评估；
`ablation_replay.py` 是回放）。按复核方法「路径不活的最高只能标待核/info，
不许给 high」，severity=high 不成立。

**相关但不同的活路径**（不构成概率形成违规）：`main.py:688-692` 把同一
compression 转成 `market_signal` 落盘（:758），`main.py:1096` 在**策略层**
用作 `market_signal_gate`（`engine/strategy/kelly.py:319-345`，注额闸门），
`engine/review/post_match.py:99-100` 做结算命中率统计。策略层闸门/评估正是
市场赔率的合法用途，且该信号本身是参考市场数据。

**保留意见**：`primary` 的兜底 `euro[0]` 身份未由代码钉死；
`data/daily/*/odds_sina.json` 被 gitignore（.gitignore:20），CI 运行时重抓
（daily-forecast.yml:80、ci-sync.yml:79），仓库内无法核验运行时实际公司。
但原结论「新浪竞彩水位」的前提不被取数代码支持，且开关关闭，判定不受影响。

复现：`test_claim3_switch_off_implies_probabilities_unchanged`（规则形式）、
`test_claim3_step_fires_only_when_switch_on`、
`test_claim3_compression_computed_from_bet365_or_pinnacle`。

## 4. `series` 未进入概率形成 —— 成立（措辞修正）

- 构造点：`engine/pipeline/sina_odds.py:134`（`build_sina_data` 内）
  `"series": series_features(match_id)`；特征计算在
  `engine/odds_series.py:101-140`。
- **未进入概率形成**：`fusion.py` 全文无任何 `"series"` 字符串常量（AST 验证），
  该模块只读 `movement`/`compression`（:307-310）；行为测试：给 `sina_data`
  注入任意 `series` 值，融合输出逐位不变。
- **措辞修正**：「仅用于结算验证」不确。全仓库读 `["series"]` /
  `.get("series")` 的代码只有 `engine/build_site.py:973`（水位监控表）与
  :2314（`_odds_series_chip` 标签）——都是**页面展示**；另有
  `main.py:840` 把整个 `_sina_data` 落盘进 `predictions.json["sina_odds"]`。
  `engine/review/` 与 `scripts/` 中没有任何 `"series"` 字符串常量（AST 扫描），
  即不存在结算验证读取代码。展示+落盘比"结算验证"更不构成泄漏。

复现：`test_claim4_series_built_in_build_sina_data`、
`test_claim4_fusion_never_consumes_series`、
`test_claim4_series_does_not_change_probabilities`、
`test_claim4_no_settlement_reader_of_series`、
`test_claim4_series_consumed_by_build_site_display`。

## 5. `njstats_features` 开盘赔率 —— 成立

`engine/sources/njstats_features.py:132-138`：

```python
oh, od, oa = r.get("openingHome"), r.get("openingDraw"), r.get("openingAway")
if oh and od and oa:
    opening = {"home": float(oh), "draw": float(od), "away": float(oa)}
# :150-151
"opening": opening,
"opening_fair": devig(opening) if opening else None,
```

模块只读 `opening*` 字段；AST 扫描无任何含 "closing" 的字符串常量。
行为测试：给记录塞入假设的 `closingHome/Draw/Away`，输出不受影响。
下游 `engine/sources/njstats_side.py:204-208` 把 `opening` 去水成 `open_fair`，
`to_features()`（:90-101）只输出 `side_open_*`/`side_ppg_*`；站点价
`site_fair`（:216-223，目标市场）被 `to_features` 排除（行为测试证实）。

**另列待核（info 级，路径不活）**：`openingHome` 属于哪家市场，取数代码没有
标识——`scripts/njstats_archive.py:164-172` 只是归档 NJStats `/prob/{ep}`
端点响应。当前线上 booster 已是 27 列（`data/models/lgbm_model.txt:6`
`max_feature_idx=26` = 21 基础 + 6 侧挂），`side_open_*` 在 booster 内；
但 `config/prediction.json:65` `"lgbm_blend": false` 且
`data/state/lgbm_status.json` `"ready": false`，LGBM 概率不进生产融合，
故不影响当前生产概率。若将来启用 lgbm_blend，应先在采集侧确认开盘价市场归属。

复现：`test_claim5_extract_record_reads_opening_only`、
`test_claim5_site_price_never_reaches_side_features`。

## 6. 训练/服务 16 参完全一致 —— 不成立（按字面）

AST 计数（可复现）：

- 签名 `engine/prediction/lgbm_model.py:253-270`：**16 个参数**（含
  `is_knockout`，:265）。
- 生产调用 `engine/main.py:545-561`：**15 个关键字实参**，
  缺 `is_knockout`。
- 训练调用 `engine/learning/lgbm_shadow.py:90-105`：**14 个关键字实参**，
  缺 `odds` 与 `is_knockout`。

「一一对应（16 个参数完全一致）」与代码不符。**实质结论的修正**：未传参数
两端同取默认值——`is_knockout` 默认 False → 特征恒 0.0（:326）；`odds` 在
服务侧虽传入但被 `include_market_odds=False` 闸掉，训练侧不传，两端同样
恒 0.0。因此**不存在实际的训练/服务特征偏移**，"对齐"的实质结论成立，
但原审计给出的证据（16 参完全一致）不成立。

复现：`test_claim6_omitted_params_all_have_signature_defaults`、
`test_claim6_training_and_serving_features_do_not_diverge`。
16/15/14 是快照数字；断言的是「未传参数都有默认值」+「两端特征逐位相同」。

---

## 原审计之外另列的风险点（均给代码事实，按方法封顶）

1. **NJStats 开盘价市场归属待核（info，路径不活）**：见第 5 条。
   `njstats_archive.py:164-172` 无市场标识；`side_open_*` 已在 27 列 booster
   内（`lgbm_model.txt:6`），但 `lgbm_blend=false`（config:65）+
   `lgbm_status.json ready=false` → 当前不影响生产概率。启用前需采集侧确认。
2. **`audit_features` 未接入生产（info）**：`engine/`、`scripts/` 中除
   `market_isolation.py` 自身外无任何调用方（grep 证实）——隔离审计目前是
   库函数而非生产闸门。且它按键判违规（:121-129），若将来直接对生产
   `feature_dict` 运行，会把恒 0.0 的 `odds_*` 键误报为 high。
3. **`fetch_sina_odds` 兜底公司身份未钉死（info，开关关闭）**：
   `primary = bet365 or pinnacle or first`（:103），Bet365/Pinnacle 缺席时
   取列表第一家，代码未约束其身份。当前 `sina_odds_movement=false`，
   若将来重新启用，建议先在采集侧固定公司白名单。

## 验证输出（本地，cwd=工单目录，解释器=生产 venv）

```
$ .venv/bin/python -m pytest tests/test_leak_audit_independent_review.py -q
..................                                                       [100%]
18 passed in 1.38s

$ .venv/bin/python -m ruff check --select F engine scripts tests evolution
All checks passed!
```

抗噪声明式验证（本地临时改生产文件做变异，跑完立即 `git checkout --` 还原，
未提交任何生产改动）：

| 变异 | 期望 | 实测 |
|------|------|------|
| 黑名单新增字段 `probe_new_impl` | 不应变红（正常演进） | 18 passed |
| `build_features` 新增带默认值参数 `probe_flag=False` | 不应变红 | 18 passed |
| `use_odds_features: bool = False` 改成 `True` | 必须变红（真回归） | 2 failed：`test_claim1_production_features_carry_no_target_market_information` + `test_claim2_no_code_turns_the_odds_gate_on` |

该变异还暴露了原扫描的一个洞：只看 `ast.Assign` 会漏掉 dataclass 的带注解字段
默认值（`AnnAssign`），已补齐。

路径分隔符：相对路径统一走 `_rel()` → `as_posix()`，并有
`test_relative_paths_are_posix_under_both_path_flavours` 用 `PurePosixPath`
与 `PureWindowsPath` 两种语义各验一遍；CI 的 `windows-latest` 跑同一份测试。

## 局限

- `odds_sina.json` 被 gitignore、CI 运行时重抓，仓库内无法核验运行时实际
  首选公司；第 3 条判定依据取数选择代码 + 生产开关关闭两项独立事实。
- NJStats `openingHome` 的市场归属无法从仓库代码确立（第 5 条另列待核）。
- 本复核只读，未改任何生产代码/配置/状态；回退 = revert 本 PR 单一提交。
