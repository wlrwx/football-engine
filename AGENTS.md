# AGENTS.md — football-engine (Sporttery Engine)

> 给任何进入本目录工作的 agent 的锚点。本项目 = 竞彩足球概率分析与虚拟投注模拟系统
> （Dixon-Coles + Monte Carlo + 市场主导融合），运行在 GitHub Actions 上。
> 本目录（`~/Downloads/football-engine`）是**唯一**工作副本，PR/实验/日常循环全部从这里走。

## 开工前三步（必做）

1. **读 `PROJECT.md` 顶部交接卡**（≤15 行）；要细节按关键词查档案对应节，不通读、不重扫代码。
2. **用仓库专属 venv**（`python3.12` + requirements 全量；lightgbm 需 `brew libomp`）：
   - 运行/测试/装包一律 `<repo>/.venv/bin/python`（或 `python3.12 -m pytest`）
   - 本机裸 `python3` 是 3.13，**不许**用来跑本项目（依赖矩阵按 3.12 锁）
3. **改完必须过门禁**（全绿才算可交付）：
   ```
   cd /Users/jason/Downloads/football-engine && python3.12 -m pytest tests/ -q && python3.12 -m ruff check --select F engine scripts tests evolution
   ```

## 硬规则（本项目语境）

- **评估层禁触**：`engine/backtest/`、账本（`data/state/review_ledger.jsonl`）、
  基线/校准状态文件、`engine/prediction/calibration.py` —— 改它们 = 改考卷，一律人工 review。
  自动补丁侧由 `evolution/harness/patch_engine.FORBIDDEN` + `.github/CODEOWNERS` 锁死。
- **融合链每一步都是 config 开关**（`config/prediction.json["fusion"]["post_fusion"]`），
  去留由 `scripts/ablation_replay.py` 对已结算账本做配对回放裁决，**不凭感觉加开关**。
- **实验先登记**：任何实验开工前在 `docs/EXPERIMENTS.md` 登记一行（假设/分支/主指标/判定日/样本量），
  判定标准写死后不许改；失败结论保留行、标注结果，不得删除。
- 账本 append-only；`chain=v2` = 洁净样本；污染样本（`engine/integrity/quarantine.py`）不得进校准/LGBM/回放。
- 数据产物（`data/`、`web/`）随 git 走但**不是**代码；PR 里带上它们只是分支落后 main 的噪音，
  合并前先 rebase 到最新 main。

## 交付（只走这一条路）

日常改动：本目录建分支 → 推 origin → 开 PR → 人工/闭环 review 后合入 main。
**本机不开定时任务、不用 cron/launchd 跑预测**（那正是历史上"两台机器各跑各的"乱源）；
日常预测/结算/采集已全部在 GitHub Actions（daily-forecast / ci-sync / health-check）。
推送走 **HTTPS + gh token**（account `wlrwx`，已配置）。

## 两台机器的分工（硬规则，2026-10-13 定）

| 机器 | 只负责 | 推送方式 |
|---|---|---|
| **主线 Mac**（本目录 `~/Downloads/football-engine`） | 所有代码改动、PR、实验、合并 | HTTPS + gh token |
| **Mac Air**（7×24 挂机） | **仅** NJStats 预测挑战每日 11:30 提交（需浏览器登录态，无法上云） | 它自己的 SSH deploy key |

- **Air 上除预测挑战外的本地 hourly cron 必须全部停掉。** 它与 ci-sync 每小时同时写
  `data/state/`，必有一次 non-fast-forward，两边互相覆盖且不可追溯。
- **不要互相照抄推送配置**：两台方式不同（HTTPS vs SSH），各自的 `git remote` 各自维护。
- 预测挑战提交器代码/状态机在 `scripts/njstats_challenge/`（原在 Air 本地 `_vault/`，未入仓库）；
  Air 重装后按该目录重建，不要凭记忆重写。

## 收工后（必做）

更新 `PROJECT.md` 顶部交接卡（状态/下一步/新坑），本轮有价值的结论写进对应节：
现象→根因→修法→`文件:行`。**卡片、档案与代码同一批 commit 提交。** 不更新卡 = 本轮没收工。
