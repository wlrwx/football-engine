# Sporttery Engine

竞彩足球概率分析、虚拟投注模拟与可审计复盘系统。基于 Dixon-Coles 泊松模型、蒙特卡洛模拟和市场概率融合，每日自动运行在 GitHub Actions，报告发布到 GitHub Pages。

**[打开在线报告](https://wlrwx.github.io/football-engine/)** · 仅用于数据分析与算法学习，不构成任何投注建议；所有投注记录均为虚拟记账。

## 当前能力

- **三源概率展示**：分别展示原始模型、去水市场和最终融合的方向与概率；实际有效权重按每场数据源可用情况计算并展示，避免把名义权重误当成真实权重。
- **Sporttery Pro Workbench**：紧凑比赛看板支持单关筛选、EV/时间排序、体彩胜平负与让球双市场、官方波胆/总进球/半全场赔率，以及本地串关计算器。
- **体彩单关标识**：抓取并传递 HAD/HHAD 的 `bettingSingle`，页面和方案明确区分单关与串关。
- **每日决策与复盘**：价值候选、资金敞口、禁投联赛、风险提醒、比分命中率、盘口信号和联赛分层统一展示。
- **逐源诊断**：每日结算分别统计原始模型、市场、最终融合的方向命中率，并记录市场一致率、平均有效权重和独立 Bernoulli 假设下的泊松二项尾概率。
- **实验护栏**：预测、选注、EV、阈值和冻结实验判据独立管理；不为制造投注而降低门槛，失败实验保留记录。
- **数据可追溯**：预测、赔率快照、结算账本和报告随 git 版本化，决策链支持审计与回放。

## 如何阅读报告

| 名称 | 含义 |
|---|---|
| 原始模型 | 不依赖目标比赛赔率的独立统计判断 |
| 市场 | 体彩/参考赔率去水后的价格观点 |
| 最终 | 原始模型、市场及可用辅助源按实际权重融合后的倾向 |
| 实际权重 | 某一数据源缺失时，其余有效权重重归一化后的真实占比 |
| EV | 模型概率与可成交赔率计算出的期望值；正值不等于无风险收益 |
| Shadow | 虚拟记账与风险模拟，不代表真实下单或真实资金变动 |

首页的“最终跟随市场 / 平均市场权重”用于回答一个关键问题：**最终判断究竟来自模型，还是主要跟随市场价格。**

## 核心理念

1. **市场是基准，模型必须证明增量**：是否启用校准、LGBM 或融合后处理，由已结算账本的时间切分与配对回放裁决，不凭感觉加开关。
2. **生产与优化目标对齐**：当前生产使用 `linear` 概率融合，权重优化使用 `softmax_brier`；`logit_nll` 只允许与 `logit` 融合配套，错误组合会直接拒绝。
3. **所有自动进化都有护栏**：校准层和 LGBM 的启用必须满足样本量、时间切分与显著性要求，退化时自动停用。
4. **决策必须可解释**：不仅记录最终概率，也记录来源、实际权重、赔率口径、拦截原因和赛果，支持逐场反推。
5. **实验判据先冻结**：实验假设、主指标、样本量和判定日先登记，运行中不修改成功标准。

## 系统架构

```text
数据采集
  竞彩官方赛程/赔率/单关标识 + 新浪赛果与赔率 + DJYY 辅助快照
        ↓
概率预测
  Dixon-Coles + Monte Carlo → 原始模型
  去水赔率 → 市场概率
  可用辅助源 → 三源融合（linear，记录有效权重与 trace）
        ↓
决策与风控
  EV / 置信度 / 联赛禁投 / 资金与熔断 / 单关串关约束
        ↓
锁定、结算与复盘
  逐源命中诊断 → 市场一致率 → 有效权重 → 周度回测与参数优化
        ↓
静态报告
  每日简报 + Sporttery Pro Workbench + Track Record + GitHub Pages
```

## 技术栈

- Python 3.12 / NumPy / scikit-learn / LightGBM / joblib
- GitHub Actions：每日预测、结算、采集、自检与报告部署
- GitHub Pages：静态报告发布
- 本地 NJStats 值守：仅在需要站方登录态的场景运行
- 无数据库：CSV/JSON + git 版本化，账本 append-only

## 目录结构

```text
engine/
├── prediction/     # Dixon-Coles / Monte Carlo / fusion / LGBM
├── strategy/       # Kelly / 水位信号 / edge 封顶 / 三票制 / 风险约束
├── learning/       # 融合权重优化 / 校准自进化 / 影子训练 / Elo
├── backtest/       # walk-forward / horizon 评估 / 周度回测
├── review/         # 结算账本 / EV 报告 / 逐源诊断 / 串票结算
├── integrity/      # 决策包 SHA-256 链 / 方案锁定
└── sources/        # 体彩 / 新浪 / DJYY 等可插拔数据源
scripts/            # 数据采集 / 消融回放 / 效果追踪 / 完整性自检
config/             # 预测、融合、优化器与实验配置
web/                # 生成的静态报告
docs/               # 实验登记、升级说明与运行手册
tests/              # 模型、数据链、网站与安全回归测试
```

## 快速开始

使用仓库虚拟环境（Python 3.12）：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m engine.main --date 2026-11-28
python -m engine.main --settle --date 2026-11-27
python -m engine.selfcheck --date 2026-11-28 --fail-on-critical
python engine/build_site.py
```

本地开发门禁：

```bash
python -m pytest tests/ -q
python -m ruff check --select F engine scripts tests evolution
git diff --check
```

日常预测、结算和报告部署优先交给 GitHub Actions，避免本机与云端同时生成同一份数据产物。

## 自动化与运行

- `daily-forecast.yml`：定时预测、最近数日幂等结算、数据自检、报告构建与 Pages 部署。
- `ci-sync.yml`：赔率时序等增量采集与同步。
- `health-check.yml`：数据完整性和关键链路巡检。
- 本机 NJStats 值守与调度细节见 `docs/DUTY_RUNBOOK.md`；不要在没有站方授权和登录态的情况下运行相关任务。

## 近期演进

| 日期 | 事件 | 关键变化 |
|---|---|---|
| 2026-11-28 | Sporttery Pro Workbench | 单关筛选、双市场、官方全玩法赔率、焦点栏与串关计算器 |
| 2026-11-28 | 三源预测透明化 | 展示原始模型/市场/最终、实际权重和市场一致率 |
| 2026-11-28 | 优化目标对齐 | `linear` 生产融合配套 `softmax_brier`，拒绝目标错配 |
| 2026-08-29 | 融合链重构 | 消融回放决定后处理开关去留，Brier 0.617 → 0.588（回放） |
| 2026-08-14 | 市场主导修正 | 修复水位信号反向问题，Brier 0.69 → 0.63 |

## 免责声明

本项目仅用于数据分析、算法学习和系统开发验证，不构成任何投注建议。
所有输出均为虚拟记账条目（Shadow 模式），不承诺收益，也不代表真实交易结果。
