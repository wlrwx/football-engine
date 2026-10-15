# 挑战子系统（NJStats 预测挑战）— 边界与退役手册

> 本子系统是**寄生在外部设施上的临时校准器**。设施方（NJStats）随时可能下线挑战。
> 设计原则：核心引擎对它的依赖必须为零，拔除动作必须只删不改。

## 它是什么
每小时(:35)从引擎预测里取"经过计算的"合格场次，预登记提交到 NJStats 预测挑战
（每场限一市场、周配额 28）；周日结算后由 challenge_review.py 把"声明 vs 兑现"
做成校准数据，进周一深度体检简报。**价值=免费的实盘预登记镜**，不是榜。

## 依赖边界（耦合审计结论）
| 资产 | 与核心引擎的关系 |
|---|---|
| scripts/njstats_api_submit.py | **只读** predictions.json + 私有协议，零 import engine.* |
| scripts/challenge_review.py | **只读** ledger + _vault/challenge_state.json，零 import |
| _vault/challenge_state.json / recon_njstats.md | 仓库外/不追踪 |
| data/state/challenge_review.json | 产出报告，无下游引擎逻辑消费 |
| 引擎 → 挑战 数据流 | 单向：读 predictions/ledger。**引擎永不读挑战产物**（原则） |
| 周一深度体检 | 仅"简报引用+可选自否决条款"，删除该步不影响其它步 |

## 变更纪律
本子系统的一切代码/流程变更走**独立 PR**（本文件即示例：feature/challenge-subsystem），
不夹带进核心改动；PR 描述须含"拔除影响评估"一节。

## 退役程序（挑战下线时照此执行，约 10 分钟，零风险）
1. 删除/停用两条自动化：「NJStats 挑战提交 每小时:35」；周一深度体检去掉第 6 步与简报③④句（CronUpdate）
2. scripts/njstats_api_submit.py、challenge_review.py **保留不动**（已自然空转：无 state/无列表即退出）
3. 历史复盘价值归档：challenge_review.json + _vault/challenge_state.json 存档为静态数据；本文件末尾追加"退役记录：日期/存活周数/池内注数/最终校准结论"
4. 复用资产说明：openApi protobuf 逆向方法（_vault/recon_njstats.md）是通用手艺，与站点无关，保留
5. 核心引擎回归自检：跑一次完整周检（tracker/EV/CLV 各步照常），确认无红

## 运营状态
- 2026-10-12 上线（UI 模拟）→ 10-15 纯 API + 预登记纪律 v5.2
- W42 特例（本周）：填仓模式——门槛 edge>0、K=剩余配额/剩余天数，目标周日 28/28
- W43 起（常规）：周一~四 ≥1.5pp / 周五~日 ≥0.8pp，K=max(2,⌈剩余/天⌉)，上限 6
