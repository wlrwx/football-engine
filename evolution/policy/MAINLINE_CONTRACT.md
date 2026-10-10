# 主线 Agent 裁决契约

这是自动进化闭环与主线 agent 之间的**唯一接口**。
主线不需要读代码，只读 `verdict_round*.json`。

---

## 0. 闭环现在的能力（2026-10-09 更新）

闭环分三层，主线只需关心「它这次动了什么」：

| 层 | 能力 | 产出 |
|---|---|---|
| **发现** | 诊断引擎从账本/预测落盘**主动挖问题**（不再只复读静态清单） | `verdict_round*.json` 的 `diagnostic/*` 候选 |
| **修复** | 对可自动修复的代码缺陷在真实源码上打补丁 + 提取函数执行验证 | `autofix_*.json` + `patch_*.patch` |
| **交付** | 推分支 + 开 PR（GitHub Actions 上） | GitHub PR |

诊断发现的「事实性错误」（如巴甲 draw 钉死、概率非法）标 `auto_mergeable`，
走 audit 通道直接 ACCEPT，不等统计显著性。

---

## 1. 文件约定

每轮产出（`evolution/demo/out/`）：

| 文件 | 用途 |
|---|---|
| `verdict_round<N>.json` | **裁决输入**（机器读） |
| `pr_body_round<N>.md` | 人类可读说明（PR 描述） |
| `evolution_report.json` | 完整轮次历史 |
| `commands_round<N>.sh` | 合入/回滚命令提示 |

---

## 2. 裁决流程

```
读取 verdict_round1.json
  ↓
selfcheck.passed == true ?
  ├─ false → 直接拒绝，并要求人工修复（见 §4）
  └─ true  ↓
verdict == "ACCEPT_NONE" ?
  ├─ 是 → 确认闭环健康，结束（不应产生 PR）
  └─ 否 ↓
逐个复核 accepted[]：
  ① replay 忠实率 ≥ 0.95？
  ② |train Δ| ≥ min_delta 且 p ≤ α？
  ③ val 段未反向恶化？
  ④ 改动语义合理？（这是唯一需要「人类判断」的环节）
  ⑤ 回滚路径可执行？
  ↓
全部通过 → 合入；任一不通过 → 拒绝并说明
```

### 第 ④ 项：语义合理性检查清单

数据只能告诉你「有效」，不能告诉你「正确」。以下情况即使统计通过也**应当拒绝**：

- 改动只在某个小联赛生效，且该联赛样本 < 50
- 改动效果几乎全部来自 val 段（`val_delta` 显著优于 `train_delta`）→ 过拟合
- 改动引入了一个新的手调常量（系统已经因硬编码常量出过问题，见 `LEAGUE_DRAW_ANCHOR`）
- 改动与先验矛盾（`prior=likely_bad` 却通过了统计检验）→ 需人工解释

---

## 3. 为什么闭环会输出 ACCEPT_NONE

**这是正常且期望的，不应视为故障。**

在当前 452 场可信样本上，融合链的任何配置改动的效应量都远低于噪声：
最好候选 `ΔBrier = -0.0003`（t=-0.21，p=0.42），门槛是 `-0.0015`。

把 `min_delta` 一路降到 0，仍无候选能同时满足 `p ≤ 0.10`
—— 说明这不是门槛过严，而是**当前样本量不足以证明任何改进**。

一个只会说「PROPOSE」的进化闭环是危险的：它会在噪声里不断找到
「显著」的小改动并合入，最终过拟合账本。

---

## 4. 自检失败时的处理

| 症状 | 含义 | 处理 |
|---|---|---|
| `replay_trusted_frac < 0.9` | 账本输入未充分落盘，或代码与账本不同步 | **停止进化**，先修落盘字段 |
| `baseline_brier` 越界 | replay 或账本严重异常 | **停止进化**，人工排查 |
| 样本量不足（`n < min_train_n`） | 账本太新 | 等待，不做任何改动 |
| 跨链污染 | 账本混入旧版融合链的行 | 检查 `scope_current_chain` |

**铁律：自检失败时闭环不产出任何 PR。** 在错误的测量基础上做自进化，
比不自进化危险得多。

---

## 5. 何时应该允许合入

`min_delta = 0.0015` 对应约 **+2pp 命中率**（在本项目命中率/Brier 换算下）。
低于此的改动即使统计显著也不合入，理由：

- 边际收益会被结算噪声淹没
- 增加系统复杂度与未来维护成本
- 累积起来会侵蚀模型的过度自信校准

若某类改动确有小但确定的收益（如修复事实性错误），
应当走**独立路径**：先写失败测试证明 bug 存在，再修复，
而不是依赖 Brier 的统计显著性。

---

## 6. 合入后必须做的事

1. 打 tag 记录变更：`git tag -a evolution-<run_number> -m "<cid>"`
2. 下一个结算周期后，检查实测 Brier 是否仍在基线 ±0.002 内
3. 若恶化，立即按 `commands_round<N>.sh` 的反向操作回滚
4. 把回滚结果写回 `evolution_report.json` 的 `rollbacks` 字段 —— **闭环需要记住自己的失败**

---

## 7. 反馈回路（已实现）

主线拒绝一个候选时，把裁决写入 `mainline_rejections.json`，闭环下次运行会读它：

```json
{ "rejections": [
    { "cid": "w_m0.10_k0.75",
      "reason": "val 段不稳定，市场已是更强预测器，本季度不调融合权重",
      "suppress_family": true,
      "decided_by": "mainline",
      "ttl_rounds": 15 }
] }
```

运行：`python -m evolution.cycle --rejection-file mainline_rejections.json`

| 字段 | 作用 |
|---|---|
| `cid` | 抑制该具体候选（按**内容指纹**匹配，改 seed 也绕不过） |
| `suppress_family: true` | 抑制**整个族**（如否决所有 `fusion_weight`，下一轮 20 条自动不再提） |
| `reason` | 必填。PR 里会展示抑制原因 —— **主线需要知道闭环为什么沉默** |
| `ttl_rounds` | 到期自动解禁（默认 10 轮）。**否决不是永久的**：新证据出现后应重新评估 |

删除条目 = 撤销否决。指纹匹配忽略 cid 措辞，只看
`(family, patch)` 的 SHA256，因此换个说法描述同一改动无法绕过。

---

## 8. 事实性缺陷走独立通道

以下几类**不应走 Brier/统计显著性**，因为它们是**定义错**而非**效果差**：

| 类型 | 例子 | 正确做法 |
|---|---|---|
| 语义错 | `DRAW-ANCHOR-JUDGMENT-AS-RATE`（判平精度当平局率） | 先写失败测试证明 bug，修完测试转绿 |
| 量纲错 | `DC-ATTACK-LOG-DIMENSION`（比例因子进对数域） | 方向/单调性测试 + 分布合理性检查 |
| 死参数 | `DEAD-FEATURES-INJURY-REST`（方差为 0 的特征） | 不变式：常量特征不应有非零权重 |
| 熵减器 | `COMBO-BOOST-ENTROPY-REDUCTOR`（给 argmax 加分） | 不变式：不给当前最优方向加分 |

这四类在 `known_issues.py` 中标记 `auto_mergeable: true`，
裁决走 `judge_issue()` 的 invariant / backtest / audit 通道，**不进 BH-FDR**。

理由：若强行用 Brier 裁决「DC 量纲错误」，系统会因为「市场权重占大头、
改动看不出效果」而永远不修一个已确认的 bug。让数据裁决定义错误，
等于把正确性交给运气。

---

## 9. 已知的闭环局限（请主线知晓）

1. **模型层暂不走 replay**：`attack/defense/form` 未逐场落盘，
   重放忠实率仅 40.5%（corr 0.28 DC / 0.58 MC）。模型层改用自包含
   walk-forward（`model_backtest.py`），已在 3 个时间切分上验证 DC 修复
   带来 +0.0175 Brier。**待 `MODEL-REPLAY-UNREPRODUCIBLE` 修完后
   可切回 replay 通道。**
2. **`xg_calibration` 全部置 1.0**：修好 DC 后，原本给 DC 高估打补丁的
   系数失效，需重新校准。在重校完成前不过度下调，最终值由每周回测裁决。
3. **候选池靠 adaptive 扩充**：粗网格 24 个跑完即穷尽，
   现补充细网格/温度/校准三族（round 2 新候选 0 → 18）。
