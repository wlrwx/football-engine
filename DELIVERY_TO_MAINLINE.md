# 主线 agent 交接说明（2026-10-09 初版 / 2026-10-10 拆 PR 后更新）

分支：`pr-a-bugfixes`（基于 main `05a83536`）
定位：**纯 bug 修复 PR**。自治进化闭环是另一个 PR（`pr-b-evolution`），两者互不依赖。

```
54bd3592  fix: 修复两个自引入的测试回归，让 CI 转绿
1663de6e  docs: 主线 agent 交接说明（判定清单与回滚路径）
7af8db29  fix: 执行 draw_baseline 数据迁移（8 联赛）+ 回收 xg_calibration
a5135414  fix: 补齐剩余审计发现的落盘与自检缺陷
77cddfe8  fix: 修复 Dixon-Coles 量纲/符号 bug 与平局基线语义错误
```

改动规模：13 文件，+994 / −397。

**本文件只写「怎么判定」，诊断过程与实测数字见 commit message 和 `evolution/harness/known_issues.py`。**

---

## 一、判定优先级

按「修错的后果 × 不修的后果」排，不按代码位置排。

| 序 | 项 | commit | 不修的后果 | 修错的后果 |
|---|---|---|---|---|
| 1 | league_params 字段归属 | c2 | **AttributeError，预测流水线直接崩** | 无 |
| 2 | DC 量纲/符号 | c1 | xG 均值 +0.4 球，系统性高估 | 需重校 xg_calibration |
| 3 | 巴甲平局锚 | c1 | 巴甲平局概率恒定 0.5025 | 需观察巴甲回升 |
| 4 | ablation_replay 按链过滤 | c2 | 每次消融带 6% 系统偏差 | 无 |
| 5 | model 输入落盘 | c2 | 模型层无法离线裁决 | 无（纯新增字段） |
| 6 | 合成赔率标记+告警 | c2 | 裸奔场次不可见 | 无（纯新增） |
| 7 | 关 4 个后处理开关 | c1 | 熵减器持续生效 | 见下 |
| 8 | xg_calibration → 1.0 | c1 | DC 高估被双重下调 | **短期总进球偏高** |

**第 1 项是上一轮我自己引入的字段归属错误**（`draw_baseline_min_n` 放错类，`self.config.xxx` 引用会 AttributeError）。已在 c2 修正并用 ast 静态校验过字段归属，但**请主线独立复核这一处**，它是最容易在合并后炸掉的点。

---

## 二、必须逐项确认的事实前提

这些数字是我实测的，不是估计。判定前建议自己重跑确认：

```python
# 巴甲真实平局率（本次修复的依据）
import csv, collections
cnt, tot = collections.Counter(), collections.Counter()
for r in csv.DictReader(open("data/historical/matches.csv", encoding="utf-8")):
    if r["home_score"] in ("","NA") or r["away_score"] in ("","NA"): continue
    c = r["competition"]; tot[c] += 1
    if r["home_score"] == r["away_score"]: cnt[c] += 1
print("BRAZIL_SERIE_A", cnt["BRAZIL_SERIE_A"]/tot["BRAZIL_SERIE_A"], tot["BRAZIL_SERIE_A"])
# 预期 0.266, n=4652；代码里存的是 0.60
```

**1X2 天花板是收盘线，不是模型。** 独立回测（8 联赛 7904 场，严格时间切分）：市场收盘 Brier 0.5646；自写 MLE 版 DC 0.5798；最优融合 `w=0.20 → 0.5707`，总增益仅 0.0012。

这条很重要：**它决定了本次修复的期望收益上限**。DC 修好后模型权重也不该超过 0.20。如果有人想借这次修复大幅提高 model_weight，请用上面的数字反驳。

**accounting 口径**：账本 34 注已结算 pnl 合计 **−1129.21**，EV 校准体检无一正档位。修 Brier 不等于修 ROI——本次修复改善概率质量，但**不构成任何投注价值声明**。

---

## 三、逐项验收

### 3.1 字段归属（最高优先）

```bash
python - <<'EOF'
import ast
t = ast.parse(open("engine/learning/league_params.py", encoding="utf-8").read())
for n in t.body:
    if isinstance(n, ast.ClassDef) and n.name in ("LeagueParam","LeagueParamsConfig"):
        f=[x.target.id for x in n.body if isinstance(x,ast.AnnAssign) and isinstance(x.target,ast.Name)]
        print(n.name, f)
EOF
```
期望：`LeagueParamsConfig` 含 `draw_baseline_min_n` / `draw_baseline_tolerance`；`LeagueParam` **不含**这两个，含 `draw_strength_cap` / `draw_baseline_samples`。

然后实跑一次 `python engine/main.py`，确认无 AttributeError。

### 3.2 DC 修复

`tests/test_dixon_coles_dimension.py` 三条断言：防守方向、进攻单调、均衡场总进球 < 3.0。
另需确认 **xG 分布回落**（修复前 3.22，真实约 2.8）：随机配对 6000 场，总进球均值应 ≈2.70。

### 3.3 巴甲平局

`tests/test_draw_baseline_semantics.py`：平局概率不恒定、≤0.45、归一化。
迁移幂等：跑两次 `LeagueParamsManager(...)`，第二次 `summary` 应无修正条目。

**注意**：`_migrate_draw_baseline` 会**改写磁盘上的 `data/state/league_params.json`**。这是设计意图（错值必须落盘修正，只改代码不生效），但主线应确认这个副作用可接受。

### 3.4 ~ 3.6

纯增量，字段新增不删旧字段，风险低。确认 `predictions.json` 里新增的 `attack_home`/`market_is_synthetic` 有被下游消费（或明确标注为暂未使用）。

---

## 四、我认为你应当驳回或暂缓的部分

**不要因为「闭环 ACCEPT 了」就合入。** 闭环的 ACCEPT 分两类，本文件区分得很清楚：

- **backtest/invariant/audit 通道**（DC、combo_boost、死特征）：有独立证据支撑，可信度高
- **Brier 配对检验通道**（权重类）：本轮 37 个候选**全部 REJECT**，样本量不足以证明任何配置改动

第 8 项 `xg_calibration → 1.0` 我建议**单独成 PR**。理由：它是唯一一个「修 bug 会让某个指标短期变差」的改动，混在 bugfix 里会让回滚粒度变粗。若合并后总进球类指标（大小球/比分 top-N）明显恶化，应先回滚这一项而非整个 PR。

关 4 个开关同理——`combo_boost` 我有机制性证据（给 argmax 加分=纯熵减器），另 3 个（`market_draw_pull`/`league_draw_baseline`/`same_odds_bias`）只是 ablation delta≈0，**证据强度不同**，建议分开。

---

## 五、回滚路径

每个 commit 可独立 revert，无数据迁移、无 schema 破坏。

唯一有持久化副作用的是 `_migrate_draw_baseline` 会改写 `league_params.json`。若需回滚，用 git 恢复该文件：
```bash
git checkout main -- data/state/league_params.json
```

上线后观察项：
- **巴甲**（原命中率 31.6% vs 市场 63.2%）：预期回升。若仍无改善，说明平局锚不是主因，需要重新归因。
- **总进球类指标**：预期短期偏高（xg_calibration 回收所致）。若超出可接受范围，优先回滚第 8 项。
- **整体 Brier**：预期从 0.5638 小幅改善。**不要期待大幅改善**——见第二节的天花板约束。

若连续 3 个结算日 Brier 相对基线恶化 >0.002，立即整体 revert。

---

## 六、我留下的已知局限

1. **模型层仍不走 replay**。`attack/defense/form` 已落盘（c2），但**只对新预测生效**，历史场次无法回填。所以 `model_backtest.py` 的 walk-forward 仍是模型层的裁决通道。修好后会周期性切回 replay。

2. **候选池靠 adaptive 扩充**。粗网格 24 个跑完即穷尽，靠细网格/温度/校准三族撑住探索（round 2 新候选 0→18）。长期看仍需真正的增量候选生成。

3. **一处过时的 issue 条目已修正**。`PREDICTION-TIMING-BUCKET-NO-ASOF` 我原以为账本缺 as_of，实测 v2 链 517/517 全非空，8/16 就修好了。已改为「已核实历史遗留」。真实原因是决策时点过早（`PREDICT-TIMING-TOO-EARLY`，process 类，需前瞻实盘验证，**本次未做**）。

4. **未做的 3 条 process/strategy 类**：`NORTH-STAR-METRIC`（命中率→EV/ROI）、`PREDICT-TIMING-TOO-EARLY`、`LEAGUE-COVERAGE-GAP`。它们需要方向性决策或工程排期，强行改代码会引入新风险。

---

## 七、我自己的两次错误（供你判断我的结论可信度）

1. **`@property` 装饰器丢失**：导致 `is_issue` 变成方法而非属性，恒为真，50 个候选全走错通道。是靠「输出分布不对劲（NEEDS_DATA=41）」发现的，不是靠读代码。
2. **不变式 `_res` 语义翻转**：ok/violated 传反，全部标志反转。同样是靠实跑数据发现。

共同点：**这类错误静态检查和读代码都发现不了，只有让机器跑真实数据才暴露**。所以建议主线合并前至少跑一遍 `python -m evolution.cycle --rounds 2`，看输出分布是否合理（ACCEPT 应集中在 issue 类，权重类应全 REJECT）。
