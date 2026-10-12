# PROJECT.md — football-engine（竞彩足球概率引擎）

> [!IMPORTANT] 交接卡（收工必更新；冷启动只读这块）
> 状态: doing　更新: 2026-10-13 01:55（主线 agent / 统一入口轮）
> 在干什么: 收口 PR 乱局 —— 6 个 open PR 中 3 个是重复/悬空重建版，已逐一本地验证（测试+ruff）
> 下一步: ① 合 #29(audit-chain-v2) → #28(whitelist-locks-v2) → #27(e3-e4-rebuild)（三者互不冲突，已用 merge-tree 验证）
>          ② 合完关掉 4 个悬空/重复 PR（#21/#22/#24 + 删 9 个遗留分支）
>          ③ 合并后观察巴甲平局率回升（原 31.6% vs 市场 63.2%）与总进球指标（预期短期偏高）
> 已知坑: ① 分支带上 `data/`+`web/` 全量产物 = 几千行 diff 噪音（主因是分支落后 main 6~14 笔自动提交）
>          ② PR#28 的 `evolution/` 有 16 处 ruff F 级告警，但 test.yml 只 lint `engine scripts tests` ——  lint 范围没跟上触发路径
> 待验证: ① `~/.ssh/football-engine_deploy` 本机不存在（STATUS.md 说推送走它）——推送方式实测待确认
>          ② 本机无 `~/football-engine`、无 crontab/launchd —— 旧"本地每小时跑"管线确认已不在本机

- 更新: 2026-10-13（主线 agent 统一入口轮）

## 这是什么
竞彩足球概率分析与虚拟投注模拟：Dixon-Coles 泊松 + 蒙特卡洛 + 市场主导融合 + Kelly 虚拟下注。
全自动跑在 GitHub Actions（预测/结算/采集/自检/Pages 报告），无服务器。**仅数据分析与算法学习，不构成投注建议。**

## 结构速查
- `engine/main.py` — 每日管线入口（抓数据→融合→锁定→结算）
- `engine/prediction/` — `dixon_coles.py` / `monte_carlo.py` / `fusion.py`（纯函数+开关+trace）/ `signals.py` / `score_candidates.py`
- `engine/backtest/` — 回测与实验（**评估层，禁自动改**）：`gate_reachability.py`(E3) / `book_vig.py`(E4) / `cross_market.py`(E2)
- `engine/review/post_match.py` — 结算写账本（PR#29 在此补全审计链）
- `engine/integrity/quarantine.py` — 污染样本隔离（巴甲平局概率被钉 0.5025 的 20 场）
- `engine/odds_bands.py` — 赔率分层 L1-L5 单一真源（与闸门边界 1.8 对齐）
- `evolution/` — 自治进化闭环（诊断→补丁→验证→开 PR；三道锁见 `harness/patch_engine.py`）
- `config/prediction.json` — 模型/融合参数；`config/strategy.json` — 下注策略
- `data/state/review_ledger.jsonl` — 训练账本（append-only，chain=v2 洁净）
- `docs/EXPERIMENTS.md` — 实验登记表（开工前登记，判定日写死不改）

## 环境事实
- Python 3.12（CI 同版本）；本机裸 python3 是 3.13，勿用于本项目
- 依赖：numpy/scikit-learn/LightGBM/joblib（lightgbm 需 `brew libomp`）【易变：brew 源】
- 推送：HTTPS token 或 SSH deploy key；STATUS.md 记录走 `~/.ssh/football-engine_deploy`【本机未验证】
- GitHub Actions：daily-forecast(:15/:45) / ci-sync(:47) / health-check(:00) / evolution-loop(每日 01:30 UTC，PR#28 新增)
- GitHub 对公开仓高频 schedule 有排队限流：实测 */15 实际只跑 5 次/天，阈值必须按真实频率设

## 已知未修（按优先级）
- [ ] `evolution/` 16 处 ruff F 级告警（`fusion_core.py:109` 未用变量、`invariants.py` 未用 import）→ 修法：加 `evolution` 进 test.yml lint 范围，或直接清掉
- [ ] issue #1（外部用户 dzd1234 报）：竞彩未开盘但 predictions.json 有胜平负推荐 → 需复现"无盘口场次仍出 1X2 推荐"的路径
- [ ] `kelly.py` 拿 `1/odds`（含 12.1% 抽水）当市场概率，与 `clv.py`/`upgrade_tracker.py`/`post_match.py` 的去水口径不一致 → 改口径会放宽闸门 1.4~2.6 倍，**需人拍板**（PR#27 文档已标注，故意未改）
- [ ] 开盘价列（PSH/PSD/PSA）样本为 0：`_new.csv` 有收盘无比分旧格式有开盘无比分 → 要开收盘对比须先合并两套 schema
- [ ] `evolution/` 每轮在 main 上直接跑 `python -m evolution.cycle`，产物进 `evolution/out*`（已 gitignore）

## 已修（现象 → 根因 → 修法）
- 2026-10-12 CI 重试循环从不 abort → Daily Forecast 永久失败；`|| echo` 吞掉失败把毒状态传给下游 → 失败必须 abort + 显式告警（daily-forecast.yml）
- 2026-10-12 测试并发组跨 PR 互相取消 → group 是全局 `sporttery-tests` + cancel-in-progress → 按 PR 号分组（test.yml）
- 2026-10-12 评估类报告从未被 CI 刷新 → 修复只进 PR 没进生产（PR#6 的修复在生产从未生效）→ 报告生成步骤挂进 ci-sync
- 2026-10-11 健康检查 60 分钟阈值是死逻辑 → 声明 */15 实际 4~5 小时才跑一次 → 阈值按真实频率设（health-check.yml）
- 2026-10-11 账本把「1X2 方向命中」当「下注命中」→ 聚合口径混淆两件事 → 写入端修 + 逐场落盘审计字段（PR#29）
- 2026-10-11 三套赔率分层口径不一致（账本 2.0/3.0 vs ev_report 1.8/2.2 vs 闸门 1.8）→ 账本 1.5-2.0 桶横跨闸门边界，L3 缺口结构性不可见 → `odds_bands.py` 单一真源（PR#29）
- 2026-10-11 DJYY 断供 6 周静默（名义权重 0.20 实际贡献 0）→ 融合层归一化兜住但无任何运行时信号 → 逐场落盘 `fusion_effective_weights` + 死源告警（PR#29）
- 2026-08/10 巴巴西平局概率被钉 0.5025（真实 0.266）→ `league_draw_baseline` 把"判平精度"当平局率 → bug 已修，但 20 场污染样本仍在 v2 链 → `quarantine.py` 按 match_id 隔离（PR#29）

## 决策与理由
- 融合权重市场主导（model 0.05 / market 0.75 / djyy 0.20，实际 6.25%/93.75%/0）：426 场账本实证纯市场 Brier 0.583 优于任何模型流（0.646+）
- 污染样本按 match_id 剔除而非升 chain：污染仅 20/558 条（3.6%），升 chain 会作废全部样本重来（2026-08-29 已做过一次）
- PR#27 **故意不改** `kelly.py` 的含抽水口径：E0~E2 全负时放宽闸门只会增加亏损注，这是需要人拍板的取舍
- 三道锁（PR#28）：C1 白名单禁触评估层（代码级）+ C2 自动 PR 强制反事实对照（bootstrap CI）+ C3 CODEOWNERS 锁给人（流程级）—— 因为闭环会朝"指标好看"的方向进化，没有约束它就是奖杯机
- 本目录 `~/Downloads/football-engine` 定为唯一工作副本（2026-10-13）：本地 cron/launchd 全停，日常循环全在 GitHub Actions，避免多机器各跑各的

## 下次注意
- 开 PR 前先 rebase 到最新 main：`data/`+`web/` 有每小时自动提交，落后 6~14 笔就会带上几千行噪音 diff
- 合并顺序 #29 → #28 → #27（三者互不冲突，但 #27 的 `docs/EXPERIMENTS.md` 与另两者无交集，顺序不影响）
- 合并 #29 后头几天重点看：巴甲平局概率是否回升、总进球类指标是否短期偏高（`xg_calibration` 回收的已知代价）
