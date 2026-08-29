# 比赛情境数据层（2026-08-30，store-only）

## 目标
平局盲点（实际平局率 25.8%，判别力≈0）的唯一突破口是**情境信号**：
动机（stakes）、伤停、阵容收缩、裁判、休息天数。本层先把数据落盘累积，
**不参与预测**——样本够了用账本验证，数据说话再决定入模。

## 数据来源与成本
| 字段 | 来源 | 额外API成本 |
|---|---|---|
| stakes 动机/情境 | DJYY comparison（本来就在抓） | 0 |
| referee / weather / coach | DJYY info（本来就在抓，此前只用了伤停） | 0 |
| injuries / rest_days | 已有（此前影响模型但未落盘） | 0 |
| 首发阵容（阵型/首发攻击手数） | DJYY lineups（新增） | 每场每日1次（context_cache.json 门控） |

## 落盘位置
- `data/daily/<date>/predictions.json` 每场新增 `context` 字段（store-only）
- `data/daily/<date>/context_cache.json` 阵容抓取缓存（防 30 分钟一轮重试打爆接口）

## 验证计划（样本累积后）
1. 情境覆盖率体检（stakes/lineups 抓到率）
2. 平局信号挖掘：stakes 分组 × 实际平局率；5-4-1 等收缩阵型 × 平局率；
   缺阵攻击手数 × 总进球/平局率
3. 显著且样本充分的信号 → 走 draw_strength/league_params 现有通道入模
   （概率层 config 开关 + ablation_replay 回放裁决，与前几轮同一纪律）
