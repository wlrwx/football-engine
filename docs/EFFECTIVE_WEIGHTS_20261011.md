# 有效权重落盘 + 死源显式化（2026-10-11）

## 问题

融合层在源缺失时会**重新归一化权重**，所以 config / `fusion_weights.json`
里写的不是真正参与运算的值：

```
名义 champion 权重   model 0.05 / market 0.75 / djyy 0.20
DJYY 断供时实际      model 0.0625 / market 0.9375 / djyy 0
```

这个差异带来三个后果：

1. **账本不可审计** — 拿到一条预测，只有 `model_raw` / `market_fair` /
   `djyy_prob` 三路原始值，看不出最终概率是按什么比例混出来的。
2. **死源完全静默** — DJYY 从 2026-08 中旬断供，558 场 v2 样本里只有 4 场
   有数据，但日志和配置都看不出任何异常（`djyy_weight` 一直写着 0.20）。
3. **评估层无法回答"模型贡献多少"** — 只能靠外部推算，而推算依赖"当时
   有没有 DJYY"这个账本没记的信息。

## 改动

### engine/prediction/fusion.py

`FusionResult` 新增两个字段：

```python
effective_weights: dict[str, float] | None   # 归一化后的实际比例
source: str                                   # 实际走的融合分支
```

`source` 取值：`fuse_three_way` / `fuse_two_way` / `fuse_djyy_only` /
`fuse_model_only`。裸模型分支 `effective_weights` 为 `None`（单源无需权重）。

**行为不变** — 只加返回字段，不动任何概率计算。老调用方全部兼容。

### engine/main.py

1. 每条 prediction 记录新增 `fusion_effective_weights` 与 `fusion_source`。
2. 运行末尾统计分支命中数；**DJYY 全场未命中时打显式告警**，并同时打出
   融合分支分布。断供当天就能从日志看出来。

### engine/review/post_match.py

`MatchReview` 新增同名字段（带默认值 `None` / `""`），结算时从
predictions.json 透传进账本。老账本记录读回不受影响。

### engine/selfcheck.py

新增第 6 项检查，把上面落盘的字段变成**每日自动告警**（这个脚本每次
预测/结算后都会跑）：

- **死源检测** — 名义权重非零但某源实际贡献恒为 0 → 告警。名义权重从
  `config/prediction.json` 读，读不到就不告警（不猜）。
- **覆盖率偏低** — 某源只在少数场次生效（<50%）→ 告警，区别于"完全死掉"。
- **模型有效权重过低** — 均值 <10% → 提醒"系统实质接近纯市场基准"。
- **审计缺口** — predictions 无 `fusion_effective_weights` 字段 → 告警。
- **分支分布** — 打印各融合分支命中数。

用当天 28 场真实预测注入"DJYY 断供"场景实测输出：

```
融合分支(2026-10-11): fuse_model_only=1 / fuse_two_way=27
⚠ 2026-10-11: 1/28 场合成赔率且 market_fair=None，完全绕过市场融合（裸模型概率）
⚠ 2026-10-11: DJYY 是死源——名义权重 0.30 但实际贡献 0（28 场全部为 0）
⚠ 2026-10-11: 模型有效权重均值仅 9.6%（名义 0.1）——系统实质接近纯市场基准
```

这三条在改动前**一条都不会出现**。

## 用法

账本里现在可以直接查：

```bash
python3 -c "
import json
for l in open('data/state/review_ledger.jsonl'):
    r = json.loads(l)
    w = r.get('fusion_effective_weights')
    if w: print(r['date'], r.get('fusion_source'), w)
"
```

## 验证

```
tests/test_fusion_effective_weights.py      7 个 —— 有效权重语义 + 各分支
tests/test_effective_weights_audit_chain.py 4 个 —— predictions→账本→读回闭环
tests/test_selfcheck_fusion_weights.py      6 个 —— 死源/覆盖率/权重告警
```

全套 `pytest tests/ -q` = **224 passed**（原 207），`ruff check --select F` 干净。

## 仍然没解决的（按优先级）

这些是本次评估发现、但**没有动**的问题，避免一次改太多。

> **订正（2026-10-11 复核）**：初稿把"熔断器看不到串关"和"熔断器无回撤触发"
> 列为待修，实为误判——这两项在同一天的另一个分支里已经修完并有回归测试。
> 下表是复核当前工作区后的真实状态。

| # | 问题 | 状态 | 位置 |
|---|---|---|---|
| 1 | L3 闸门缺口：odds 1.8–2.2 可下注 ROI −13.1%(n=247)，闸门却敞开 | ❌ 未修，需扩样本确认 | `config/strategy.json` `favorite_band.odds_below` |
| 2 | 熔断器看不到 parlay / score_parlay | ✅ 已修 | `main.py` 结算段 7b，独立结算 + 幂等守卫 |
| 3 | 熔断器无"距峰值回撤"触发条件 | ✅ 已修 | `circuit_breaker.py` `drawdown_warn_ratio` / `drawdown_halt_ratio` |
| 4 | `main.py` 两个千行函数（1225 + 955） | ❌ 未修 | god object，拆分风险高，建议单独 PR |
| 5 | 69MB 产物入库（data 48M + web 21M） | ❌ 未修 | `.gitignore` |
| 6 | 模型权重是否归零 | ❌ 待用户决策 | `config/prediction.json` |

### 关于 #1（L3 闸门缺口）

`data/state/ev_report.json` 的可下注口径（`stakable`，已过 `favorite_band` 闸门）：

| 赔率层 | n | ROI |
|---|---|---|
| L3 中(1.8-2.2) | 247 | **−13.1%** |
| L4 冷(2.2-3.0) | 173 | +0.2% |
| L5 深冷(≥3.0) | 41 | −3.8% |
| **合计** | **461** | **−7.3%** |

L3 独占几乎全部亏损，而闸门边界 `odds_below=1.8` 正好切在 L2/L3 之间
——把同为送钱区的 L2 挡住了，L3 敞开。

**但本次没有改**：n=247 的 ROI 置信区间较宽，且按 12.1% 抽水与模型
0.64% 相对优势的关系（差 19 倍），更根本的做法是先决定要不要继续在
竞彩价格上下注。扩闸门边界属于拿噪声改规则。

## 为什么不做「权重一致性 CI 门禁」

评估时考虑过加一个门禁：读 predictions.json 的有效权重，与 `config` /
`fusion_weights.json` 对不上就失败。**放弃了**——

源缺失导致的权重下降是**设计行为**，不是故障。DJYY 断供时权重下降，
系统应该正常跑并**告警**，而不是整个流水线挂掉。门禁会把正常情况变成
事故，逼着人去改配置把权重"凑齐"，那正好是这次要消灭的行为。

所以选的是告警而非门禁：让断供每天可见，但不阻断。

## 需要用户决策的一件事

`data/state/historical_replay.json` 的证据（71,774 场、t=+34.74、21 联赛
FDR 全显著）指向：**`model_weight` 应从 0.05 降到 0.00**。

本次优化**没有改这个值**，因为它会改变所有输出概率，属于策略决策而非
工程修复。当前名义权重下模型实际贡献 6.25%，改成 0 后系统变成纯市场基准
——按现有证据这是更优解，但它会改变你账本里之后所有记录的形态。

