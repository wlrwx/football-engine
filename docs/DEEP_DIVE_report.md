# 深挖报告：DJYY 数据体系 + GitHub 足球预测方法论（2026-08-11）

> 深挖人：OpenSquilla（研究侧）。本报告只提供**方法论 + 可落地建议**，代码实现由 Codex 负责。
> 范围：djyylive.com / djyydata.com 全量探测 + GitHub 新增 2 仓库（0xNadr/wc2026、raynergoh/EPL-Predictor）源码分析，叠加此前已深挖的 JetQiao / cnemri / Hicruben。

---

## 一、DJYY 数据体系（djyylive.com）— 免费 API 全图谱

DJYY = 「赛前概率模型对比机构赔率、实时赛况、球队球员高级数据」React SPA，Cloudflare 托管，数据源 **SportMonks + 自有 djyy-elo-model**。公开 API 无鉴权，全部实测可用：

| API | 能力 | 对 football-engine 的价值 |
|---|---|---|
| `/api/leagues/fixtures?date_from=&date_to=&category=` | 比赛列表：中/英队名、SportMonks 队 id、联赛、比分、**xG**、has_odds | 现已在用，可补 category 全量（tier1+euro+tier2+other+world） |
| `/api/match/{id}/comparison` | **模型概率**（p_home/p_draw/p_away + BTTS + 大小球1.5/2.5/3.5/4.5）vs **Pinnacle 即时盘**（raw_odds + probs + handicap + totals）vs **Pinnacle 初盘**（opening_raw_odds）| ⭐ 初盘/即时盘对比 = 市场移动信号，本地未用 |
| `/api/match/{id}/info` | 教练（含近期战绩）、**裁判（场均黄牌/红牌/总牌数）**、天气（风速/湿度/云量）、场地（草皮类型/容量） | ⭐⭐ 裁判/天气/场地全是本地没有的特征 |
| `/api/match/{id}/team_form?limit=5` | 主客队近 5 场：对手、主客、比分、**xG**、联赛 | 补强 form 特征 |
| `/data/league-matrix.json` | **33 联赛场均**：进球、xG、角球、黄牌、犯规、零封率、BTTS | 补强 league_params |

### 关键技术情报
- 后端暴露 Supabase：`wqfvrkcwvmvomofvsxqm.supabase.co`（CSP 里可见）
- 认证：Clerk（djyylive + djyydata 双域），付费墙后的接口需登录
- 前端 cdn：`cdn.djyylive.com/_next/static/`（djyydata 是 Next.js 多语言站，Fly.io 托管）

---

## 二、DJYY 商业产品（djyydata.com）— 价值模型方法论

djyydata.com = 「DJYY Data — Football Strategy Data」：**120,000+ 场回测历史、精选策略、每日价值推荐**。

### 核心产品结构（实测页面 + API）
- **Value Model**：`model × market odds` → 哪个比赛、哪一边、赔率下限（odds floor）→ 每日价值推荐。风险声明原文：*"The signal is intermittent and negative in some seasons"*（诚实披露：信号间歇、部分赛季为负）⭐ 这个诚实披露值得本地学习
- **Featured Strategies**：官方精选策略，「真实归档赔率结算（settled at real archived odds）、全历史公开、每日更新」⭐ 结算口径 = 归档赔率而非即时赔率，这是严谨的 EV 回测标准
- **Strategy Lab**：自建策略 + 回测（对标本地 combo_miner/param_optimizer）
- **Track Record**：回测历史 + 实盘记录，**统一 100/bet 口径**
- 页面：`/en/value-model`、`/en/featured`、`/en/screener`、`/en/tools`、`/en/pricing`（付费墙）
- 数据 API：`/en/api/strategies`、`/en/api/value/today` 等返回 200，但为 Next.js 页面路由（客户端 JS 渲染），真实数据走登录态 + Supabase，未继续深挖（付费墙）

### 对本地最有价值的 3 个产品化思路
1. **Value 推荐 = model × market odds × odds floor**：本地已有 ev_recommendation，缺的是**统一的 odds floor 口径**（多大 EV 才出手）
2. **Track Record 统一 100/bet 口径 + 归档赔率结算**：本地 kelly_staking/ev_evaluator 已有雏形，可对齐「归档赔率」结算标准（避免用实时赔率回测的乐观偏差）
3. **诚实披露负赛季**：页面直接展示「部分赛季为负」——比只报 ROI 更有公信力，Hicruben 的 track record（成败都列）同理

---

## 三、GitHub 新增深挖：2 个仓库源码级结论

### 1. 0xNadr/wc2026（层级贝叶斯 Dixon-Coles，PyMC 实现）
**核心创新——先验由外部强度信号投影**：
```python
# build_priors(): Elo 归一化 z-score + 阵容强度 z-score → 0.7*elo_z + 0.3*squad_z
# att_prior_mu = def_prior_mu = 0.4 * composite - mean   (收缩到均值为0)
# att[i] ~ Normal(att_prior_mu[i], att_prior_sigma)
```
- **稀疏数据球队（库拉索、佛得角）有合理的收缩目标**——这是对 JetQiao「解析收缩」的贝叶斯升级版
- 时间衰减权重 + 比赛重要性权重（世界杯正赛 > 友谊赛）
- τ 低比分修正照搬 Dixon-Coles 原文
- 对本地的启示：本地 shrinkage_dc.py 是解析 MLE 收缩，**可进一步用 Elo/联赛强度做先验均值**（而不是简单收缩到全局均值）

### 2. raynergoh/EPL-Predictor（Poisson 三模型 + ξ 调参）
**核心创新——时间衰减参数 ξ 的严谨调法**：
```python
# φ(t) = exp(-ξ * t)，t 以 3.5 天（半周）为单位，参考 Dixon & Coles (1997) 原文
# tune_xi.py：时间序列 CV（保序切分防泄漏）扫描最优 ξ
# 对比：baseline（无时间加权）vs 时间加权最优 ξ → accuracy/Brier/log-likelihood/ROI
```
- 对本地的启示：本地 time_decay.py 有衰减但**没有系统性扫 ξ**，可加「保序 CV 扫 ξ」脚本（对标 epl-predictor tune_xi.py）
- 三模型（Elo/DC/GBM）walk-forward 对比 + 与庄家市场比较 → 本地 ensemble 可加「vs 市场基准」报告

---

## 四、对 football-engine 的可落地改进点（合并全部深挖，按优先级）

### P0（高价值低成本，数据已在手）
1. **市场移动信号**：comparison API 已有 Pinnacle 初盘 vs 即时盘 → 算 `odds_move = current/opening - 1` 作为特征（强信号：机构调盘方向）→ 接入 lgbm/ensemble
2. **裁判/天气/场地特征**：info API 提供裁判场均牌数、风速、湿度、草皮 → 作为 lgbm 特征 + 进球模型微调（严格裁判/大风/人工草皮 → 压低进球）
3. **league-matrix 增强**：DJYY 33 联赛 avg_xg/零封率/BTTS 并入本地 league_params（现只有场均进球）

### P1（方法论升级）
4. **层级先验收缩**：shrinkage_dc 的收缩目标从「全局均值」升级为「Elo/联赛强度投影先验」（0xNadr 思路，解析近似版）
5. **ξ 最优调参**：保序 CV 扫时间衰减 ξ（对标 tune_xi.py），替代手工默认值
6. **归档赔率结算口径**：EV 回测统一用归档赔率（DJYY featured 标准），避免实时赔率乐观偏差

### P2（产品化/展示）
7. **Value 推荐产品化**：model × market odds × odds floor 统一口径，每日推荐页
8. **Track Record 页**：统一 100/bet 口径 + 诚实披露负赛季（DJYY/Hicruben 双参考）
9. **DJYY 模型概率作为 ensemble 第三方输入**：comparison 的 p_home/p_draw/p_away（source=statistical-model）与本地模型融合（注意：需验证其独立性，避免同源）

---

## 五、风险与注意
- DJYY 的 model 概率是**第三方黑盒**，作为 ensemble 输入前需验证其与本地模型的相关性（防止同源叠加）
- djyydata 付费 API 未深挖（需登录），若需要可后续研究其前端 JS bundle 的 API 调用（已定位 chunk：`cdn.djyylive.com/_next/static/chunks/`）
- 裁判/天气特征需**逐字段验证可用率**（info API 的 weather 部分字段为 null，见实测）
- 所有新特征接入前必须过 cnemri 的「防泄漏」纪律：只用开赛前可得数据
