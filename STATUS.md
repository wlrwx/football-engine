# 足球引擎 · 状态文档（新对话开场先读这个）

> 最后更新: 2026-10-11 | 项目: 《王者万象棋》无关，这是竞彩足球预测引擎

## 一句话现状

竞彩足球概率预测 + 虚拟投注系统，GitHub Actions 全自动运行。
**系统当前实质是「去水市场基准 + 约 6% 的模型」，而那 6% 经7 万场检验
显著为负贡献。** 下面三行是复算自`review_ledger.jsonl`（984 行）的配对结果，
不是引用旧报告：

```
口径          n     model_Brier  market_Brier     Δ        t        命中率
全量        873       0.5860       0.5698    +0.0162   +3.79   53.5% vs 55.4%
v2洁净      509       0.5646       0.5601    +0.0045   +1.23   54.8% vs 56.0%
legacy      364       0.6159       0.5834    +0.0325   +3.69   51.6% vs 54.7%
```

约定：**Δ = 模型 − 市场，正数 = 模型更差。**

### ⚠️ 订正 2026-10-11：旧版此处写的是错的

旧文案写"IMPROVED 判定成立 z=3.14 / 持续小幅跑赢纯市场"。两处都不准确：

1. **`z=3.14` 是 vs 旧链（legacy 0.6159），不是 vs 市场。** 对 vs 市场，
    v2 口径是 Δ=+0.0045 / t=+1.23，**不显著，且方向是模型更差**。
2. **"跑赢"不成立** — v2 上模型命中率 54.8% **低于**市场 56.0%。

`upgrade_tracker` 的 `IMPROVED` 判定量的是"修 bug 有没有效果"，不是
"有没有 edge"。这两个问题必须分开回答，混在一起会系统性地把"没赢"报成"赢了"。

### 7 万场强证据（`data/state/historical_replay.json`）

仅靠 984 场账本还不足以定论。接入 `data/historical/football_data/` 的
71,774 场多公司收盘价后，严格 walk-forward（171 次重拟合、70,974 条配对预测）：

```
Brier    模型 0.60902   市场 0.58885   Δ=+0.02018   t=+34.74
21 个联赛全部 FDR 校正后显著更差，无一例外
```

E1（市场自身的修正 vs 市场收盘）：Δ=−0.00006，t=−0.80，**不显著**。

**两条独立路径撞到同一堵墙** —— 纯球队实力模型走不通，预测市场会怎么动
也走不通。按现有证据，`model_weight` 应从 0.05 降到 **0.00**。

**尚未决策**（会改变所有输出概率，属策略决策不是工程修复）。

### 成本侧的硬约束

竞彩胜平负 overround 实测（`data/state/odds_series/`，936 文件 / 2,917 快照，
overround = 隐含概率之和 1/H+1/D+1/A）：

```
中位 1.1211   均值 1.1204   P10 1.1103   P90 1.1276
→ 理论返还率 89.2%   抽水 12.11%
```

同期模型相对优势约 **0.64%**（全量配对 Δ=+0.0162 / market 0.5698）。
**两者差约 19 倍。**

对比：欧洲盘 overround 均值 1.057（5.7%），比竞彩低 2.1 倍。

### ✅ 已决策（2026-10-11，用户拍板）

**1. 模型权重不归零。** 理由记录在案：上一次"归零"造成了 chain 重置，
426 场 legacy 洁净样本作废、从头累积至今，代价很大。技术上也站得住——
`chain=="v2"` 的样本喂给 `calibration_auto` / `lgbm_shadow` /
`upgrade_tracker` / `ablation_replay`（见 `main.py:2325`）。若把
`model_weight` 降到 0，新样本由**另一个生成器**产出（纯市场 vs
6.25% 模型），与现有 558 场混在同一 chain 会污染校准器，
等于又要做一次 chain 重置。**归零的代价是再废一次样本，收益是 6% 的边际改善，不划算。**

**2. 继续虚拟下注。** 下注记录是预测系统的重要产出，不下注就没有记录。
`activation_mode` 保持 `shadow`。熔断器已于 2026-10-11 修复并生效
（实测 tier=1、真实 pnl、peak_bankroll 正常追踪）。

### ⚠️ 下注记录可信度修复（2026-10-11）

既然记录本身是资产，复查发现两个缺口并已修：

- **三套赔率分层口径不一致** → 账本的 `1.5-2.0` 桶横跨闸门边界 1.8，
  导致"odds 1.8-2.2 系统性亏损"在账本里**结构性不可见**。
  现在统一到 `engine/odds_bands.py` 单一真源，边界与闸门对齐。
- **`pnl=0.0` 无法区分"没下注"和"下了没中"** → 现在账本记录
  `odds_used` / `odds_layer` / `bet_sel` / `stake_planned` / `gate_blocked`。

详见 `docs/BET_RECORD_INTEGRITY_20261011.md`。

---

## 系统实际在跑什么

三处权重各不相同，生产路径读的是第二处：

| 来源 | model | market | djyy | 谁在读 |
|---|---|---|---|---|
| `config/prediction.json` | 0.10 | 0.60 | 0.30 | 兜底默认值 |
| `data/state/fusion_weights.json` champion | **0.05** | **0.75** | **0.20** | **生产推理实际使用** |
| `ablation_report.json` meta | 0.10 | 0.75 | 0.15 | 消融回放 |

DJYY 从 2026-08 中旬断供（v2 链 558 场里仅 4 场有数据，全在 10-10），
融合层走 `fuse_two_way` 并重新归一化：

```
有效 model  = 0.05 / (0.05 + 0.75) = 0.0625
有效 market = 0.75 / (0.05 + 0.75) = 0.9375
```

**模型在整个融合里的边际贡献是 6.25%。**

> ✅ 2026-10-11 已修：这个比例原先哪儿都没记。现在逐场落盘
> `fusion_effective_weights` / `fusion_source`（predictions + 账本），
> `engine/selfcheck.py` 第 6 项检查会每天自动告警死源与权重过低。
> 见 `docs/EFFECTIVE_WEIGHTS_20261011.md`。

这也解释了过去两个月的所有困惑：Brier 为什么贴着市场、37 个权重候选为什么
全被 REJECT、优化器为什么自己搜出 `best_weights={model: 0.0}`——
**它唯一的直觉是对的**，只因 t=1.64 < 1.96 被门槛挡下。
### DJYY 第三通道（2026-10-11 上游已修）

曾静默死亡 6 周，结算侧现按 `_djyy_id`/`match_id` 从 context_cache 回填，并加存活率报警。回填样本待重新定价，不影响上面的口径结论。
- 融合权重：每次结算自动调优（champion 0.05/0.75/0.20）。**调优器一直
  把它按在 0.05**，因为任何往上调的方向都在制造损失——这不是调优器不工作，
  是它在如实反映"模型无边际信息"。
- 校准层：自动启用/停用决策——维持关闭（验证段 Δ=+0.0016 / t=0.28，不显著）。
- LGBM：已首训 + 影子验证**诚实拒绝**（`data/state/lgbm_status.json`：
  vs 融合v2 Δ=−0.0048 / t=−0.38，vs 市场 Δ=−0.0130 / **t=−0.94**，
  两条基线都没到 1.96）。继续攒样本等它显著。
  ⚠️ 旧文案写"Δ=-0.018"未给 t 值，容易被读成"LGBM 跑赢市场"——
  **负号只表示"没有变差"，不表示"更好"**。判定看的是 t。
- push 卡死 detached HEAD（2026-10-11 修）: monitor/auto_run 的 pull --rebase 停在
  空提交上残留 rebase-merge → HEAD 游离、push 全挂、本地拉不回远端（2小时数据没同步）。
  两脚本已加自愈: rebase --quit + checkout -B main + reset --hard origin/main（远端为准，
  数据每小时重生成不怕丢）
- 熔断器三层惰性（2026-10-11 全修，见 `docs/analysis_20261011/05_*.md`）:
  ①`effective_mult` 被硬编码 1.0 丢弃熔断输出（提交 f93821560 引入）
  ②结算只遍历 stable/value/lottery，parlay+score_parlay 占计划 78% 完全不可见
  ③日/周止损依赖 pnl<0，但 shadow 模式 stable/value/lottery 注额恒 0
  → 另补「距峰值回撤」触发维度（07-27 暴跌是回撤驱动不是连败驱动）。
  **修复前 `ticket_plan.bankroll` 那条曲线不能作为任何决策依据。**
- [ ] PR #4 合并：已 rebase 到最新 main 并连同 auto-patch 三道锁（白名单/反事实/CODEOWNERS）
      开在 **PR#21**，255 测试全过、不再回退 #6~#9 测试，**等用户在 GitHub 上点合并**
- [x] ~~决策：模型权重是否归零~~ → **决定不归零**（2026-10-11 用户拍板）。
      归零会要求 chain 重置，废掉现有 558 场洁净样本，代价远大于 6% 边际改善。
- [x] ~~决策：竞彩 12.1% 抽水还要继续下注吗~~ → **决定继续**（虚拟下注，
      记录本身是系统产出）。熔断器已修复生效。
- [ ] L3 闸门缺口：**证据不支持移边界，先不动。** 全样本 n=248 / ROI −12.7% /
      t=−2.02 看似显著，但前后半段分别 t=−1.14 / t=−1.71 **均不显著**，
      按联赛也全部不显著且有正收益联赛（日职 +9.8%）。移边界后剩余
      L4+L5 = n=210 / ROI +3.4% / t=+0.39，同样不显著——**移边界得不到
      显著盈利口径，只是移走边缘亏损**。正确动作是继续积累记录让证据自然显现。
      账本分层口径已修（此前该缺口在账本里根本看不见）。
- [ ] 洁净样本继续累积（558→更大），跟踪 upgrade_tracker 判定。
      注意判定口径：IMPROVED 是 vs 旧链，不是 vs 市场。
- [ ] `main.py` god object：`run_daily_pipeline` 1225 行 + `run_settlement` 955 行，
      全文件仅 4 个顶层函数。同项目 `fusion.py` 292 行做得很干净——两种标准。
      建议单独 PR 重构，不要和功能改动混在一起。
- [x] 有效权重不可审计（2026-10-11 已修）：逐场落盘 `fusion_effective_weights` /
      `fusion_source`，`engine/selfcheck.py` 第 6 项检查每日自动告警死源。
      见 `docs/EFFECTIVE_WEIGHTS_20261011.md`。

## 关键路径

| 内容 | 位置 |
|---|---|
| 引擎本体 | `~/football-engine/`（engine/ + config/ + data/） |
| 四轮升级文档 | `docs/UPGRADE_20260829.md`、`docs/UPGRADE2_20260829.md`、`docs/CONTEXT_DATA_20260830.md` |
| 实时状态 | `data/state/`（calibration_status / lgbm_status / upgrade_tracker / selfcheck_report） |
| 训练账本 | `data/state/review_ledger.jsonl`（append-only，chain=v2 = 洁净样本） |
| 体检脚本 | `scripts/upgrade_tracker.py`、`scripts/edge_calibration.py`、`scripts/ablation_replay.py` |

## 自动化（重要）

- **每周一 03:30 周度体检**（升级效果+EV校准+情境覆盖）——确认它是否还挂在旧对话的工作区；
  若失效，在新对话用 CronCreate 重建，提示词要点在 docs/UPGRADE2 的路线图段落
- GitHub Actions 每半小时跑 daily-forecast（预测+结算+自检+部署 Pages；schedule 实际延迟 2~4h）
- 本地 `auto_run.sh` 每小时实时监控（含代理自愈：7897 不通自动拉起 Clash Verge）
- **系统医生 `engine/doctor.py`（2026-10-11 起）**：auto_run 每轮末尾自动体检+安全自愈——
  git 卡死清场/积压补推/refspec 修复、补抓赛果、**补结算**（结算本地化由此覆盖）、
  DJYY 存活率、CI 停摆、概率分布先验带、权重归一、cron 存在性、代理、仓库体积。
  报告: data/state/system_health.json。
  设计: 幂等自愈 + critical 返回 2（供 CI 门禁）+ 每条检查对应一次真实事故
- **仓库专属 .venv（2026-10-11 起）**：py3.12 + requirements 全量（sklearn/lightgbm 可用，
  lightgbm 需 brew libomp）。auto_run 探测顺序 .venv 优先——本地进化决策不再依赖
  hermes venv 的残缺环境；本地现在也能跑 LGBM 训练/校准拟合（与 CI 同基线）
- **推送方式（2026-10-13 更正）**：主线工作副本在 Mac（`~/Downloads/football-engine`），
  推送走 **HTTPS + gh token**（`gh auth login` 已配置 account wlrwx）。
  ⚠ 此前这里写的是「SSH deploy key `~/.ssh/football-engine_deploy` + `git@github-football-engine:`」，
  **该 key 与 ssh config 条目在主线 Mac 上均不存在**，是 Mac Air 那台的本地配置。
  两台机器推送方式不同，别再互相照抄：Air 保留它自己的 key，主线用 HTTPS。
  原 SSH 方案要解决的问题（launchd/cron 会话 osxkeychain 静默返回空）在 Actions 侧不存在，
  CI 一律用 `github.token` 走 HTTPS（见 ci-sync.yml 提交步骤）。
- **Mac Air 的唯一职责（2026-10-13 起）**：只跑 **NJStats 预测挑战每日 11:30 提交**
  （需浏览器登录态，无法上云）。**Air 上其他本地 hourly cron 必须全部停掉**——
  它与 ci-sync 每小时同时写 `data/state/`，必有一次 non-fast-forward，
  两边互相覆盖且不可追溯（10-12 晚 ~10-13 凌晨的 hourly 提交即来自 Air，彼时 Actions 无调度）。
  提交器代码与状态机说明见 `scripts/njstats_challenge/`（原在 Air 本地 `_vault/`，未入仓库）。

## 自进化机制现状（数据纪律，勿手动干预）

- 融合权重：每次结算自动调优（champion 0.05/0.75/0.20）
- 校准层：自动启用/停用决策——3 次评估均理性维持关闭（不显著不开）
- LGBM：已首训+被影子验证诚实拒绝（vs市场 Δ=-0.018），继续攒样本等它显著
- 每次结算自动写 chain=v2 洁净标记

## 已修过的大坑（防止重蹈）

- DJYY 断供两周三层根因（2026-10-09 全修）: ①限流(2400+调用/天→按天缓存)
  ②队名匹配(竞彩缩写/译名→前缀规则+别名) ③API schema迁移(referee→referees.main、
  injuries→home.sidelined)
- push 卡死 detached HEAD（2026-10-11 两次复发，晚20:25终修）: auto_run 的 pull --rebase
  停在空提交上残留 rebase-merge → HEAD 游离。自愈代码其实凌晨就加了，但埋在
  `if ! git pull | tee` 里——测的是 tee 的退出码，bash 管道取末位退出码，**自愈分支
  是从未执行的死代码**。终修: PIPESTATUS 判 git 退出码 + 启动时兜底清场。
  monitor 的 python 版自愈用 returncode 判断，一直是好的
- cron 推送拿不到凭据（2026-10-11 修）: keychain 条目和设置都正常、GUI 会话能推，
  但 launchd 后台会话 osxkeychain 静默返回空 → "could not read Username: Device not
  configured"。凌晨起挂了数小时，根因未完全定位（疑似 keychain 授权弹窗未被应答后拒绝）。
  终解: 注册 deploy key 走 SSH（见"自动化"），与 keychain/登录状态彻底解耦
- DJYY 第三通道数据断链（2026-10-11 修）: 8/16~10/9 账本 djyy_prob 全 null——CI 屏蔽期
  生成的预测无 DJYY→结算写 null→融合优化器把 DJYY 权重压到 0（有效 0%，模型仅 6.25%）。
  修: post_match 结算按 _djyy_id/match_id 双键从 context_cache 回填（djyy_backfilled
  溯源标记）+ selfcheck 存活率报警（近3天中2天<50%）。历史缓存无 model_probs 不可救
- stakes 字段（动机/情境）DJYY 上游恒 null（2026-10-11 抽样证实）: comparison API 有 key
  不填值，store-only 不进模型，周检覆盖率别拿它当健康指标
- CI 侧 djyylive 屏蔽 Azure IP 段 → 字段保全方案(本地富化+CI不覆盖, 双端各取所长)
- 本地调度: cron :00 盘口监控 + :25 完整管线(auto_run.sh, 含 DJYY 富化)
- 两个本地 cron 均带代理自愈(Clash 关闭自动拉起)
- 国庆停售三层修复(优雅跳过/非竞彩过滤/残留清理)
- CI schedule 实际延迟 2~4 小时（GitHub 调度延迟，非故障）→ 当日结算滞后属正常，
  可本地补: fetch_sina_results.py --date X && python -m engine.main --settle --date X（幂等）

- isotonic platt 分支 shrink 未定义（30≤n<100 触发）→ 已默认值+try/except
- selfcheck 小样本日（<5场）全缺误阻断 → 已加门槛
- LGBM save() API 不对称（LGBMClassifier 无 save_model）→ 已修
- CI 挂了别慌：先看失败步骤（守门员拦截=设计行为），再查日志

## 本地保险库 `_vault/`（不入公开仓库，同机 agent 可读）

站点侦察细节/密钥位置/挑战策略与配额状态全在这里；公开仓库只留泛化结论。
文件索引：recon_njstats.md（端点+JWT+规则）、recon_djyy.md（screen 契约+交叉验证）、
recon_njstats_matrix.md（16/16 矩阵）、challenge_state.json（配额）、
njstats_challenge_submit.js（v2 提交器）、njstats_value_model.json（1,667 注战绩）、
njstats_exports/sample.xlsx（Excel schema）、close_calibration_notes.md（研究笔记）。

## 下一步/开放事项（2026-10-12 晨更新）

- [ ] NJStats 试用窗口（10-15 00:08 到期）：归档管线每小时断点续采（match/odds/8概率端点
      ×分页）；概率端点的历史页在深页，逐小时补齐后做他们的信号质量评估（Brier vs 赛果）
- [ ] 预测挑战：每日 11:30 自动提交已建（EV 策略：只提赔率∈[1.60,2.31]且模型概率最高的场次，
      每日≤5 场、周配额 28，状态 _vault/challenge_state.json）；10-13 凌晨首轮结算后复盘命中率
- [x] CLV 三源交叉验证（2026-10-12）：DJYY 均价 -12.45% / football-data 平博 -6.27%（258注）/
      NJStats 平* -9.22%（28注）；**锐度梯度：澳*软庄 -3.29% → 平*锐线 -9.22%，6pp 差=
      市场水位差**。教训固化：量 CLV 必须用最锐基准（scripts/njstats_clv_check.py）
- [x] DJYY 数据门户审计：1,667 注（底价 +13.57% / 可执行收盘 +7.21% / 实盘 -8.7%），
      每月随管线自动重审（scripts/djyy_audit.py）
- [ ] SportMonks Growth 试用激活（用户暂缓）→ 验 odds 模块是否随套餐
- [ ] git 历史端点路径清洗决策（HEAD 已清，历史待用户裁决是否 force-push 重写）
- [ ] AUT 平局纸面证据累计（双源佐证 +7.5%/+9.24%，t≈1.7 候选，live 证据持续累计）
- [ ] 洁净样本累积 / LGBM ready / Qwen 新闻源（用户决定）

## 云端化与凭证现状（2026-10-12 下午补录）

- **GitHub Actions 三条管线**：daily-forecast（预测+结算，原有）、njstats-archive（试用窗口归档）、
  **ci-sync.yml（新）**：SportMonks 采集/NJStats 归档/每日 fdc+纸面+DJYY 审计/CLV/体检
  （2026-10-13 删掉了原来那步「盘口监控」——跑的 realtime_odds_monitor.py 自述已弃用且空转）
  ——Mac 关机不断采（预测挑战除外：需浏览器登录态，留 Mac Air）
- **CI Secrets**：NJSTATS_KEY ✓、SPORTMONKS_TOKEN ✓（均 API 端加密注入）；
  ⚠ **DJYY_PROXY 从未设置**——daily-forecast 引用它但 secret 不存在，CI 侧 DJYY 富化
  一直静默直连被 403（Worker URL 在用户 Cloudflare 账号，待用户提供后一条命令补上）
- **推送方式**：主线 Mac 走 HTTPS+gh token；Mac Air 用它自己的 SSH deploy key。两台不要互相照抄。

## 预测挑战状态（2026-10-12 晚）

- 本周 11/28 已提交，段位**挑战者**、周榜 **#12**、排位分 140
- 实测规则：**每场仅一市场**（提过即消失）；低赔带豁免只适用胜平负；
  让球按水位口径记录（主队-1@0.95），真实给分待 10-13 结算校准
- 自动化 v2（每日 11:30）：跨 胜平负/让球 择优（大小球观察不提交），
  提交器 `_vault/njstats_challenge_submit.js`（带让球线校验防符号错位）

## 数据资产现状（2026-10-12 晨）

| 资产 | 规模 | 生命周期 |
|---|---|---|
| football-data 历史 | 60,912 场含平博收盘 | 永久 |
| NJStats 归档 | 596 场×8 庄×带赛果（赔率）+ 概率端点深页补齐中 | **试用到期后失去更新** |
| DJYY 价值模型 | 1,667 注记录 | 永久（公开） |
| SportMonks 免费档 | 丹麦超/苏超阵容伤停 24 文件 | 永久 |
| AUT 平局纸面 | 1,022 注 | 永久 |
