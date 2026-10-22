# PROJECT.md — football-engine（竞彩足球概率引擎）

> [!IMPORTANT] 交接卡（收工必更新；冷启动只读这块）
> 状态: doing　更新: 2026-10-22 15:00（DJYY 收尾 13/13；E10 低赔档开闸）
> 在干什么: ① **E8 换闸门量纲**：`edge` 档实测**不可达**（7 场对照 P−market_fair 全负，
>           最大 −0.23pp；10-19 起连续 131 轮零合格），改成
>           `ev_site = P×站点结算价 − 1 ≥ 2%`（上界 25% 判脏价），开关在
>           `data/state/challenge_policy.json` 的 gate_mode，改回 `"edge"` 即停实验。
>           **赌的是盘口软（站点价比体彩价中位高 11.6%），不是模型变强**；
>           判定标准已写死进 docs/EXPERIMENTS.md E8 行，不许边跑边改。
>           E8 已真实进账：W43 至 12:35 已投 5/28（含 12:35 格拉茨风暴 vs 马赛
>           ev_site +2.64%）。**E10（用户口头称 E9，ID 已被侧挂实验占用）同日开闸**：
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
>           只是 Worker 响应被边缘缓存 60s 的假象。修复：duty 用本机住宅网络直连抓取，
>           超过 10h 即刷新并提交快照，CI 只消费仓库快照。10-22 实测本机直连 200 场、
>           200/200 带 Pinnacle 赔率与模型概率；预测侧 fusion 在实时源缺失时用快照
>           `djyy_model_prob` 补位，第三通道不再被重新归一化掉。E8 在恢复前 n=0，
>           无样本污染；恢复会改变 P→ev_site，后续按 E8 护栏③记 edge/ev_site。
>           **DJYY 已收尾**：① 补「萨迪纳摩→萨格勒布迪纳摩」别名（只加 sources 层，
>           不合并 team_ratings 键），当日预测富化 13/13；② 快照 `extracted_at` 暴露、
>           日志打快照龄并过期告警，补位事实写进 djyy_status；③ health-check 只在
>           「实时被拦且无补位」时报 error，selfcheck 存活率改看 djyy_model_prob
>           （实时或快照），不再在通道健康时误报；④ comparison 接口已无 xG
>           （快照 0/200），xG 字段恒空但不阻塞概率通道。
>           已知覆盖缺口：弗拉门戈 vs 拉普大学 不在 DJYY 快照池内（非别名可修）。
>           **不需要再配 DJYY_KEY 或 Worker 白名单。**
>           ⑤ 删 `harness/`（DeepSeek 值守 agent）——`deepseek_key` 不存在，每轮都走降级分支；
>           并发锁搬进 `njstats_duty.py`（固定路径 `~/.football-engine/duty.lock`），
>           launchd 改为直接跑脚本，Qoder 自动化接手「每日 4 次巡检」+「E8 每日对账」。
> 新坑: ① 本机**没有 gh CLI**，AGENTS.md 里那些 `gh ...` 全跑不了（已改写交付节）。
>       ② `odds_coverage.not_started_yet` 原先语义与名字相反（它数的是「末快照早于开赛」），
>          这次复核就是被这个字段名骗过 —— 已拆成 assessable / pending_kickoff /
>          stopped_before_kickoff / post_kickoff_last。
>       ③ 站点板 333 场 ∩ 我方预测 13 场 = 可评 10 场：**闸门放开后瓶颈变成覆盖率**；
>          10-22 那轮新口径只放 1 场。④ `rank_diff` 恒 0（djyy.py 解出了 _rank_home/_rank_away
>          但 main.py 一个没传）—— 已接，但 lgbm 仍 ready:false，今天不改变任何输出。
> 下一步: ① 修复已全部合入 main（#37–#44、#50 DJYY 别名、#51 DJYY 观测、E10 PR）：
>           挑战池匹配 11/11、零合格日志带归因、DJYY 富化 13/13。
>         ② E8/E10 攒注：E8 至 12:35 已 5/28（首笔 ev_site +2.64%）；E10 目标 n≥60
>           或 2026-11-22 收尾。**每日对账必须按 `exp` 拆池：无 exp 视为 E8，
>           `exp="E10"` 只进 E10 裁决**，否则两边抽样混在一起。每小时确定性保留在
>           launchd（本机合盖会漏轮，10-21 实测 20:00/21:00 零日志）。
>         ③ 周体检（周一 03:30）已修为读-合并-写 policy，不再洗掉 E8/E10 开关。
>         ③ 独立信息源剩余项（DJYY 已不再阻塞）：context 层是 store-only（72/1163 条带 context，
>           `FusionInput` 无字段）、雷速/Qwen 新闻（`leisu_intel` 10-11→10-20 每天 0 命中
>           且 football-engine 无人读，还依赖本机 Chrome 上不了 CI）。
>           **SportMonks 是付费墙不是代码**：token 本机可读、CI 已挂，但 90 个
>           `data/state/sportmonks/*_details.json` 全空 `{}`，odds/xg/predictions 未订阅。
>           **NJStats 侧挂特征也已断供**：njstats_features 试用 10-15 到期，10-14 后
>           无快照（10-21 当天 model_eligible=0）→ E9 已在 EXPERIMENTS.md 标「阻塞」，
>           解阻只能恢复采集或换源，不降覆盖门槛。
>         ④ 待用户裁决：让球注 records schema（play/line）；`mult()` 赔率档倍数与站点
>           33 注实盘得分对不上（而排序正在用它）；账本 `djyy_prob` 的 null 是**永久的**
>           （append 去重、backfill 路径实测 0 行生效）要不要回填 = 改考卷；
>           社区倾向**不能**接进模型层（docs/CHALLENGE_SUBSYSTEM.md:18 引擎永不读挑战产物，
>           tests/test_challenge_firewall.py 执法）。
>           E8 截止风险：按当前覆盖（每天约 10 场可评、E8 12:35 已 5 注），
>           11-22 前未必凑满 60 注；冻结判据写的是「否则回退 edge」，而 edge 已证
>           不可达。到期 n<60 时按无结论处理/延长/换口径，需在截止**前**裁决，
>           不能临时改判据。
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
> 下一步: ① **rho 自进化在 CI 上一直失效（待定，属模型参数禁触区）**：engine/main.py:2320-2327
>             每轮把 rho 写回 config/prediction.json，而 daily-forecast.yml:193 只
>             `git add data/ web/`（不含 config/）→ 写进工作区无人提交，被
>             `git pull --rebase --autostash`(:220) 卷成 stash 残渣（已清掉 2 个）。
>             现状 rho=-0.0748（10-12 起未变），实测当前拟合为 -0.0705（n=1111）。两条修法待选：
>             让 daily-forecast 提交 config/，或确认 rho 只该在 backtest-weekly 那周更
>             （但上一轮 6d0a22d1f 跑完 rho 仍是 -0.0748，说明那条路也没真写）。
>          ② duty 与 launchd 并发无锁（scripts/njstats_duty.py 与 harness/run_duty.sh 均无 flock）：
>             同一分钟手动跑 + launchd 跑会并发 `git pull --rebase` 与写 njstats_jwt。修法待定。
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

## 已知未修（按优先级）
> 2026-10-13 复核：前 4 条**已解决**，移到底部「已完成」；下面是真正还开着的。

- [ ] **`main.py` god object**：`run_daily_pipeline` 1225 行 + `run_settlement` 955 行。
      风险：任何改动都要在 2000+ 行里定位，且两处逻辑容易各写一份（同口径漂移的高发区）
- [ ] SportMonks Growth 试用激活（用户暂缓）→ 验 odds 模块是否随套餐
- [ ] git 历史端点路径清洗决策（HEAD 已清，历史待用户裁决是否 force-push 重写）
- [ ] AUT 平局纸面证据累计（双源佐证 +7.5%/+9.24%，t≈1.7 候选，live 证据持续累计）
- [ ] 洁净样本累积 / LGBM ready / Qwen 新闻源（用户决定）
- [ ] SportMonks `fixtures_on` / `fixture_odds` / `injuries` 三个方法未接入。
      **本机无 token 无法验证**（SPORTMONKS_TOKEN 只在 GitHub Secret 里，
      试用激活用户已暂缓）。要动它们需先在 `~/.football-engine/sportmonks_token`
      配好 token 再实测——**不要凭文档猜**，这三个的 odds 模块"需订阅"
- [ ] 全仓库 98 处 `except → return None/pass` 静默吞异常。**这是本项目的头号病害**——
      今晚发现的三处静默零产物（代理硬编码、key 文件名、`same_odds` 列名不匹配）
      都是同一个病：不报错，只是没产出。**不要批量改**（会淹没真正需要静默的地方）；
      建议按"关键路径"逐个加可区分的错误记录，参考 `engine/sources/djyy.py` 的
      `_record_error()` 模式

## 今晚已解决（原在待办清单里，复核后移出）
- [x] issue #1 无盘口标注（PR #35：`_pred_pick` 加「⚠ 无盘口·不可投注」；页面已验证 1 处）
- [x] `kelly.py` 口径（量化裁决：**不改**，依据 docs/KELLY_CALIBER_DECISION_20261013.md）
- [x] 开盘价缺口（DJYY comparison 的 8 个市场带 Pinnacle/bet365 开盘价，**零额外请求**）
- [x] `evolution/` 产物（已 gitignore）

## 已修（现象 → 根因 → 修法）
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
