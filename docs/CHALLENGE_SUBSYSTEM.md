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
| scripts/challenge_review.py | **只读** ledger + data/state/challenge_state.json，零 import |
| _vault/recon_njstats.md 等侦察笔记 | 仓库外/不追踪 |
| data/state/challenge_review.json | 产出报告，无下游引擎逻辑消费 |
| 引擎 → 挑战 数据流 | 单向：读 predictions/ledger。**引擎永不读裁判产物**（见下方「侧挂数据」） |
| 周一深度体检 | 仅"简报引用+可选自否决条款"，删除该步不影响其它步 |

## 反馈合同（三层，方向锁死）
```
模型层   ← 全量账本 + CLV + 融合权重自进化（40× 样本、零选择偏差）
             ⚠ 永不读取：challenge_state / challenge_review / challenge_policy
策略层   ← challenge_policy.json（挑战实战经验唯一合法回流口：门槛/线/市场偏好）
裁判层   = 挑战预登记池（独立于模型训练数据，才有资格当镜子）
```
理由：用被审数据改评分标准=自己批卷（DJYY 门户实证：回测+12.6% 实盘-8.7%）。
裁判独立是这个子系统最贵的资产；挑战死了可以再找，裁判一污染就全废。
tests/test_challenge_firewall.py 在 CI 强制此边界（engine/ 出现 challenge 产物引用即红）。

## 侧挂数据（允许进模型；边界是「裁判产物」，不是「数据来源」）

2026-10-22 澄清：NJStats 的旁路数据是**侧挂**，不是**热插拔**。
侧挂 = 常驻旁路，有就读、缺了就降级，引擎不改一行也能跑；
热插拔 = 可随时替换数据源/实现而不改变输出——本项目**不做**这个承诺，
所以侧挂入口收敛成 `engine/sources/njstats_side.py` 一个文件。

| 类别 | 例子 | 能否进模型 | 约束 |
|---|---|---|---|
| 赛前公开输入 | NJStats 开盘赔率、主客分离统计 | ✅ 允许 | 只经 `njstats_side.py`；快照须早于开赛，训练侧还须早于当时预测时刻 `as_of` |
| 目标市场价 | 站点赔率 `site_odds/` | ❌ 不进特征 | 按 `engine/prediction/market_isolation.py` 只能留在策略层（E8 闸门 / EV） |
| 社区票 | `people` / `votes` | ⏸ 暂不能 | 快照里 `code` 是市场内序号且无市场标识，无法映射 1x2；`dom_tendency` 是方向真值，禁止读 |
| 裁判产物 | `challenge_*` 三件套、结算结果 | ❌ 禁止 | 自己批卷 |

禁令的对象是**判决**，不是数据来源。把「不许读裁判产物」扩读成「不许读
NJStats 的任何数据」，是把侧挂误当热插拔。实测覆盖率见
`python -m engine.sources.njstats_side`。

## 变更纪律
本子系统的一切代码/流程变更走**独立 PR**（本文件即示例：feature/challenge-subsystem），
不夹带进核心改动；PR 描述须含"拔除影响评估"一节。

## 退役程序（挑战下线时照此执行，约 10 分钟，零风险）
1. 删除/停用两条自动化：「NJStats 挑战提交 每小时:35」；周一深度体检去掉第 6 步与简报③④句（CronUpdate）
2. scripts/njstats_api_submit.py、challenge_review.py **保留不动**（已自然空转：无 state/无列表即退出）
3. 历史复盘价值归档：challenge_review.json + data/state/challenge_state.json 存档为静态数据；本文件末尾追加"退役记录：日期/存活周数/池内注数/最终校准结论"
4. 复用资产说明：openApi protobuf 逆向方法（_vault/recon_njstats.md）是通用手艺，与站点无关，保留
5. 核心引擎回归自检：跑一次完整周检（tracker/EV/CLV 各步照常），确认无红

## 运营状态
- 2026-10-12 上线（UI 模拟）→ 10-15 纯 API + 预登记纪律 v5.2
- W42 特例（本周）：填仓模式——门槛 edge>0、K=剩余配额/剩余天数，目标周日 28/28
- W43 起（常规）：周一~四 ≥1.5pp / 周五~日 ≥0.8pp，K=max(2,⌈剩余/天⌉)，上限 6
- **2026-10-22：闸门量纲切换到 `ev_site`（E8 前瞻实验，见 docs/EXPERIMENTS.md）**。
  原 edge 档从 10-19 起连续 131 轮零合格——不是"1.5pp 太少"，而是
  `P − market_fair` 在实测模型下**恒为负**（7 场对照最大 −0.23pp，与 E3 一致）。
  新口径 `ev_site = P×站点结算价 − 1 ≥ 2%`（上界 25% 判脏价），赌的是盘口软而非模型聪明。
  开关在 `data/state/challenge_policy.json` 的 `gate_mode`/`ev_floor`，改回 `"edge"` 即停实验。
  实测覆盖率才是下一层天花板：站点板 333 场 ∩ 我方预测 13 场 = 可评 10 场，
  10-22 这天只有 1 场过新闸门；要放量得先扩赛程/赔率覆盖（见任务：独立信息源）。
- **2026-10-22：低赔候选另立 E10**（`min_sporttery_odds=0.0`，登记见 EXPERIMENTS E10）。
  旧筛「体彩价≥1.60」只对 E8 之前的 edge 口径有意义，ev_site 下会漏掉正 EV 的低价热门。
  E10 候选单独成池、排在 E8 之后吃剩余名额；E8 候选/排序/上限不变，
  记录带 `exp="E10"`。默认 1.60 = 不启用，改回一行即停。

## JWT 续期机制（2026-10-16 逆向定稿）
- **正道（免验证码）**：浏览器登录 session 调 `GET /api/user/current`（credentials:include）→ `data.token` 返回全新 20h JWT。Mac :35 值守每次 mint 后写文件 + 推 GitHub secret `NJSTATS_JWT`。只要登录时勾了"自动登录"（Spring SESSION cookie 长期有效），即可无限续命，无需再碰验证码。
- **兜底（命令行重登）**：`scripts/njstats_auto_login.py` 已逆向完整登录链——`GET /api/public_key` → `GET /api/captcha`（三层 AES-CBC，密钥硬编码前端）→ `POST /api/login`（RSA-PKCS1 密码 + captcha + captchaIndex）。但验证码含算术型（"2×6=?"→答 12）与叠字干扰型，ddddocr 识别 + 一次性 TTL 使其不可靠，仅留作协议存档；真到浏览器 session 也死时优先人工登录一次再 mint。
- 登录 POST 必须 **form-urlencoded**（JSON 会走另一错误分支永败）；连错约 4 次触发限流（响应变空），勿连打。
- 站方 Spring session 约 **60 分钟空闲即死**——值守 :35 单频恰好踩边界，需 :05/:35 双频或人工登录时先刷一下页面。
- 凭据 `~/.football-engine/njstats_account`（0600，仓库外）。
