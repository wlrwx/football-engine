# ⚠️ 本目录文档状态（2026-10-11 更新）

这五份报告是**当时的快照**，结论大体仍然成立，但其中最核心的一条论断
已经被后续工作推翻。读之前先看这里，避免重复劳动或被误导。

## 已被超越的论断

### ❌「6.2 万场收盘价已下载，但代码一列没读」

五份报告都强调这是最重要的发现，并写道：

```bash
grep -rn --include=*.py -E "AvgC?H|B365C?H|PSCH|IWCH|VCH" .
# → 零命中
```

**这个 grep 现在会命中 14 处**，集中在两个新建的模块：

| 文件 | 读的列 |
|---|---|
| `engine/backtest/historical_replay.py` | `AvgCH/CD/CA`（多公司收盘）、`PSCH/CD/CA`（Pinnacle）、`MaxCH` |
| `engine/backtest/residual_market.py` | 收盘−开盘修正、`MaxCH − AvgCH` 跨公司分歧 |

配套实验已跑完并落盘：

- `data/state/historical_replay.json` — E0：71,774 场、171 次重拟合、
  70,974 条配对预测。模型 vs 去水收盘市场 Brier Δ=+0.02018，t=+34.74，
  21 个联赛 FDR 校正后全部显著更差。
- E1（市场修正 vs 市场收盘）：Δ=−0.00006，t=−0.80，不显著。

结论方向没变（模型层无边际信息），但**证据已经从 955 场升级到 7 万场**，
且方法（严格 walk-forward）比报告里计划的更完整。

## 仍然成立的部分

| 论断 | 现状 |
|---|---|
| 6.25% 有效模型权重 | **成立**，本次优化后已在每条预测/账本记录落盘 |
| DJYY 长期断供、权重却挂着 | **成立**，本次已加运行时告警 + 逐场 `fusion_effective_weights` |
| `upgrade_tracker` 记账口径问题 | 已修（见 05 文档与 `scripts/upgrade_tracker.py`） |
| 熔断器三层失效 | 已修 `effective_mult` 接线，仍待补 parlay 桶与回撤触发 |
| 竞彩 overround ≈ 1.12（抽水 12.1%） | 成立 |
| L3 闸门缺口（odds 1.8–2.2 送钱区） | 成立，仍未处理 |

## 本次（2026-10-11 后续）新增的工程改动

见 `docs/EFFECTIVE_WEIGHTS_20261011.md`。

