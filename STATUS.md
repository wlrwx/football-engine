# 足球引擎 · 状态文档（新对话开场先读这个）

> 最后更新: 2026-10-11 | 项目: 《王者万象棋》无关，这是竞彩足球预测引擎

## 一句话现状

竞彩足球概率预测 + 虚拟投注系统，GitHub Actions 全自动运行。
**vs 自身旧版 IMPROVED**（配对 ΔBrier 0.062, t=3.3, n=558，2026-10-11 修正口径后重算）；
**vs 去水市场 TIED**（Δ=+0.0045, t=1.23，无证据 edge）——有效融合权重 market 93.75%，
DJYY 第三通道曾静默死亡 6 周（10-11 已修复回填+报警），待真实样本重新定价。

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
  git 卡死清场/积压补推/refspec 修复、补抓赛果、补结算、DJYY 存活率、CI 停摆、
  概率分布先验带、权重归一、cron 存在性、代理、仓库体积。报告: data/state/system_health.json。
  设计: 幂等自愈 + critical 返回 2（供 CI 门禁）+ 每条检查对应一次真实事故
- **推送走 SSH deploy key（2026-10-11 起）**：远端 = `git@github-football-engine:...`，
  密钥 `~/.ssh/football-engine_deploy`（只授权本仓库，经 ssh.github.com:443 + Clash 代理）。
  cron 后台会话拿不到 keychain 凭据，别把推送改回 HTTPS+osxkeychain

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

## 下一步/开放事项（2026-10-11 深挖后重排）

- [ ] DJYY 通道复活验证：10-11 起结算行应出现 djyy_prob（含回填），优化器用真实样本重定价
- [ ] CLV 评估体系：#6/#9 的 7 万场收盘价就位，用收盘线价值替代 ROI 代理做技能度量
      （edge 分桶当前全负：edge>0.10 桶 ROI -19.8%，EV 口径需在收盘价基础上重估）
- [ ] 环境/决策权对齐：本地 hermes venv 缺 sklearn/lightgbm/pandas——校准启用决策
      曾在无拟合器环境做出；要么本地补依赖，要么进化决策权只归 CI
- [ ] 结算归属本地化：CI schedule 延迟 2~4h 是账本滞后根因，本地 :25 加幂等 settle
- [ ] PR #4（进化闭环）rebase 到最新 main 再合（直接合并会回退 #6~#9 的 5500 行测试）
- [ ] 分支清理：7 个远端空壳分支 + 4 个本地分支可删（内容零残留，已实测）
- [ ] 仓库瘦身：.git pack 186MB（30分钟提交数据+odds_series 873文件），考虑 LFS/瘦身
      （doctor 已加体积告警阈值 400MB）
- [ ] 洁净样本继续累积，跟踪 upgrade_tracker 判定
- [ ] LGBM 每次结算自动重训（训练在 CI 侧），盯 ready=true（需双基线显著）
- [ ] 可选：Qwen 新闻→情境层管道（等用户定新闻源）
- [x] 用户侧遗留→已解锁：本地推送已不依赖旧 PAT（deploy key 接管），keychain 里的
      GitHub token 现在可以随时撤销重发，不影响自动化
