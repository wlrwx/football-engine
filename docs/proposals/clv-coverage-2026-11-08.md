# CLV 面板覆盖与匹配失败：只读量化 + 提高覆盖预登记草案（2026-11-08）

- 触发：`dykily/football-engine` Issue #7（每日探索 · scout）。
- 复核基线：上游 `wlrwx/football-engine` main `d7247f6`（2026-11-07）。
- 样本：账本 `data/state/review_ledger.jsonl` 中 `best_selection` 非空的下注
  **1404 注**，日期 2026-07-19 ~ 2026-11-06。
- fdc 快照：本工单目录内重跑 `scripts/fetch_football_data.py`（2026-11-08），
  61710 行、latest 2026-11-06、42/45 文件成功（`2526_SW1`/`2627_SW1` HTTP 300，
  `2526_F2` 本次超时）。CSV/parsed.jsonl 不入 git，数字以当次抓取为准。
- **只读**：新增 `scripts/clv_coverage_audit.py` 不改 `clv_pinnacle.py`、不写
  `data/state/`、不改任何门槛/判据。顶层分类顺序与生产一致，**同一份输入下
  审计的 `stats` 与面板 `clv_pinnacle.json` 逐项相等**（本次实测相等，见下）。

## 复现

```bash
python scripts/fetch_football_data.py          # 刷新 fdc 缓存（parsed.jsonl 不在 git）
python scripts/clv_coverage_audit.py --out /tmp/clv_audit.json
python -m pytest tests/test_clv_coverage_audit.py -q
```

审计脚本复用 `clv_pinnacle` 的 `LEAGUE_MAP / _resolve_team / _name_match /
pick_close`，保证口径一致；`what-if` 段只进 `recoverable`，**绝不改顶层 stats**。

## 1. 面板现状（已证实，生产 == 审计）

| 桶 | 注数 | 占比 |
|---|---|---|
| ok（已匹配 CLV） | 758 | 54.0% |
| `league_uncovered` | 486 | 34.6% |
| `no_match` | 114 | 8.1% |
| `no_en` | 46 | 3.3% |
| `no_close` | 0 | 0% |
| 合计 | 1404 | 100% |

证据：`data/state/clv_pinnacle.json` `stats`；审计 `stats` 与之逐项相等。
分桶代码：`scripts/clv_pinnacle.py:187`（league_uncovered）、`:205/:211`（no_en）、
`:223`（no_match）。

## 2. 逐桶成因（已证实）

### 2.1 `league_uncovered` 486 —— 结构性，fdc 源不覆盖，**可回收 0**

按联赛：欧冠 99、欧罗巴 75、K1联赛 54、欧国联 52、沙职 27、亚冠精英 24、
英联赛杯 20、解放者杯 20、荷乙 18、亚运男足 15、国际赛 14、巴西杯 13、澳超 10、
德国杯 7、日联赛杯 7、亚运女足 7、欧协联 5、亚冠乙 4、韩职 3、意大利杯 3、
英锦标赛 3，其余各 1。

这些是**杯赛 / 洲际 / fdc 未收录的联赛**，`LEAGUE_MAP`（`clv_pinnacle.py:38`）
没有映射是因为 fdc 根本没有对应文件，不是映射漏写。**当前 fdc 源下无可回收
部分**；要覆盖只能换/加数据源（超出本工单，见 §5 推测）。

### 2.2 `no_en` 46 —— 两类，其中 6 注已证实可回收

| 子成因 | 注数 | 说明 |
|---|---|---|
| `team_unresolved` | 40 | 中文队名解不出英文，13 个队名（见下表） |
| `prediction_id_format` | 6 | **账本 `match_id` 缺日期前缀**，predictions 里有对应行 |

- **6 注格式漂移（已证实可回收）**：2026-08-01/02 的账本行 `match_id` 形如
  `周六006`，而 `data/daily/<date>/predictions.json` 里是 `2026-08-01_周六006`。
  `clv_pinnacle.py:199` 用 `taken_cache[date].get(match_id)` 精确查，查不到 →
  taken 为空 → 记 `no_en`。补日期前缀即可命中（审计样本
  `prediction_id_format` 给出归一化后的 id）。
- **40 注队名未解析（候选，回收不确定）**：13 个队名——赫尔辛基火花 9、大宫松鼠 6、
  福图纳 6、莱红牛 5、秋田闪电 4、仙台七夕 4、甲府风林 3、卡斯鲁厄 2、
  枥木城 1、鸟栖沙岩 1、札幌冈萨 1、宫崎牛鸟 1、南锡 1。多为日乙/芬超/德乙队，
  应走既有 `scripts/verify_team_zh_en.py` 的「候选 + 同日同联赛交叉验证」补字典，
  **不是纯手工映射**。能否回收取决于 fdc 是否有该场（日乙覆盖不全，见 §2.3）。

### 2.3 `no_match` 114 —— 大头是**日期口径**与**队名归一**，可回收 99

| 子成因 | 注数 | 可回收性 |
|---|---|---|
| `name_mismatch`（同日有行、队名对不上） | 75 | 51 注可回收（25 日期 + 21 转写 + 27 别名，见下） |
| `no_fdc_rows_for_date`（该日该文件无行） | 28 | 26 注日期漂移可回收，2 注日乙缺场 |
| `cache_stale_for_date`（源文件未更新到该日） | 11 | 全部 2026-11-06，源站滞后，刷新后或自愈（推测） |

**已证实的可回收损失（99 注）：**

1. **日期漂移 51 注（已证实为同一场比赛）**：同日匹配失败，但 fdc 在 **±1 天
   （本样本全部为 +1 天）** 有**完全相同的两队**。联赛分布：美职 33（美职 23 +
   美职联 10）、巴甲 17、挪超 1——集中在美洲联赛，与「竞彩北京日期 vs fdc 当地
   日期」的时区差一致。逐注核验：30 注归一化后队名字串完全相等，21 注为同一队
   的短名/全名（Chapecoense↔Chapecoense-SC、LA Galaxy↔Los Angeles Galaxy、
   Columbus↔Columbus Crew 等），**无一跨队误配**。
   例：账本 `2026-07-22_周三206` 美职联「迈阿密国际 vs 芝加哥」→ fdc `new_USA`
   `2026-07-23 Inter Miami vs Chicago Fire`。
   根因：`clv_pinnacle.py:214-224` 只查 `fdc_index[(fname, date)]` 同日。
2. **重音转写 21 注（what-if 已验证场次存在）**：`_norm`（`clv_pinnacle.py:61`）
   用 `[^a-z ]` **删掉**重音字母而非转写——`Marítimo→"mar timo"`、
   `Lillestrøm→"lillestr m"`、`Málaga→"m laga"`，与 fdc 的 `Maritimo/Lillestrom/
   Malaga` 对不上。模拟转写（NFKD + ø/ß/æ 显式映射）后这 21 注全部 score≥0.5。
3. **别名 27 注（what-if，需交叉验证后才可上线）**：`Depor↔La Coruna`（11）、
   `Sporting CP↔Sp Lisbon`（8）、`Cologne↔FC Koln`（8）。已逐场核对 fdc 当日
   **确有该对手**（如 `2627_SP1 2026-08-17 La Coruna vs Elche`）。别名是主观映射，
   **必须走 `verify_team_zh_en.py` 交叉验证**，错一个就把 A 队收盘价配到 B 队。

**不可回收 / 待观察（15 注）：** `cache_stale` 11（2026-11-06，源文件 latest 停在
11-01~11-04，刷新后大概率自愈——推测）；日乙缺场 3（`new_JPN` 对 J2 覆盖不全，
部分队名在、部分场次缺）；巴甲 1（2026-11-06 Vitoria vs Fluminense，fdc 暂无该场）。

> **子集误配警示**：`_name_match` 用 `交集/min(长度)`，`Los Angeles FC` 与
> `Los Angeles Galaxy` 都得 `{los,angeles}`/含 `{galaxy}` 可能打满分。本次 51 注
> 日期漂移已逐注人工核验无跨队误配，但**生产引入 ±1 天匹配时必须配更严的校验**
> （如归一化全等，或复用交叉验证），不能只凭 score≥0.5。

## 3. 回收潜力汇总（分清已证实 / what-if / 推测）

| 项 | 注数 | 置信 | 上线前置 |
|---|---|---|---|
| match_id 补日期前缀 | 6 | **已证实** | 无（纯格式归一） |
| ±1 天日期匹配 | 51 | **已证实同场** | 需更严队名校验防子集误配 |
| 重音转写 | 21 | what-if 已验证 | 作为**fallback**，不改已有匹配 |
| 别名字典 | 27 | what-if | **必须交叉验证** |
| 队名字典补全（13 名） | 40 | 候选，不确定 | 交叉验证 + fdc 有该场 |
| 源站滞后自愈 | 11 | 推测 | 仅需刷新 |

仅前三项（**高置信 78 注**）：ok 758 → **836（54.0% → 59.5%）**。
再加别名 27：→ **863（61.5%）**。队名补全 40 注为额外上行空间、不计入保底。

`no_match` 114 的去向核对：51（日期）+ 48（转写 21 + 别名 27）+ 11（源滞后）
+ 3（日乙缺场）+ 1（巴甲待观察）= 114。

## 4. 预登记草案（供人审后登记进 `docs/EXPERIMENTS.md`，本文件不代登记）

> 按仓库规矩，实验开工前需要人在 `docs/EXPERIMENTS.md` 登记一行并写死判定标准。
> 以下是草案，**未登记、未实施**；E8/E10 正按冻结判据跑样本，本草案不改任何
> 模型特征 / 门槛 / 上界 / 校准参数。

- **假设**：CLV 面板覆盖损失主因是「日期口径 + 队名归一」，而非源数据缺失；
  以**fallback**方式修复可在**不改动任何已匹配样本 CLV 值**的前提下，把覆盖率从
  54.0% 提到 ≥59.5%。
- **分支**：`agent/clv-coverage-match-failure-290173113319`（本 PR 只含审计脚本 +
  测试 + 本文档；匹配逻辑改动在登记后另开分支）。
- **干预**（全部为「同日匹配失败才触发」的 fallback，保证已有 758 注逐位不变）：
  1. `match_id` 缺日期前缀时补前缀重查 predictions；
  2. 同日无匹配 → ±1 天重查，且要求归一化队名全等（或交叉验证）防子集误配；
  3. 同日/±1 天仍失败 → 重音转写归一重试；
  4. 别名仅经 `verify_team_zh_en.py` 交叉验证通过后启用。
- **主指标**：覆盖率 `ok/bets`（面板 `stats`）。
- **护栏（硬）**：已匹配 758 注的 `(taken, close, benchmark, clv)` **逐位不变**；
  新增匹配全部来自原 `no_en/no_match`；不触碰 `engine/backtest/`、校准、门槛。
- **判定日 / 样本量**：建议以再积累 ≥2 周新注（预计 ≥150 注）后判定；判定标准
  登记时写死——覆盖率提升的 95% 置信区间下界 >0，且护栏零违例。**样本不足则明说
  不足，不把噪声当信号。**
- **失败处理**：若护栏发现任何已匹配样本 CLV 变化，立即回退该 fallback。

## 5. 推测 / 超出本工单（不作为本轮结论）

- `league_uncovered` 486 注要覆盖需引入 fdc 之外的数据源（欧冠/欧罗巴/K联赛/杯赛），
  成本与授权另议，本轮不展开。
- `cache_stale` 11 注是否真自愈，需下一次刷新后复查确认。
- 日乙（J2）在 `new_JPN` 的覆盖边界需向源站核实，本轮仅观察到「部分队名在、
  部分场次缺」。

## 6. 本轮结论

**有可行动发现**：高置信可回收 78 注（覆盖率 54.0%→59.5%），另有别名 27 注与
队名补全 40 注的上行空间。最大桶 `league_uncovered`（34.6%）为源结构性缺失，
当前 fdc 源下无可回收部分——不硬凑。全部数字可由上文「复现」节命令重跑。