# DJYY 深挖报告 #2 —— 数据驱动验证（268 场回测）

> 深挖日期: 2026-08-11 | 深挖人: OpenSquilla | 执行人: Codex
> 数据: djyylive.com 免费 API 历史拉取 + djyydata.com 逆向 + GitHub 源码
> 用途: 为 football-engine 融合权重 / 市场信号 / 策略设计提供**实测依据**

---

## 0. 结论速览（TL;DR）

| # | 发现 | 影响 | 建议 |
|---|------|------|------|
| 1 | DJYY 模型整体不如 Pinnacle 市场（Brier 0.579 vs 0.535，268 场） | 本地 `djyy_weight=0.15` 固定权重**是负贡献** | 改条件融合（仅高置信才加权）或降权 |
| 2 | 模型与市场分歧时（47 场）：市场 48.9% vs 模型 19.1% | 模型不能单独信任 | 市场方向优先，模型仅做确认 |
| 3 | 模型高置信区（p>0.55）有增量：条件融合 Brier 0.5344 < 纯市场 0.5349 | 高置信时模型有微弱正贡献 | 门槛 0.50、权重 0.3 条件融合 |
| 4 | 市场移动信号有正预测力但弱（涌入主队命中 56.5% vs 基线 46.8%） | 本地 ±0.02 启发式方向对、幅度拍脑袋 | 需分桶校准，勿强移动追单 |
| 5 | DJYY 商业策略 = floor/current/gap 三要素 + 完整结算 | 本地 EV 策略缺"赔率下限"概念 | 引入 odds floor 机制 |

---

## 1. 数据与方法

- **样本**: 2026-07-27 ~ 2026-08-10 两周，djyylive.com fixtures + comparison API
- **有效场次**: 268 场已完场（1X2 有完整 model 概率 + Pinnacle 初盘/即时盘）
- **口径**: Brier score（三分类，越低越好）、分歧分析（模型 vs 市场看好不同方）
- **局限**: 两周样本、夏季联赛、无世界杯/欧冠强度；结论方向性可信，幅度需更长周期复核

## 2. 核心发现

### 2.1 DJYY 模型 vs Pinnacle 市场

```
【A】校准对比（268 场）
  DJYY模型   Brier = 0.5793
  Pinnacle   Brier = 0.5349   ← 市场更准
  差距 4.4 点

【B】分歧分析（47 场模型/市场看好不同方）
  模型方对: 19.1%
  市场方对: 48.9%            ← 市场完胜
```

**解读**: DJYY 的 statistical-model 是第三方/自有模型概率，整体校准不如 Pinnacle 赔率去水。这与行业共识一致——**收盘赔率是最强的单一预测器**。

### 2.2 融合权重敏感性（重要！）

```
固定权重（当前本地配置 djyy_weight=0.15）:
  w=0.00 (纯市场):  Brier = 0.5349   ← 最好
  w=0.15 (本地配置): Brier = 0.5373   ← 比纯市场差！
  w=0.30:           Brier = 0.5411
  w=0.50:           Brier = 0.5487

条件融合（仅当 max(p_home,p_away) > 阈值 才给模型 w=0.3）:
  门槛 0.45: Brier = 0.5359
  门槛 0.50: Brier = 0.5344   ← 略优于纯市场
  门槛 0.55: Brier = 0.5351
```

**结论**: 本地 `djyy_weight=0.15` 固定融合是负贡献。改为**条件融合**（模型高置信 >0.50 时给 0.3 权重，否则纯市场）可微弱改善。注意改善幅度小（0.0005），可能被噪声淹没，但**至少不应固定 0.15**。

### 2.3 市场移动信号（初盘→即时盘）

```
资金涌入主队 (主胜赔率降>2% 且 客胜升>2%): 26/46 = 56.5%
资金涌入客队:                               21/49 = 42.9%
基线: 主胜 46.8% / 客胜 31.8%

主胜赔率移动分桶 vs 主胜实际命中:
  强降<-5% : 41.7%  (12场)  ← 强移动反而不准！
  降-5~-2%: 65.0%  (20场)  ← 温和移动最准
  平±2%   : 45.2%  (93场)
  升2~5%  : 50.0%  (10场)
  强升>5% : 36.8%  (19场)
```

**解读**: 
- 温和移动（-5%~-2%）有正信号，但**强移动（>±5%）反而弱/反向**——可能被博彩公司主动调盘（诱盘）或大额资金左右。
- 本地 main.py 的 ±0.02 概率修正方向对，但**未按幅度分桶**，对强移动同样 +0.02 是错误。
- 建议: 移动信号仅取温和区间（2%~5%），强移动应视为"市场噪音"。

### 2.4 DJYY 模型概率单调性

```
p_home 分桶 vs 实际主胜率（模型）:
  <0.30: 15.0% | 0.3-0.4: 30.8% | 0.4-0.5: 56.2% | 0.5-0.6: 66.7% | >0.60: 54.5%
p_home 分桶 vs 实际主胜率（市场）:
  <0.30: 15.8% | 0.3-0.4: 28.0% | 0.4-0.5: 65.7% | 0.5-0.6: 52.2% | >0.60: 72.7%
```

- 模型单调性尚可（0.15→0.67），但 **>0.60 高置信区掉头**（54.5% vs 市场 72.7%）
- 市场在最高置信区最可靠 → **模型高置信 ≠ 市场高置信，市场高置信可信度更高**

## 3. DJYY 商业策略逆向（djyydata.com）

### 3.1 数据文件（公开可拉，滚动窗口）
```
GET https://djyydata.com/en/api/dj/model-data/value-picks.json        ← 1X2/平局价值
GET .../value-picks-cards.json                                        ← 牌数大小
GET .../value-picks-corners.json                                     ← 角球大小
```

### 3.2 策略结构（核心三要素）
```json
{
  "home": {"zh":"滨海布洛涅"}, "away": {"zh":"南锡"},
  "sel":  {"zh":"平局价值"},
  "floor": 3.2,      // 模型算出的价值下限赔率
  "current": 3.2,    // 当前市场赔率
  "gap": 0,          // gap% = (current-floor)/floor*100，>=0 才推荐
  "won": 1,          // 结算: 1胜 0负
  "push": false,     // 走水
  "score": "0:0", "settled_at": "..."
}
```

**要点**: DJYY 只推 `current >= floor`（gap>0）的场次——即**市场赔率 ≥ 模型认为的公平赔率**时才出手。这就是"odds floor"机制，本地 EV 策略没有这个概念（本地是 EV>阈值就推，无赔率下限过滤）。

### 3.3 当前窗口 track record（8/9-8/11，20 单）
- 1X2 平局价值: 1/1 中
- 角球小: 12/18 = 67%（主打策略！）
- 牌数大: 0/1
- 注意: 单窗口样本小，仅作结构参考，非长期业绩

### 3.4 页面诚实披露
djyydata.com value-model 页明示: "Backtested history — not a forecast of future returns. The signal is intermittent and negative in some seasons."——负赛季也公开，公信力做法，可借鉴到本地站点。

## 4. GitHub 方法论补充（源码级）

### 4.1 JetQiao OOF 防泄漏（关键约束）
```python
# 整天预测完成后，才把当天赛果加入历史
for match_date, day_rows in sorted(by_date.items()):
    ...
    # 用 history[:当天] 预测当天全部
    # 全部预测完，才 extend(history, 当天结果)
```
本地 walk_forward 已有类似逻辑（ts_split 按时间切分），但可核对是否做到"整天批量"而非逐场混入。

### 4.2 epl-predictor 双参数调参
- `tune_xi.py`: 保序时间序列 CV 扫最优衰减 ξ（引用 artiebits 方法）
- `tune_rho.py`: ρ 联合调参（参考 Dixon-Coles 论文 -0.13~-0.18）
- 本地有 rho_fitter（黄金分割）和 time_decay，但缺"ξ 保序 CV 扫描"→ 可补

### 4.3 wc2026-hier 贝叶斯先验
- att/def ~ Normal(prior_mu, prior_sigma)，prior_mu 由 **Elo + 阵容强度线性投影**得到
- 稀疏球队（库拉索/佛得角）有合理收缩目标 → 本地 shrinkage_dc 可升级此思路

## 5. 可落地建议（给 Codex）

### P0（强烈建议，有数据支撑）
1. **改 DJYY 融合为条件融合**: 模型置信>0.50 才给 w=0.3，否则纯市场。参考 `engine/prediction/fusion` 现有融合点。
2. **市场移动信号分桶**: main.py 中 ±0.02 修正改为按幅度分桶（温和 2-5% 才修正，强移动>5% 不修正或反向警示），页面 chips 同步。

### P1（高价值）
3. **引入 odds floor 机制**: EV 策略加"赔率下限"过滤——`current_odds >= model_fair_odds * 1.02` 才推（对齐 DJYY floor/gap 概念）。
4. **DJYY 角球数据接入**: DJYY comparison 有 totals/角球市场，可验证"角球小"策略在本数据集的适用性（本地 league-matrix 有 avg_corners）。
5. **ξ 保序 CV 扫描**: 复用 epl-predictor 方法为 time_decay 定参。

### P2（参考）
6. 页面加 track record 诚实披露（负赛季也展示，学 DJYY 公信力）
7. 每日 DJYY picks 抓取存档（滚动窗口会覆盖，建议定时拉取留存）

## 6. 数据文件（Codex 可复查）
```
/tmp/djyy_bt.json    # 7/27-8/3 场次（115）
/tmp/djyy_bt2.json   # 8/4-8/10 场次（154）
/tmp/djyy_hist_cmp.json
/tmp/djyy_value-picks*.json
```
分析脚本在 /tmp/*.py 内联，可重新生成。

## 7. 补充：JetQiao 分层收缩完整方案（P2 升级参考）

源码 `/tmp/jetqiao/src/football_prediction/modeling/dixon_coles.py`:

```python
# 收缩权重: 低样本球队似然加权放大 → L2 惩罚更重 → 向 0 (联赛均值) 收缩
shrinkage_weights = 1.0 + prior_matches / max(1, team_match_counts[team])
#   prior_matches=16, 球队打2场 → 权重9.0; 打16场 → 2.0; 打80场 → 1.2

# 目标函数三件套:
loss -= weight * log_likelihood          # 时间衰减 exp(-decay*age), decay=0.0025
loss += 100 * mean(attack)**2            # 攻击参数强中心化
loss += reg * sum(sw * attack**2 + sw * defence**2)   # 分层 L2 收缩

# rho 无约束参数化
rho = math.tanh(params[-1]) * 0.2        # 限制在 (-0.2, 0.2)
```

本地 `shrinkage_dc.py` 已有分层收缩雏形，缺: ① 时间衰减 decay ② rho tanh 参数化 ③ 攻击中心化硬约束 → 可作 P2 升级。

## 8. 补充：cnemri 防泄漏特征范本（本地 lgbm 可对照）

源码 `/tmp/cnemri/src/wc2026/features.py`:
- 按日期排序，逐行: **读状态→建特征→只有已完场才更新状态**（未开赛跳过）
- Elo 更新: `k = get_k_factor(tournament) * margin_multiplier(净胜球)`（K 因子按赛事分级，非固定）
- momentum = 当前 elo - window 前 elo
- 新队默认 rolling (1.2, 1.2, 0.35)、rest_days 30、elo INITIAL
