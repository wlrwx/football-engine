# PROJECT.md — football-engine（竞彩足球概率引擎）

> [!IMPORTANT] 交接卡（收工必更新；冷启动只读这块）
> 状态: doing　更新: 2026-10-28 16:20（三条 PR 合并 + Windows 门禁 + 模型权重归零研究）
> 待合并(11-04, 自动化 agent): 泄漏审计 PR #80 + 独立复核 PR #83。审计入口合并成
>           一份 `scripts/leak_audit_check.py`（原 `scripts/leak_audit/audit_leakage.py`
>           删除，其未复核观察点降级为报告里的 info 记录）；两份测试改为**断言判定
>           结论**而非硬编码计数（黑名单允许位置集合、开关规则、未传参数默认值 +
>           训练/服务特征逐位相同）。只读，未改任何生产行为/配置/状态。
> 复核(11-04, 自动化 agent): 两条「待核」观察点已查清（dykily/football-engine#5），见
>           `docs/proposals/dc-market-blend-and-devig-review.md` +
>           `tests/test_dc_market_blend_review.py`（22 项）。判定：DC 内混的来源确是
>           竞彩(target)，但 `market_blend_weight=0.0`（config/prediction.json:11），
>           喂任意 target 赔率概率逐位不变 ⇒ 路径不活、封顶 info，**不是违规**；
>           devig 两套口径作用于不同对象（回放=参考盘收盘基准 / 服务=竞彩融合输入），
>           裁决开关的 ablation_replay 用冻结 market_fair ⇒ **无漂移**。只读复核。
>           坑：`DOUBLE_MARKET_BLEND_20261023.md:26` 的「改回 0.28」回滚步骤会同时
>           制造市场隔离违规与双重计入；MC 的「默认评级→赔率反推 xG」无开关，另列待裁决。
> 本轮(10-28): 见 `docs/UPGRADE_20261028.md`。① PR #59/#60/#61 全部 cherry-pick 进
>           `feat/consolidated-upgrade-20261028`（23 条评审项落地；冲突仅 plan_lock/
>           main.py 导入/fusion.py 的 _djyy_confidence 复用/config 的 rho 保留最新）。
>           ② **三 PR 的 CI 一直是红的，原因是分支陈旧**：都带着 10-23 的
>           `scripts/fetch_news.py`（F401 未用 import），pytest 根本没跑。
>           ③ Windows 门禁从 24 红到 0：文本 IO 补 encoding 106 处、subprocess 26 处、
>           .gitattributes 补 yml（workflow 被检出 CRLF → 抠出的 shell 每行带 \r）、
>           测试改用 Git Bash 绝对路径（System32 的 `bash` 是 WSL 垫片，Windows 路径
>           被翻成 /mnt/c → 裸库 remote 全丢）、submit.js 的 python3→python 回退、
>           DJYY 预算判定 `>`→`>=`。新增 `tests/test_io_encoding.py` 静态守卫。
>           ④ **语义变更待你复核**：`market_blend_weight` 0.28→0（市场不再双重计入）、
>           djyy-only 补置信门槛、账本 `result_version`、loose 规则收紧——回滚方式都在
>           UPGRADE_20261028.md 第五节。
>           ⑤ 收尾 PR：`submit.js` 的 HOME 未定义回退 `os.homedir()`（Windows
>           计划任务会崩在模块加载）、doctor 的 crontab 假结论（没命令 ≠ 已验证无残留）、
>           关闭 issue #1（合成赔率早已不出票 + 页面标「不可投注」）、删远端合并分支。
>           ⑥ **模型权重归零研究（10-28）**：扩 `ablation_replay.py` 的
>           `model_weight_sweep`，810 场 v2 洁净样本、生产后处理开关、单变量配对。
>           结论：w0 vs w5 的 Brier Δ=−0.0003（95%CI 跨 0，t=0.73），但 argmax
>           命中率 57.0%→56.7%、方向只变 0.9% —— **不显著、不占优**。归零的真实成本
>           在 chain（Q1 会被市场化样本抬高，或需升 v3 作废 810 场）。**暂不归零**，
>           生产权重未改；登记 E11，确认性判定需 n≥8,300。详见
>           `docs/MODEL_WEIGHT_ZEROING_20261028.md`。
>           本地实跑：852 passed / 13 skipped / 0 failed（Windows，无环境变量）。
> 在干什么: ① **E8 换闸门量纲**：`edge` 档实测**不可达**（7 场对照 P−market_fair 全负，
>           最大 −0.23pp；10-19 起连续 131 轮零合格），改成
>           `ev_site = P×站点结算价 − 1 ≥ 2%`（上界 25% 判脏价），开关在
>           `data/state/challenge_policy.json` 的 gate_mode，改回 `"edge"` 即停实验。
>           **赌的是盘口软（站点价比体彩价中位高 11.6%），不是模型变强**；
>           判定标准已写死进 docs/EXPERIMENTS.md E8 行，不许边跑边改。
>           E8 已真实进账（17:35 口径）：E8 池 n=4（12:05 三注 + 12:35 格拉茨风暴
>           vs 马赛 ev_site +2.64%），E10 池 n=3（利勒斯特罗姆客胜@1.30 +5.93%、
>           萨迪纳摩主胜@1.38 +4.50%、16:35 波兹南莱赫客胜@1.49）；W43 配额已用 8/28
>           （另 1 注为实验前旧记录，不计入任一样本）。**E10（用户口头称 E9，ID 已被侧挂实验占用）同日开闸**：
>           旧筛「体彩价≥1.60」在 ev_site 下会漏掉正 EV 低价热门（10-22 萨迪纳摩
>           vs 奈梅亨 体彩 1.42/站点 1.62/ev_site +4.8%）；E10 候选单独成池、排在
>           E8 之后吃剩余名额，记录 `exp="E10"`，E8 候选/排序/上限不变。判据冻结在
>           EXPERIMENTS E10 行：n≥60 或 11-22，ROI>0 且单侧 p<0.10 → 永久移除旧筛。
>           ② 三份 njstats 快照（site_odds/my_predictions/community）**从来没被提交过**，
>           只活在笔记本工作区（duty 的 commit 链原先只认 challenge_state.json）→
>           抽 `commit_push()` 统一；并把「下单那一刻的站点价」写进 records——
>           每日快照是覆盖式的，存的是最后一轮的价，不是成交时的价。
>           ③ PR#9 扩 `--days` 判失败：10 天后收盘覆盖 3.6%→3.5%，真因是采集**次数**
>           （sina 只挂 daily-forecast，24h 实际 12/48 次）→ fetch_sina_odds 同时挂进 ci-sync。
>           ④ DJYY 已恢复：上游 Cloudflare 对 Worker 出口是**整体** challenge
>           （`x-proxy-attempts: 3` 三次全 403，连首页都 `cf-mitigated: challenge`，
>           ORD/IAD/SJC/DFW 一致），header/cookie/重试/密钥都不是变量；此前偶发 200
>           只是 Worker 响应被边缘缓存 60s 的假象。修复：duty 用本机网络抓取
>           （urllib 默认走系统代理，实测比 Python 直连快 5 倍：单请求 4.6s vs 26s；
>           2026-10-23 并行化 comparison 抓取后 200 场约 2.5 分钟完成，原先串行会撞 600s 超时），
>           超过 10h 即刷新并提交快照，CI 只消费仓库快照。10-22 实测本机直连 200 场、
>           200/200 带 Pinnacle 赔率与模型概率；预测侧 fusion 在实时源缺失时用快照
>           `djyy_model_prob` 补位，第三通道不再被重新归一化掉。E8 在恢复前 n=0，
>           无样本污染；恢复会改变 P→ev_site，后续按 E8 护栏③记 edge/ev_site。
>           **DJYY 已收尾**：① 补「萨迪纳摩→萨格勒布迪纳摩」别名（只加 sources 层，
>           不合并 team_ratings 键），当日预测富化 12/13（唯一缺口是弗拉门戈 vs
>           拉普大学，整场不在 DJYY 快照池里，不是别名能修）；② 快照 `extracted_at` 暴露、
>           日志打快照龄并过期告警，补位事实写进 djyy_status；③ health-check 只在
>           「实时被拦且无补位」时报 error，selfcheck 存活率改看 djyy_model_prob
>           （实时或快照），不再在通道健康时误报；④ comparison 接口已无 xG
>           （快照 0/200），xG 字段恒空但不阻塞概率通道。
>           已知覆盖缺口：弗拉门戈 vs 拉普大学 不在 DJYY 快照池内（非别名可修）。
>           **不需要再配 DJYY_KEY 或 Worker 白名单。**
>           ⑤ 删 `harness/`（DeepSeek 值守 agent）——`deepseek_key` 不存在，每轮都走降级分支；
>           并发锁搬进 `njstats_duty.py`（固定路径 `~/.football-engine/duty.lock`），
>           launchd 改为直接跑脚本，Qoder 自动化接手「每日 4 次巡检」+「E8 每日对账」。
>           ⑥ **rho 写回丢失已修（91c1556ad）**：rho MLE 每轮写 config/prediction.json，
>           但 Actions 工作区一次性、commit 只 add data/ web/ → 线上 rho 自 10-12 停在
>           -0.0748（实测拟合 -0.0711, n=1142）。现提交步补且只补这一份配置
>           （存在才 add），回归测试 tests/test_rho_persistence.py。
>           **2026-10-23 03:33 已验证**（2d22b075f）：10-22 赛果结算后 rho 从 -0.0748
>           跟到 -0.0711 并随 daily 提交，持久化链路真跑通。
>           ⑦ **双端写 challenge_state 的两处静默丢记录已修（PR #53）**：CI 的「状态回仓」
>           原先只 `git add` 从不 `git commit`，push 的是 checkout 出来的旧 HEAD →
>           永远 `Everything up-to-date`（退出码 0），CI 投的注一条都不进仓而步骤是绿的；
>           duty 的 `commit_push` 则在同轮冲突后不 `rebase --abort`，仓库停在 rebase 中间态，
>           下一轮 pull 直接报 unmerged files，这台机器再也提交不了任何东西。
>           现在：CI 真提交、冲突红一轮（不用 -X ours/theirs 猜丢哪侧）、duty 冲突必 abort
>           并把本地提交留原地重放；CI 排程 :35→:25 + `concurrency: challenge-1x2`
>           （本机 duty 在 :05/:35 写同一份文件，CI 开始真提交后同分钟必撞车）。
>           **实测今日样本零丢失**：站点「已预测」40 条 vs 仓库 records 35 条，缺的 5 条正是
>           W42 那批已结算历史缺口（马竞/比利亚雷亚尔/朗斯/阿森纳/莱比锡），今天 7 注全部由
>           本机 duty 落仓——本修复是止血，不是追回旧账。
>           ⑧ **对账兜底（2026-10-23）**：本机 22:40 自动化在关机/休眠时不跑（10-22 关机，
>           00:09 开机补跑还撞上 session-failed）。daily-forecast 增加一步只读的
>           `scripts/challenge_reconcile.py`：E8/E10 速度与打平线、已结算命中率/ROI、
>           最近快照龄与账目缺口。不依赖 JWT、不新增调度；实时服务端真值与零合格归因
>           （duty.log）仍只在 Mac。Qoder 对账自动化改为 **22:40 + 08:40 双跑**补漏。
> 新坑: ① 本机**没有 gh CLI**，AGENTS.md 里那些 `gh ...` 全跑不了（已改写交付节）。
>       ② `odds_coverage.not_started_yet` 原先语义与名字相反（它数的是「末快照早于开赛」），
>          这次复核就是被这个字段名骗过 —— 已拆成 assessable / pending_kickoff /
>          stopped_before_kickoff / post_kickoff_last。
>       ③ 站点板 333 场 ∩ 我方预测 13 场 = 可评 10 场：**闸门放开后瓶颈变成覆盖率**；
>          10-22 那轮新口径只放 1 场。④ `rank_diff` 恒 0（djyy.py 解出了 _rank_home/_rank_away
>          但 main.py 一个没传）—— 已接；**LGBM 仍 ready:false 是影子验证的正常裁决**：
>          holdout n=339，Brier 0.5583 vs 融合 0.5261 / 市场 0.5301（t=-2.85 / -3.41），
>          模型文件照常训练，上线闸门不开，不是故障。
> 下一步: ① 修复已全部合入 main（#37–#44、#50 DJYY 别名、#51 DJYY 观测、E10 PR）：
>           挑战池匹配 11/11、零合格日志带归因、DJYY 富化 12/13（1 场不在快照池）。
>         ② E8/E10 攒注（10-22 15:05）：E8 n=4、E10 n=2，周配额 7/28。两池目标
>           均为 n≥60 或 2026-11-22 收尾。**每日对账必须按 `exp` 拆池：无 exp 视为 E8，
>           `exp="E10"` 只进 E10 裁决**，否则两边抽样混在一起。每小时确定性保留在
>           launchd（本机合盖会漏轮，10-21 实测 20:00/21:00 零日志）。
>         ③ 周体检（周一 03:30）已修为读-合并-写 policy，不再洗掉 E8/E10 开关。
>         ④ 独立信息源（DJYY 已不再阻塞）：context 层仍是 store-only（72/1163 条带 context，
>           `FusionInput` 无字段）。**新闻展示层 2026-10-23 落地**：`scripts/fetch_news.py`
>           抓直播吧足球列表（当日更新、无需登录），按预测队名 + `TEAM_ALIASES` 简称 +
>           挑战池译名反查匹配，落 `data/daily/<date>/news.json`；daily-forecast 每轮跑，
>           页面「赛前情报」展示最多 12 条外链（标题转义、只允许 zhibo8 域名、带免责声明）。
>           **只展示，不进模型/账本**；旧 `leisu_intel`（0 命中、依赖本机 Chrome、无读取方）作废。
>           Qwen 摘要未接——等展示层跑出覆盖率，再决定是否按输入层热插协议立实验。
>           **SportMonks 是付费墙不是代码**：token 本机可读、CI 已挂，但 90 个
>           `data/state/sportmonks/*_details.json` 全空 `{}`，odds/xg/predictions 未订阅。
>           **2026-10-22 裁决：不订阅、不接生产**——现有 token 仅见丹麦超/苏超 4 个联赛，
>           xG 403、Pinnacle 赔率/predictions 拿不到；且 DJYY 后端同为 SportMonks，
>           快照已覆盖 Pinnacle 赔率+模型概率，试用到期即断供。登录页已关闭。
>           **NJStats 侧挂特征也已断供**：njstats_features 试用 10-15 到期，10-14 后
>           无快照（10-21 当天 model_eligible=0）→ E9 已在 EXPERIMENTS.md 标「阻塞」，
>           解阻只能恢复采集或换源，不降覆盖门槛。
>         ⑤ 待用户裁决：让球注 records schema（play/line）；`mult()` 赔率档倍数与站点
>           33 注实盘得分对不上（而排序正在用它）；账本 `djyy_prob` 的 null 是**永久的**
>           （append 去重、backfill 路径实测 0 行生效）要不要回填 = 改考卷；
>           社区倾向**不能**接进模型层（docs/CHALLENGE_SUBSYSTEM.md:18 引擎永不读挑战产物，
>           tests/test_challenge_firewall.py 执法）。
>           E8 截止风险已裁决（2026-10-22 15:50 预登记，非事后改判据）：
>           11-22 若 n<60，同一门槛延期至 12-31，**不回退 edge**（已证不可达）；
>           12-31 仍 n<60 判「证据不足」，ev_site 保留为运行口径待人工裁决。
>           量化：10-13~10-20 用体彩×1.116 近似回测，E8 日均 2.1 注
>           （1/1/3/3/2/5/0/2），11-22 期望 ~65 注，方差下约 1/4 概率不足 60；
>           延期后 ~147 注，风险解除。`scripts/experiment_pace.py` 每日报
>           速度/所需速度/预计 n，已挂进 22:40 对账。E10 同步预登记延期。
> 之前: ① **复盘方向解析静默失败已修**（e088572e1）：challenge_review.py 原来靠
>           子串猜方向（startswith('主胜')/`'负@' in pick`），实测 9 种真实格式
>           4 种解析失败且**静默返回 None**——None 比 actual_idx 永远 False，
>           把命中的注算成未中。受影响：胜@2.98(考文垂)、胜@3.3(克拉约瓦)、
>           客+0.5@0.95(霍芬海姆)、让负(-2)@2.04(站点格式)。
>           新增 parse_pick()/judge_hit()：拆 {play,dir,line,side}，让球用比分+
>           让球线判（净胜1球是走盘不算命中），信息不足返回 None 不是 False。
>           比分取自 data/daily/<date>/results_sina.json（账本只有 total_goals）。
>           **战绩从 24 注 12 中(50%) → 26 注 14 中(54%)**，找回的两注与站点
>           真值一致（霍芬海姆+195、亚特兰大+195）。
>           ② 写入侧 challenge_hourly 的 records 加显式 "play" 字段+注释。
>           历史 5 条让球注被套用 "1x2"/None 是 ZCode 时代人工分支遗留。
>           ③ market 4/5 = 让球；站点赔率快照上线
> 在干什么: ① **market 4/5 = 让球**（更正前稿「口径不明」——那是我只拿赔率反查、
>           没去站点翻实盘）。站点「已预测」页明写：阿森纳 vs 里尔「让负(-2)@2.04」、
>           莱比锡 vs 埃因霍温「让负(-1)@2.03」。market 4=让球主队向、5=让球客队向。
>           那 5 条不补记的理由改为「本地 schema 装不下」：challenge_hourly.py:182
>           硬编码 market="1x2"、line=None，让球注被记成 1x2（是错记不是漏记）。
>           自动链路本身没这问题（只投 1x2，api.submit 第二参固定 1），
>           那 5 条是 ZCode 时代人工让球分支遗留。已列待修，未动代码。
>           ② **站点赔率每日快照上线**：新增 api.parse_board_with_odds /
>           board_with_odds，duty 每天落 data/state/njstats/site_odds/<date>.json
>           （实测 216 场 190 场带赔率）。存它是为了将来验证「换站点价算 EV
>           能否提高命中」——站点价只在当前板可见，赛后就变。
>           ③ **站点完整对账**（写进 docs/CHALLENGE_ODDS_GAP_20261021.md 附录）：
>           33 注已结束、17 中 16 未中（51.5%）；1x2 31 注 16 中=51.6%，
>           让球 3 注 2 中=66.7%（样本太小不下结论）。
>           ④ 赔率口径差异：本地系统性低估约 10%（18 条里 17 条，比值 1.0993）
> 在干什么: ① **赔率口径差异（重要发现，未改）**：本地算 edge 用 predictions.json
>           的体彩官方赔率，而挑战赛成交用 NJStats 站点自己的赔率。18 条对照：
>           成交赔率比本地记录高 0.186（比值 1.0993），**17/18 都是低估**。
>           影响：用体彩价算 EV 均值 −6.9%，用站点价 +2.8%，**低估 9.7pp**；
>           17 注里 12 注从负 EV 翻正。**但不改**：站点价无历史无法回测；
>           E3/E6 已证自称 edge 与兑现不相关；且挑战赛按赔率给分倍数，
>           高赔率对积分有利/对胜率不利是两个目标。详见
>           docs/CHALLENGE_ODDS_GAP_20261021.md
>           ② **账目缺口留档不补记**：服务端 33 vs state 28，差 5 条全是
>           market=4/5，口径对不上任何已知市场（阿森纳那场 1x2/让球都不沾）。
>           按「污染样本不得进校准」宁可留缺口。新增每日快照
>           data/state/njstats/my_predictions/<date>.json 防复发、可追。
>           ③ 服务端去重上线：根治 CI/本机重复投注
> 在干什么: ① **去重改走服务端真值**（bcb9f2c98）：msgId=11 是账号私有的
>           「我已预测」列表（无效 JWT 返回空 len=5，有效返回全部），与本地
>           state 无关、不受 git 同步影响。challenge_hourly 的去重名单改为
>           **本地 state ∪ 服务端列表**；取不到时退回纯本地并显式警告。
>           **实测缺口真实存在**：服务端 33 条 vs 本地 state 28 条，差 5 条
>           （朗斯vs葡体、比利亚雷亚尔vs那不勒斯、莱比锡vs埃因霍温、
>            阿森纳vs里尔、马竞vs曼联）——即 challeng_state 的 records_gap。
>           也就是说光靠本地 state，这 5 场一直处于「可被再投一次」状态。
>           ② 看门狗随之放宽（51a2ddfa9）：既然服务端兜住了并发，「本地零提交」
>           那道闸撤掉；保留「CI 超 3h」与「每小时最多一次」（防叠加/防挤配额）。
>           ③ CI 看门狗已上线（00113529b）：GitHub 对公开仓库定时任务限流严重
>           （Challenge 24h 只跑 3/24 次，启动延迟恒 0 秒 = 平台侧丢弃而非排队）。
>           duty 末尾在「CI 超 3h 没跑 + 本地这轮零提交」时用 PAT 手动 dispatch 一轮。
>           **三条约束都是防重复投注**：CI 与本机是两个独立仓库副本，去重只靠
>           challenge_state.json 的 records；本地刚投完就触发 CI 会让 CI 在没同步到
>           新记录的 state 上再投一遍（K=6，最多重复 6 注）。故：本地零提交才补 /
>           CI 超 3h 才补 / 每小时最多一次；取不到 CI 状态时保守不补。
>           `DUTY_SKIP_CI_WATCHDOG=1` 可跳过。marker `.ci_watchdog_last` 已 gitignore。
>           tests/test_ci_watchdog.py 6 例，三条 guard 变异验证 3/3 命中。
>           ② **duty 并发锁已上线**（efb979518）：`harness/run_duty.sh` 加
>           `mkdir` 原子锁（macOS 不一定带 flock），锁龄 >30 分钟自动清陈锁，
>           `trap EXIT INT TERM` 保证释放。此前 run_duty.sh 与 njstats_duty.py
>           都无锁，手动跑撞 :05/:35 会让两进程同时 git pull --rebase/写 state；
>           实测撞过一次（08:35:03 与 08:35:10 并存 7 秒，侥幸没事）。
>           三种情形实测：正常跑释放 ✓ / 锁被占跳过 ✓ / 陈锁清理 ✓。
>           ② **E7 rho 自进化结案：无实质收益，维持现状**。澄清：main.py:2320-2327
>           每轮写回 config，但 daily-forecast.yml:193 只 `git add data/ web/`，
>           写回无人提交、被 autostash 卷成残渣——**这个「没提交」不是 bug，
>           是有利状态**。账本 1135 条配对回放：现值 −0.0748 vs 当日拟合 −0.0734
>           的 ΔBrier −0.000011（t=−0.93）；网格最优 −0.0160 的 ΔBrier −0.000225
>           （t=−0.47）；75.9% 场次同向改善但幅度淹没在噪声里。
>           量纲：rho 动 0.0014 只让主胜概率变 0.011pp，比门槛 1.5pp 小两个数量级。
>           已登记 docs/EXPERIMENTS.md E7 行。**别再"修"这个写回了。**
> 之前: ① **社区倾向每日快照已上线**（95616b3b7）：新增
>           scripts/njstats_community_snapshot.py，duty 末尾每天跑一次（date 门控 +
>           置于 1x2 之后，失败不影响提交链；DUTY_SKIP_COMMUNITY=1 可跳过）。
>           站点无历史接口（msgId=17 不接受日期参数，三种写法返回字节数完全相同），
>           只能自己攒样本。首日：813 场中 12 场有分布，8 场拿到 DOM 方向真值。
>           **重要教训**：f1 选项码不跨场次同义，方向名只在 DOM 里——脚本走
>           API+DOM 双取（API 存结构、DOM 贴方向）。中途两次猜错（f1=1主/3客、
>           取全表 pct 最大）都是靠 DOM 真值纠正的。
>           ② **E6 门槛上限实验结案：不加**。账本 1018 条配对回放，B(1.5~3pp) 兑现
>           +2.32pp vs C(≥3pp) −4.23pp 看似该加上限，但 Welch t=+1.13、Bootstrap
>           95%CI [−4.63,+17.97]pp 跨 0；分月看 C 档 8月−7.3 / 9月+0.0 / **10月+6.3pp**，
>           整体负值由 8 月 75 条样本拖动。已按规则登记 docs/EXPERIMENTS.md，未删行。
>           ③ **修正两条我报错的结论**：(a)「体彩只开 15 场」不成立——fixture_count
>           实测 0~59 波动（中位 14），用户指正正确；(b)「赛程源覆盖不足是 bug」
>           不成立——跨日过滤用的是竞彩比赛日（match_id 前缀）而非开赛日，体彩 21 场
>           按周二/周三分组后 10-20+10-21 合计 23 场完整覆盖，一场不漏。
>           fallback_used=True 只表示字段来自 priority>1 的源，**不代表降级失败**。
>           ④ GitHub cron 漏跑复测：启动延迟恒为 0 秒（created_at 即触发时刻），
>           但触发点分钟数散乱（challenge 偏差 −34~+23 分），24h 内仅 3/24 次。
>           属平台侧调度丢弃，workflow 内无法修。
> 之前: session 自动续期已合入 main；ZCode 自动化清空：
> 之前: 已合入 main（五项修复）：
>           ① W42→W43 周翻转未落盘 → challenge_hourly.py:55-67 翻转即落盘（+3 例回归测试）
>           ② 复盘器读 _vault 快照 → 云端复盘从未真跑过 → 改读入库 data/state/，去掉 `|| true`
>           ③ 本机 cron 与 CI 抢写 → 已删 cron + doctor 检查反转为「不许有」+ djyy 缺文件显式跳过
>           ④ 文档一致性：docs/CHALLENGE_SUBSYSTEM.md:15/16/39、STATUS.md:258 里指
>              _vault/challenge_state.json 的过时描述一并对齐到 data/state/
>           ⑤ 审计产物时间戳「北京日期 + UTC 时分」超前 8h → doctor.py:71 / clv.py:229
>              统一走 beijing_now()（92e5c93c5，+1 例变异验证过的回归测试）
> 已解决: ① **rho 自进化写回丢失（2026-10-22，91c1556ad）**：engine/main.py 的 RhoFitter
>             每轮把 rho 写回 config/prediction.json，而 daily-forecast 的 commit 只
>             add data/ web/，Actions 工作区又是一次性的 → 写回从未到过 main。
>             修法取「让 daily-forecast 提交 config/prediction.json」（只此一份、存在才 add），
>             不动拟合规则与触发时机；回归测试 tests/test_rho_persistence.py。
>             现状 rho=-0.0748（10-12 起未变），本机实测拟合 -0.0711（n=1142）。
>          ② duty 并发锁已上线（efb979518）：锁在 scripts/njstats_duty.py（mkdir 原子锁），
>             launchd 直跑脚本，harness/run_duty.sh 已随 harness/ 整体删除。
>          ③ 早前汇报的「W42 1x2 2/7」作废——那是 _vault 陈旧快照的产物；
>             真值是跨周累计 22 注 11 中（50%），单周口径需 records 带 week 字段才能算
>          ④ GitHub PAT 已装（~/.football-engine/github_token，600，scope repo，已验证推 secret 204）；
>             仓库 deploy key 曾整条消失（API 查 deploy keys: 0）导致 SSH 全断，
>             已用 API 重新添加（id 166885983），fetch/push 已复验通过
> 已知坑: ① token 20h 过期，Air 上每日刷；本机非常开不能承担定时任务。
>           session 死（mint 401）≠ 需人工登录：pwprofile 的 remember-me 可无头重签，
>           见上方「在干什么」——只有 remember-me 也过期（约 14 天）才需人工跑 seed_login
>          ② git reset --hard 会清归档（发生两次）；reset 前跑 njstats_safeguard.py
>          ③ prob 归档 label ≠ 快照时刻，下游时序以 snapshot_at 为准
>          ④ challenge_hourly 的 STATE.write_text 原先只在「有注提交成功」时执行，
>             零合格轮不落盘——已由 6a1570e8b 修复，此后翻转即落盘
>          ⑤ weekly-deep-check 的 `|| true` 曾长期掩盖复盘器的 FileNotFoundError——
>             同类「失败被吞」的 step 值得逐个复查（已复查 djyy_audit，同样问题）
> 待验证: ① 让球走盘口径已实测（走盘=0 分），待改 handicap_ev ← 见下方既有条目
>          ② 巴甲平局回升（仍无 10-11 后巴甲结算样本）
>          ③ rsync/自动灌仓遗留开关已清（CHALLENGE_TOPFILL 无 env 残留，W42-fill 已关）

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
- **main 不加分支保护**（2026-11-04 事故结论）：保护会拦 `github-actions[bot]` 直推，而 duty/三条
      CI 回仓都靠直推；owner 探针验不出这一侧。小号「只能提 PR」靠 fork 交付 + 上游零写权限实现
- 运维摘要：duty 每轮跑 `scripts/ops_digest.py --write --quiet` → `~/.football-engine/ops_digest.{txt,json}`
      （含 `red_flags`，每条带人的动作）；`DUTY_SKIP_OPS_DIGEST=1` 可关。红线同时写 duty.log
- Qoder 自动化：原「巡检 4 次/天」「E8 对账 2 次/天」已**禁用**（prompt 原文保留，回退即重新启用）；
      现役只有「竞彩运维分诊」每日 22:40 一次（`independent`、只读、无红线只回一行）
- 第二运行时（未起服务）：`~/.dsh/profiles/superagent` + bundle `~/src/dsh-superagent`；小号 `dykily`
      的 gh 凭据只在 `~/.config/gh-agent`（默认 `gh` 刻意保持未登录）；启动前必须过
      `scripts/agent_identity_preflight.py`

## 已知未修（按优先级）
> 2026-10-13 复核：前 4 条**已解决**，移到底部「已完成」；下面是真正还开着的。

- [ ] **`main.py` god object**：`run_daily_pipeline` 1225 行 + `run_settlement` 955 行。
      风险：任何改动都要在 2000+ 行里定位，且两处逻辑容易各写一份（同口径漂移的高发区）
- [x] ~~SportMonks Growth 试用激活~~ → **2026-10-22 裁决不订阅**：现有 token 仅见
      丹麦超/苏超 4 联赛，xG 403、Pinnacle 赔率/predictions 不可得；DJYY 后端同源，
      快照已覆盖，试用到期即断供。不接生产、登录页已关（docs/CLOUD_PLAN.md）。
- [ ] git 历史端点路径清洗决策（HEAD 已清，历史待用户裁决是否 force-push 重写）
- [ ] AUT 平局纸面证据累计（双源佐证 +7.5%/+9.24%，t≈1.7 候选，live 证据持续累计）
- [x] ~~LGBM ready~~ → 2026-10-22 复核：`ready:false` 是影子验证的正常裁决，
      holdout n=339，Brier 0.5583 vs 融合 0.5261 / 市场 0.5301（t=-2.85 / -3.41），
      模型照常训练、上线闸门不开；不是接线 bug。
- [ ] 洁净样本累积 / 新闻进模型：覆盖率已量出（14 天 217 场，任意新闻 **45.2%**、injury **18.0%**，
      PR #74），旧上限 40 截断已提到 120 并记录 `matched_total`。**E11 草案在 PR #74 描述里，未启用**；
      开跑前置：E8/E10 先收尾（中途加特征会污染冻结样本）、重测覆盖 ≥70% / injury ≥35%、
      特征需 `has_news` 指示位区分「无覆盖」与「覆盖且 0 条伤病」
- [ ] dsh-superagent 接入停在半路：bundle/profile 已装、身份不变量与 PR 闸门已上线，但 **fork 未建、
      服务未起**，小号对上游只读。剩余：建 fork → 单独 clone 到 `~/agentbox`（**不要复用 duty 那份
      clone，worktree 会继承大号的 SSH remote**）→ 身份检查全绿 → 配 Sources/Policy（observe、
      只允许 wlrwx）→ 起服务 → 第一张工单验「只能提 PR」
- [ ] 残留安全面（用户裁决不建独立 macOS 账号）：同 OS 用户下的 agent 理论上能读 `~/.ssh` 冒充大号
      直推 main。现有兜底是 main-watch 报警 + 身份不变量检查 + 小号上游零写权限，**不是硬隔离**
- [x] ~~NJStats 自建策略回测的对照臂需要会员~~ → **2026-11-09 用户裁决：不开会员**。本账号
      `msgId=1028/0` 的 tab=2 稳定返回空（非会员不能创建，官方 5 个基础策略照常可读），
      **不绕过、不撞写入端点**，永久只有官方 5 个策略可作外部对照。每日快照把
      「0 条是权限状态」写进 `mine_empty_means`，将来若开会员也能区分「从没建过」与「建了又删」。
- [ ] 10-31 三份逐时快照永久缺失（断网当天，不可补）：E8/E10 配对回放少一天；速度分母按真实观察
      天数计，不要试图伪造补齐
- [ ] 站点赔率覆盖 84%（184 场里 29 场无赔率）：成因是**赛事级别**覆盖（亚冠二级/东南亚/中东俱乐部
      赛事为主；青年队只占缺口 8/29，而带赔率的 155 场里反有 30 场青年队）。**不是抽样瓶颈**——
      瓶颈是与体彩预测的交集（匹配5/173）与价差（最高<2%）。裁决：不改采集口径
- [x] ~~SportMonks `fixtures_on` / `fixture_odds` / `injuries` 未接入~~ → 随不订阅裁决关闭。
      **不要凭文档猜**这三个方法；真要接需先有能覆盖竞彩联赛的套餐与 token 再实测
- [ ] 静默吞异常（2026-10-22 AST 重扫：**256 处**，旧记的 98 是漏算）——**不报错，只是没产出**，
      是本项目的头号病害。**不要批量改**（会淹没真正需要静默的地方）。
      2026-10-22 已把**预测/结算主链路 10 处**改成留痕（详见「已修」），
      剩余集中在 build_site（30+，页面渲染兜底）、odds_series（逐行跳过，语义正确）、
      以及 xG/阵容/反向分析等可选增强——那些失败本就不该刷屏。
      参考 `engine/sources/djyy.py` 的 `_record_error()` 模式继续按关键路径推进

## 今晚已解决（原在待办清单里，复核后移出）
- [x] issue #1 无盘口标注（PR #35：`_pred_pick` 加「⚠ 无盘口·不可投注」；页面已验证 1 处）
- [x] `kelly.py` 口径（量化裁决：**不改**，依据 docs/KELLY_CALIBER_DECISION_20261013.md）
- [x] 开盘价缺口（DJYY comparison 的 8 个市场带 Pinnacle/bet365 开盘价，**零额外请求**）
- [x] `evolution/` 产物（已 gitignore）

## 已修（现象 → 根因 → 修法）
- 2026-11-10 **别名表漏一条，白丢一注**：当天摘要报「匹配0」，但预测其实已产出（2 场：
  伯顿 vs 诺茨郡、布罗姆利 vs 米尔顿）。站点挑战池里同一场写的是 **伯顿 vs 诺斯郡**——
  同一支 Notts County 的两种译名，`engine/team_aliases.py` 没收录 → 归一化后对不上 →
  该场静默丢失（另一场布罗姆利确实不在站点池，属站点覆盖缺口）。修法：别名表加
  `"诺茨郡": "诺斯郡"`（带实证注释），`test_team_aliases.py` 的 CONVERGE_PAIRS 同步加配对，
  防止回退。
- 2026-11-10 **摘要把「有预测但零命中」也误报成「预测没产出」**：昨天为「无在售」加的
  `prediction_pending` 只看 `import_manifest` 在不在，今天 predictions.json 明明在，
  文案却断言「没有 predictions.json」。改成**三分法**（用 daily 自己的产物，不猜时钟）：
  predictions 在 → `pool_no_overlap`（有预测但零命中，先查译名/站点覆盖，别当数据洞）；
  predictions 缺、manifest 在 → `prediction_pending`（当日无在售，属正常）；
  两个都缺 → `prediction_coverage_gap`（真数据洞，11-04 回仓被拦那种）。前两者归 `HUMAN_ONLY`。
- 2026-11-09 **登录红线改成「提前预警 + 主动续期」**：原先只在 mint 失败后才无头续期，
      等发现时缓存 JWT 最多剩 20h，人去处理已经很赶；而 remember-me 是否还有效，也只有
      真去续一次才知道。现在 duty **每 12h 主动续一次**（距上次成功 >12h 才续；未成功时
      最多每 6h 重试一次，避免厂商 5xx 期间每轮起浏览器），结论写
      `~/.football-engine/njstats_session_health.json`：`ok`=登录态新鲜；`auth_dead`=无法
      自动续期 → 摘要出 `njstats_session_expiring`（`HUMAN_ONLY`，动作「缓存 JWT 还有约
      20h，择机 ./fe-agent seed」）；`vendor`=厂商 5xx/网络，**不喊人、不覆盖**登录态
      （旧版把 502 判成「登录态已失效」，既喊人白跑又会拿空结果覆盖能用的 SESSION）。
      人工播种成功也会把 health 置回 `ok`，红线立刻消失。2026-11-09 晚 502 实测：
      续期脚本正确返回 `vendor`（`HTTP 502（5xx/网络，与登录态无关），本次不续不覆盖`）。
- 2026-11-09 **播种登录「登录成功却保存不了」**：用户登录后跳空白页、脚本不返回成功。
      根因：旧 `njstats_seed_login.py` 只靠 `/api/user/current` 判成功、且只存 SESSION cookie。
      厂商那个端点 5xx 时（11-04→07、11-09 都挂过），登录其实成功、token 已写进 localStorage，
      脚本却永远判不了成功，又因抓不到 SESSION 一直空转到超时。修法：优先读 localStorage 的
      token（登录成功前端必写，不依赖厂商端点），拿到就**必存 JWT**；SESSION 有则一并存、没有
      不阻塞。启动时清残留 token，避免把旧 token 误当已登录。本次已用无头浏览器从用户刚登录的
      profile 里取出 token 存好并实测（私有端点 60 条/全板 44 场）。新增 6 条假浏览器行为测试。
- 2026-11-09 **「匹配0」被误报成数据洞，还派了张查不出故障的工单**：当天 duty 全天报
      `匹配0/候选0`，`data/daily/2026-11-09/` 只有 `import_manifest.json`、没有 `predictions.json`。
      **实测结论：当日竞彩无在售，属正常，不是数据洞。** 证据：直接调
      `SportterySource.fetch_fixtures(2026-11-09)` 返回 4 场，编号日期全是 `2026-11-10_周二001/002`、
      `2026-11-11_周三005/006`（11-11/11-12 凌晨开球）；当天 00:53 那轮体彩 0 场、05:06 那轮 4 场
      全被「编号日期 != 今天」正确过滤。站点挑战池里当天有 40+ 场，但那是站点自己的赛程，
      我们只预测竞彩在售场次（既有设计，见 10-13「联赛不重叠」）。
      缺陷在**摘要**：旧代码把「匹配0」一律归 `prediction_coverage_gap` 并写死「数据洞」，
      派单器当晚就在小号仓库建了工单。修法：用 **daily 自己的产物**区分，不猜时钟——
      `import_manifest.json` 在 = 跑过但没产出 → 新 code `prediction_pending`（动作：先查体彩
      当天是否有在售；无在售=当日无机会属正常，有在售却没产出才查 CI/回仓），归 `HUMAN_ONLY`
      不派 agent；manifest 也不在 = 没跑/没回仓 → 仍是 `prediction_coverage_gap`
      （11-04 分支保护那次的形状）。归因行文案同时去掉「数据洞」这个未经验证的断言。
      **代价记一笔**：无在售日 E8/E10 攒不到注，速度分母按真实观察天数计，不要伪造补齐。
- 2026-11-08 **服务端真值快照断更三天，而每轮摘要都「无红线」**：11-04 的快照有 60 条，
      11-05/06/07 **三天没有任何快照文件**，11-08 才恢复——而 11-04→11-07 正是厂商
      `/api/user/current` 502 的四天（见下条），私有端点 `msgId=11` 同样非 200，
      `api.my_predictions()` 返回 None，旧代码在 `if recs is None: return` 处**静默返回**：
      不写文件、也不写日志。摘要侧的 `snapshot_line()` 于是读最新的旧文件，报
      「快照 60 条/48h（可能过期）… 仓库未记 0 条」——「未记 0」是按那条旧快照的条数遍历出来的，
      读起来像账目干净，红线列表里也没有任何一条。
      **证据形状要记准：不是「日志里天天写 0 条」（旧代码成功路径根本不写日志），而是
      没有任何日志、也没有任何文件**——这种静默比「写了个 0」更难发现。
      另一个同族形状（11-08 用过期 JWT 实测）：`msgId=1` 全板照常 232 场、`msgId=11`
      HTTP 200 + 空 body，旧代码会把 count=0 写进当天快照，覆盖掉上午拿到的真值。
      修法（四处，都不改判据）：① duty 两条路径都必须留字——取到数记「服务端真值 N 条」，
      取不到记「私有端点取不到」，空返回记「0 条而本地有 N 条注单」；② 空返回**不再覆盖**
      当天已有快照；③ `ops_digest.truth_check()` 取近 24h 最后一次自述，仅「0 且本地>0」时发
      `truth_snapshot_empty`；④ 新增 `truth_snapshot_stale`（快照龄 >26h 或从未落盘、且本地有注单）
      专治「几天没文件」那种静默；若同一轮已有 `error_upstream`（厂商 5xx 已在报）则不重复报，
      因为修法是等厂商而不是查我们。两个 code 都归 `HUMAN_ONLY`。
      顺带修一个同函数里的控制流坑：早退 `return` 会跳过后面的站点赔率快照（真值一坏就少
      一天赔率样本），现已保证赔率块无条件执行，`test_empty_server_truth_alongside_odds_snapshot_still_runs` 钉住。
      **另注：`_cached_jwt_usable` 故意没动**——加 msgId=11 探针判「非空」会在账号真的没注单时
      误报登录失效，而「私有端点返回空」这个形状从探测函数的返回值里区分不出来；探测的职责是
      「还能不能下单」（看 board），检测真值快照异常的职责在摘要侧。
- 2026-11-08 **第四份 njstats 快照差点又没人提交**：站点 11 月上线的「自建策略回测」把官方 5 个
      基础策略的定义与回测放在 `modId=1028`，站点**没有历史接口**（每次都是截至当下的全量重算），
      不每天存就永久丢失。新增 `scripts/fetch_njstats_strategies.py`（store-only）+ duty 每日接入。
      根因不在"少一个脚本"，在于**名单是人写的**：`test_three_snapshot_dirs_cover_the_duty_outputs`
      只手写三个目录名，第四份进来时它照样绿。改成扫 `scripts/*.py` 里所有
      `data/state/njstats/<x>/` 写入点并要求进 `SNAPSHOT_PATHS`（`test_every_njstats_snapshot_dir_is_committed`），
      以后加快照目录不登记就直接红。
- 2026-11-04 **给 main 加分支保护 → CI 回仓全灭（自己造成的事故，务必记住）**：为接 dsh-superagent
      想让小号「只能提 PR」，用经典分支保护开了「必须走 PR + 1 批准 + 必过检查」。owner 的 SSH key
      是 admin 可绕过（duty 一路绿），但 `github-actions[bot]` 的 `GITHUB_TOKEN` **不是 admin**，
      直推被 `GH006: Protected branch update failed / Changes must be made through a pull request` 拦掉：
      `daily-forecast` #1627~#1630 连红四轮，11-03 的 `predictions.json` 没落库 → duty 连续「匹配0」，
      11-02 的 `results/results_sina/review` 也没生成。处置：撤保护（一条 DELETE）→ 重跑 #1631 补回全部数据。
      **结论：不要再给 main 加分支保护**；「小号只能提 PR」改用 **fork 交付**（小号推自己的 fork、
      对上游零写权限，结构上就合不了），main 保持无保护。教训：验 owner 侧探针**验不出 Actions 侧**，
      两侧都要实测才算验过。
- 2026-11-04 **health-check 连红 5 轮，红的是检查自己**：`football-data cache freshness` 步骤里 jq
      程序被写成两行，第一行末尾就闭合了单引号 → `jq compile error` → 整步 exit 3，**后面两条真判定
      （files_ok==0 / 停更>8天）从来没执行过**。数据其实健康（43/45、最新到 11-01）。修法：jq 合成一行、
      两条 `::error::` 补 `exit 1`（原来只发 annotation、步骤照样绿）、加 6 项真跑 bash+jq 的测试并做
      变异验证（旧代码确实报错）。PR #71。**写了检查 ≠ 检查在跑**——这条判定正是 10-28「缓存停更 19 天」
      之后补的，补上了却因引号一直空转。
- 2026-11-04 **ci-sync 回仓失败静默**：`git push … || echo "推送跳过（无新提交或竞态）"` 把权限/保护/
      网络失败与「无变更」混成一句无害提示，是上面那次事故的隐形放大器（同样被拦却一直绿）。修法：
      无变更明确 exit 0；真 commit + 3 轮 pull/push 重试；冲突 abort 后 `::error::`+exit 1；3 轮仍失败
      `::error::`+exit 1。staged 条件是「路径存在 **或** 仍被 git 跟踪」——只判 `[ -e ]` 会漏掉目录被删时的
      删除动作，导致 `pull --rebase` 报 unstaged changes（这个洞被新测试抓出来）。PR #72，测试用
      pre-receive 钩子模拟 GH006 拒推。
- 2026-11-04 **`--no-net` 名不副实 → CI 偶发超时**：`engine/doctor.py` 的 `check_git_state()` 无条件
      `git fetch origin main`，于是 `test_run_smoke_no_net` 在 runner 上真联网，偶发
      `TimeoutExpired … 120 seconds`（PR #72 的 ubuntu 与 windows 各中一次）。修法：fetch/push 只在
      `net=True` 下做，离线用本地 `origin/main` 引用并在结论里注明「可能滞后」；治疗动作同样按 net gating。
      新测试 spy `_git`，任何 fetch/push/ls-remote 直接断言失败。PR #73。
- 2026-11-03 **巡检/对账烧 credits：脚本化后降约 94%**：逐笔核会话计费字段发现 1.1.79 之后单笔从
      0.36 涨到 68~80 credits，而两条自动化（巡检 4 次/天、对账 2 次/天）做的事几乎全是确定性的。
      新增 `scripts/ops_digest.py`（复用 `challenge_reconcile`/`experiment_pace`/`challenge_review` 的
      **冻结判据**，只补 duty.log 轮次/异常/归因 + 配额与字段完整性），由 duty 每轮调用（48 次/天、
      零模型成本，覆盖比原来更密），红线写 duty.log。原两条自动化**禁用**（prompt 原文保留可回退），
      新增每日 22:40 一次的分诊（`independent` 每次新会话、只读、无红线只回一行）。实测：新分诊
      2 请求/33.7 credits，旧巡检 9~14 请求/77~129。另：原对账用 `merged` 模式，同一会话从 10-23
      续用累计 618 credits——上下文越滚越贵，换 `independent` 掐掉增长。PR #70。
      **真数据抓出的三个 bug**：①「匹配0」那类零合格行**没有「最高」字段**，正则要求它存在会让最该报的
      数据洞归因成 None ②EV 会写成 `+0.88%`（正号）③把日志**行数**当轮次（24h 276 行其实 46 轮）。
      对照证据：11-03 早上「匹配0」正是上面那次事故的数据洞，**旧巡检模型的报告写的是「全部正常，
      无需人工介入」**；新脚本把 `匹配0` 判为 `prediction_coverage_gap` 红线。
- 2026-11-03 **外部 PR 闸门 + main 写入者监督 + 大小号身份不变量**：`scripts/pr_guard.py` +
      `.github/workflows/pr-guard.yml`——自动化身份（`AGENT_AUTHORS={dykily}`）**永远**不得碰裁判层/
      策略状态/实验台账（不看标签，标签是 Write 账号自己加得动的）；人的 PR 碰这些需 `human-approved`；
      碰挑战子系统必须写「拔除影响评估」。duty 每轮核对 `origin/main` 新增提交作者，陌生作者/非快进
      都打 ⚠（**只报警不自动 revert**：第一次给第二个身份开写权限，没有坏样本可校准 allowlist，
      误判撤真提交比漏报严重）。基线从近 90 天真实历史种出来——实测含 `bot@football.engine`、
      `dsh-agent@…`、`jason@MacBook-Pro.local`，硬编码「wlrwx+actions」会把 duty 自己当偷推。
      `scripts/agent_identity_preflight.py`：小号只走 HTTPS+自己的 fork、对上游 push/admin 全 false、
      署名不得是大号那套、大号 remote 必须仍是 SSH 别名、环境不得有 GH_TOKEN。PR #68 / #69。
- 2026-11-04 **新闻覆盖率被采集上限低估**：14 天里 6 天条数正好卡在旧上限 40。上限提到 120，
      `news.json` 新增 `matched_total`（截断前总数）与 `limit`，否则永远分不清「源不足」和「我们截断了」。
      按场次实测：217 场预测里任意新闻覆盖 **45.2%**、injury 覆盖 **18.0%**。E11 预登记**草案**写在
      PR #74 描述里（未启用、未进 EXPERIMENTS 生效区）：必须等 E8/E10 收尾、覆盖率达阈值才允许开跑。
- 2026-11-01 **断网被误报成「需人工登录」**：10-30 17:35 → 11-01 11:35 本机网络被中间设备
      劫持（`SSL: CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`），
      duty 连续 81 轮 mint 失败，每轮都打「需人工登录（跑 seed_login）」——实际 session 没死，
      11:35 网络一恢复立刻 mint 成功。更糟的是这 42 小时把两个 Qoder 自动化打到
      `failureCircuit` 自动停用（已重新启用）。修法：`mint()` 用 `MINT_NET` 哨兵区分
      网络异常（退出码 0 + 日志「不需人工登录，下轮自动重试」）与 session 真死（20）；
      另加 `~/.football-engine/net_fail_state.json` 计时，日志一条就能看出「自 HH:MM 已 Xh」，
      恢复时打印总停机时长；`challenge_reconcile.py` 新增「逐时快照(近7天) 缺 N（日期）」，
      断网缺口在 CI 报告里也看得见；`fix_conflicts` 对 `challenge_state.json` 改为
      **两侧 records 并集**（同一注保留字段更全的一条、used/quota 取大），
      不再「取新生成侧」整份覆盖对侧注单。
      **断网实际损失**：10-31 的逐时站点快照整日缺失、DJYY 快照停更 49h（现已刷新 204 场）；
      CI 侧不受影响，期间投的 2 注（10-31 AC米兰 vs 国米、11-01 利物浦 vs 阿森纳）
      正是靠 #53 的「CI 状态回仓真提交」修复才进了仓（账目 0 缺口）。
- 2026-10-23 **daily-forecast 看门狗**：GitHub cron 限流导致 10-23 的预测一直没生成
      （上次 daily 是 10-22 23:15），挑战池连续两轮「匹配0/候选0」，页面/结算/rho 也停。
      duty 的 CI 看门狗扩展到 Daily Forecast：**北京时间今天还没跑过、或距上次超 4h**
      就补触发一轮（2h 冷却；daily 刚补过则本轮不再叠 challenge）。取不到状态时保守不动。
      回归测试 `tests/test_ci_watchdog.py` 新增 6 例。**生产已验证**：06:01 BJT 自动
      workflow_dispatch 一轮（距上次 4.5h），随后 06:45 schedule 正常。
- 2026-10-23 **新闻源（展示层）落地**：旧路径依赖本机 Chrome、10-11→10-18 每天 0 命中，
  且 football-engine 无任何读取方；Qwen 摘要也上不了 CI。改用**直播吧足球列表**
  （HTTP、当日更新、无需登录）→ 按预测队名 + `TEAM_ALIASES` 简称 + 挑战池译名反查匹配 →
  `data/daily/<date>/news.json`；daily-forecast 每轮抓取（失败转 `::warning::`，不阻塞预测），
  `build_site` 只展示外链标题（HTML 转义、域名白名单、免责声明），并按
  **伤病/首发 → 其他 → 转会传闻** 排序、带分类徽章与计数。实测 10-22：扫描 583 条、
  命中 25 条（其中伤病/首发 2、转会 6）。**不进模型、不进账本**；进模型要等覆盖率并预登记。
- 2026-10-23 **issue #1「竞彩未开售仍推荐串关」·显示层修复**：根因不是合成赔率
      （排查历史串关腿，踩中 `odds_synthetic` 的是 0 张），而是 fixtures 长期 fallback 到
      500/Bet365 参考盘——竞彩胜平负未开售的场次仍有真实参考赔率，串关照算 EV 并标
      「⭐ 正EV 推荐」。现在 `build_site` 用 duty 入库的 `site_odds` 快照比对
      （三位赔率齐全才算在售，匹配复用 `challenge_hourly.match_prediction`，同场定义一致）：
      含未开售/未确认腿的票降级为「⚠ 仅参考·竞彩未确认在售」，腿上加对应小标；
      无当日快照时保持原标注并提示「未验证」。**选腿、EV、账本零改动**。
      回归测试 `tests/test_site_market_label.py`（5 例）。快照是生成时点，之后才开售的
      场次会先被标参考——这是保守方向，实盘验证后再决定要不要升级成策略层硬门控。
- 2026-10-23 **赛果源全挂被当成「今天没比赛」**：`SourceManager.fetch_results` 原先对所有源
      异常都 `continue`，最后返回 []；结算层看到空列表直接「无新增赛果，快速退出」，
      账本静默不涨、校准/LGBM 不更新——与 sporttery/DJYY 之前的静默死亡同型。
      修法：给 `DataSource` 加 `trust_empty_results`（只有 sina 为 True，因为它用
      `raise_for_status()`/JSON 异常表达失败；500万/ESPN/DJYY 会把网络错误吞成 []）；
      仅当**没有任何可信源回答**时才 raise。结算主路径 main.py:1529 不吞异常 → CI 会红。
      回归测试 `tests/test_source_manager_results.py`（5 例，含「不可信空列表不能掩盖失败」）。
- 2026-10-22 **静默吞异常·预测/结算主链路 10 处留痕**：AST 全仓重扫得 256 处 `except → pass/return 默认值`
      （旧文档的 98 是漏算），按「失败会不会让输出静默退化」筛出主链路 10 处，只加日志不改控制流：
      ① `sina_odds.load_sina_odds_map` 整份赔率文件解析失败原先 `pass` → 市场通道整轮退化却像「今天本来没数据」
      （+回归测试 `test_corrupt_odds_file_is_reported_not_silent`）；
      ②③ `main.py` 联赛参数同步 / MatchDB 联赛基线同步失败 → 沿用旧参数无人知；
      ④⑤ 结算侧读 `predictions.json` / `results.json` 损坏文件被静默 continue → 预测索引与幂等基准悄悄变少；
      ⑥ **`results.json` 损坏原先直接当空列表覆盖** → 改成先改名留一份 `results.json.corrupt-<北京时间>` 再重建
      （这是唯一一处不只是加日志的：覆盖会永久丢历史赛果）；
      ⑦ 联赛命中/判平反馈写失败；⑧ 串关赛果映射失败（少一条腿→整注不结算）；
      ⑨ 赛果回写 `predictions.json` 失败；⑩ 账本无法解析的行按条计数后汇总告警（校准/LGBM 样本会少）。
      **未动的**：xG/阵容/反向分析/波胆 EV 等可选增强，以及 odds_series 的逐行跳过——那些本来就是局部坏数据，
      刷屏只会掩盖真问题
- 2026-10-22 **周度体检摘要周报化**（4270e7a7c）：摘要原先只打当次值，且 `upgrade_tracker`/`clv_report`
      每天被 daily-forecast 刷新，**git 里的上一版不是「上周」**，所以基线另存
      `data/state/weekly_digest.jsonl`（append-only，16 项指标 + 评委判定变化；E8/E10 行数
      复用 `experiment_pace.pool` 的冻结分池，rho 也在表里，用来验证写回是否真落盘）。顺带修回仓步：
      原 `git add a b c 2>/dev/null || true` 在任一文件缺失时**整条 add 原子失败**，把「什么都没暂存」
      伪装成成功 → 改逐文件 `if [ -e ] ; then git add`
- 2026-10-22 **挑战池队名匹配主客颠倒**：`challenge_hourly.py` 的模糊分支写成「池子主队比预测客队、池子客队比预测主队」，等于只有原文精确匹配生效——10-22 的 11 场预测只进池 6 场，另 5 场明明在板上（4 场译名差异：萨迪纳摩/萨格勒布迪纳摩、克里特/OFI克雷迪、利勒斯特伦/利勒斯特罗姆、拉普大学/拉普拉塔大学生；1 场子串：塞尔塔/维戈塞尔塔）。修法：方向纠正 + 走 `normalize_team` + 保守子串；池子译名映射只放脚本本地（写进 `engine/team_aliases.py` 会合并 team_ratings 的键，属模型层变更）；子串不跨梯队/女队（10-21 板上里尔与里尔U19 并存，不挡会一注变两注），同一预测只取第一条板条目。10-22 进池 6→11、候选 4→5，仍 0 场 ev_site≥2%
- 2026-10-22 **LGBM 特征「有数据、模型不读」四项**：① `TeamRating.form` 一直在 EMA 更新，但调用点从不传 `home_form/away_form` → 三个 form 特征恒 0（仅 45% 场次有非零值，全被浪费）；② `data/league_matrix.json` 33 个联赛、覆盖 65.6% 场次，`league_avg_goals/league_home_win_rate` 从不传 → 恒 2.5/0.45；③ 服务端 xG 取 `fixture._xg_home`（DJYY，403 时为空），训练端取落盘 `home_xg`（模型 xG）→ 同一特征两端异源；④ 排名只在服务端接线，训练端没传 → 训练 rank_diff 恒 0、服务端非 0。修法：服务端统一取一次 `_lgbm_inputs`（form/联赛先验/排名/模型 xG）随预测落盘为 `lgbm_inputs`，`build_training_rows` 优先回放冻结值；旧样本退回历史列（`form_home` 自 10-09 有值、`home_xg` 为模型 xG），联赛先验/排名无从追溯就保持默认，不用今天的表回填历史。`is_knockout` 仍无任何上游字段（`Fixture` 本身就没有该字段，采集层也没有赛段信息；只有 dict 版 `predict_batch` 契约里留着参数），属数据源缺口不是接线缺口
- 2026-10-13 **静默吞异常·关键路径第一处**：sporttery（竞彩官方主源）的 fetch_results 完全静默 return []、fetch_fixtures 只 print——被 WAF 拦/代理没配/结构变更三种病因在下游全表现为「今天没赛果」→ 结算静默跳过、账本静默不涨（与 DJYY 静默 3.5 周同病）→ 照 djyy 的 LAST_ERROR 模式接上，blocked_by_waf 与 fetch_failed 可区分；写测试时又当场抓到「成功后旧错误不清会永久挂红」并补上
- 2026-10-13 same_odds._load() 列名全错：读 match_id/result/league 而 odds.csv 里是 home_score/away_score/competition → 永远加载 0 条 → analyze 恒 0 → find_value_signals 恒空，功能从未真正跑通过且不报错；已改真实列名+比分推导（**0 → 29586 条**）。教训：**评估参数必须用真实数据**——3 个样本测出「容差要 0.15」，92 场真实数据测出 0.015→14%、0.1→91%，结论相反
- 2026-10-13 死代码终审（7 个「写了从未被调用」逐个实测）：fetch_odds_snapshot 真 bug 已修（1/p 把去水概率转成公平赔率当真实赔率存，偏高 3~4%=抽水量，与 sporttery 口径不一致）；same_odds 两条已修；get_model_probabilities 标注已取代；fetch_match_events 实测正常；sina.get_available_dates 可用未接；sportmonks 三项本机无 token 不可验证（如实标注，不凭文档猜）
- 2026-10-13 evolution-loop 首跑已验证（手动 dispatch 38365314945，success）：ACCEPT 2 候选走 audit/invariant 事实通道；**auto-fix 补丁 0 个 → 不推分支不开 PR（C1/C2 三道锁行为正确）**
- 2026-10-14 **死特征 rest_days 修复**（原 DEAD-FEATURES-INJURY-REST）：真因是 `EloUpdater.update()` 从不写 rest_days，1359 队恒为默认值 3 → `dixon_coles.py:131/143` 的 `(home.rest_days-away.rest_days)*rest_weight` 恒为 0，config 的 `rest_weight` 是从未生效的死旋钮。**不是**"enrichment 没进特征装配"——伤停数据一直通过 main.py:390-399 的 attack 惩罚生效。修法：`update(match_date=...)` 结算侧写 `last_match_date`/`rest_days`（封顶 14 天、只向后推进、乱序不回归）；预测侧新增 `refresh_rest_for(rating, kickoff_date)` 按本场开赛日折算且**不落盘**（否则一次预测就把"上一场"改写成还没踢的比赛）；存量 1359 队按 match_history.db 最后两场间隔回填 → 取值种数 1→12。`invariants.check_dead_features` / `liveness._probe_dead_features` 改为只判 rest_days——**injury 恒 0 是有意不接线**（"缺阵人数→单一 injury 标量"无证据，且与 attack 惩罚路径并存会双重计罚）；`known_issues` 条目改名 DEAD-FEATURES-REST-DAYS 并把候选修复从"两个权重都归零"改成"仅 rest_days 退回常量时才归零"
- 2026-10-13 全盘复核：① **待办清单陈旧**——4 条早已完成却仍挂在"已知未修"（issue #1/kelly 口径/开盘价/evolution 产物），会让下一个 agent 重做已完成的事，已清理并标注 ② 全仓库扫出 **7 个"写了却从未被调用"的方法**，其中 `DJYYSource.fetch_odds_snapshot` 有真 bug：`1/p` 把去水概率转回的"公平赔率"当成真实赔率存（实测偏高 3~4%=抽水量），与 sporttery 口径不一致 → 已改用 `markets.raw_odds` ③ `main.py` 2421 行（两个 1000+ 行函数）经核实**无重复去水逻辑**，属维护性风险而非正确性漂移
- 2026-10-13 **挑战任务首次真实提交成功**：真 token 下 5 注全部落库（服务端配额 usedCount 0→14，浏览器直查后端确认）。过程中发现**成功判定写反**——`GMI_SUBMIT` 成功时不返回 `code` 字段（只回 `msg="suc"`），原判定 `code===0` 把成功记成失败、破坏幂等（`scripts/njstats_challenge/submit.js::isSubmitOk`）
- 2026-10-13 token 有效期实测 **20 小时**（JWT iat→exp=72000s）→ 推翻"搬进 CI"方案：CI 无法无人值守刷新，任务必须留在 7×24 的 Air（`scripts/njstats_challenge/README.md`）
- 2026-10-13 **别名匹配打通价值兑付**：挑战任务"命中 0/5"根因是**队名译写差异**（非联赛不重叠）——竞彩用简称（比利亚雷/巴萨/里斯本/莱红牛/马竞/加拉塔萨），池子用全称。改用手写归一化 → 复用 `engine/team_aliases.py` 三级匹配（别名表/loose/内置兜底）+ 补齐欧冠主流简称 → **命中 5/5，全部走竞彩口径**（`scripts/njstats_challenge/submit.js`）
- 2026-10-13 归档安全网：`git reset --hard` 清掉归档（第二次发生，靠备份救回 124 文件）→ 加 `njstats_safeguard.py`（只增不删/拒写仓库内/每轮自动跑）+ 归档进度每轮自愈对账（`scripts/njstats_safeguard.py`）
- 2026-10-13 归档跨机器故障：代理硬编码 7897 + key 文件名跨脚本不一致（`njstats_key` vs `njstats_apikey.txt`）→ 两处都**静默零产物**（不报错）→ 改为探测/多文件名兜底（`scripts/njstats_archive.py`）
- 2026-10-13 挑战 guessingId 映射打通：用真实 apikey 调 GMI_MATCH_LIST(listType=OPEN) 拿 222 场，按 schema 逐层解出 guessingId+中文队名+开赛时间+三盘口赔率；weekId 编码实测=YYYY+ISO周号（2026-10-13→202642）；配额实测 maxCount=28/本周已用0。**同时发现池内 8 场欧冠才有竞彩盘口（jcSpf/jcRq 带让球线），我们 10 场匹配 0 命中（联赛不重叠）**——不是没注可提，是没预测欧冠 → `matchPool()` 加盘口口径择优（有 jc 用 JC_SPF/JC_RQ，否则欧赔）（`scripts/njstats_challenge/submit.js`）
- 2026-10-13 预测挑战协议实证：v3 端点是推断的 → 逆向前端 bundle 挖出真实协议（`POST /njserver/openApi/` + `application/x-protobuf` + modId/msgId/Bearer），实测「合法 protobuf + 无 token → 200 + 请先登录」证明链路正确 → 改用手工 protobuf 编解码 + `--probe` 子命令；5 条新测试锁 wire format/有符号 code/枚举/pick 口径（`scripts/njstats_challenge/submit.js`）
- 2026-10-13 Air 收口：预测挑战提交器（`_vault/njstats_challenge_submit.js`）从未入仓库，只在 Air 本地磁盘，Air 一出问题这个唯一无法上云的任务就断了且仓库无记录 → 根因：`_vault/` 被 gitignore，关键任务成了"黑工" → 修法：按 STATUS.md 记录的实测规则重建为 `scripts/njstats_challenge/submit.js` + README，补 7 条回归测试锁住周界/择优/幂等/让球符号
- 2026-10-13 `weekKey()` 时区坑：用 `getDay()` 算周界，北京周一 07:00 = UTC 周日 23:00 → 被算成上一个周一，周一早上的提交记到上周配额（部分重复计算、部分凭空消失）→ 修法：shift 到 +08:00 再取 UTC 字段（submit.js）
- 2026-10-13 `submit.js` 的 ROOT 基准：`path.resolve(__dirname,'..','..')` 在源码被载入的上下文里 `__dirname` 失效，ROOT 静默跑偏 → loadCandidates 报「无预测文件」而真实原因是路径基准丢了，错误信息毫无指向性 → 修法：优先用 `process.argv[1]` 推导，并加 `test_root_points_at_repo_root` 锁住（已验证能抓住该 bug）
- 2026-10-13 PR 乱局收口：6 个 open PR 中 3 个是重复/悬空重建版（分支落后 main 68 笔后 CONFLICTING，只能开新分支重放）→ 根因是 `data/`+`web/` 有每小时自动提交，分支不及时 rebase 就必然悬空 → 修法：三有效 PR 按 #29→#28→#27 合入（merge-tree 预验证互不冲突），4 个悬空 PR 关闭+注释去向，8 个遗留分支删除，开 PR 前必先 rebase 写进 AGENTS.md
- 2026-10-13 `evolution/` 16 处 ruff F 级告警 CI 完全不可见 → 根因：#28 把 `evolution/**` 挂上触发路径但 lint 步骤没跟着扩（"跑了"不等于"查了"）→ 修法：test.yml ruff 范围加 `evolution`，含 F601 重复 dict 键（`known_issues.py` 的 detect_by/invariant_id/candidate 被静默覆盖）、F821 缺 `import random`
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
