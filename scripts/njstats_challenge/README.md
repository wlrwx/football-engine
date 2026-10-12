# NJStats 预测挑战提交器（Mac Air 专属任务）

> 这是整个项目里**唯一不能上云**的任务，其余全部在 GitHub Actions。
> 本目录是 2026-10-13 从 Air 本地 `_vault/` 重建进仓库的版本（原文件从未入 git）。

## 为什么它必须在 Air 上

预测挑战要**浏览器登录态**（Bearer token），GitHub Actions 的 runner 是一次性容器，
没有登录态，也无法人工过验证码。所以这个任务只能留在 7×24 挂机的 Mac Air 上。

## 已验证的真实协议（2026-10-13 逆向 + 实测）

从 NJStats 前端 bundle（`/app/index-*.js` + `mine-*.js` + `proto-*.js`）逆向出协议，
并用 curl 实测通过：

| 项 | 值 |
|---|---|
| 端点 | `POST https://www.njstats.cn/njserver/openApi/` |
| Content-Type | `application/x-protobuf` |
| 自定义头 | `modId: 1012`（MI_GUESSING）、`msgId: 2`（GMI_SUBMIT） |
| 鉴权 | `Authorization: Bearer <token>` |
| 请求体 | protobuf：外层 `{params(1,bytes), page(2,msg)}`，params = `ReqGuessingSubmit{guessingId(1,str), playType(2,enum), pick(3,int32)}` |
| 响应体 | protobuf：`{code(1,varint), msg(2,string)}`；`code=0` 成功，`code=-1` = 未登录 |

**实测结果**：合法 protobuf + 无 token → `HTTP 200 code=-1 msg="请先登录"`。
协议链路完全正确，只差登录态。`node submit.js --probe` 可随时复验（不需 token）。

### GuessingPlayType 枚举（逆向自前端）

| 值 | 名称 | 用途 |
|---|---|---|
| 0 | GPT_NONE | — |
| 1 | GPT_SPF | 胜平负（欧赔） |
| 2 | GPT_RQ | 让球（欧赔） |
| 3 | GPT_DX | 大小球（**竞彩不用**，与 STATUS.md「大小球只观察不提交」一致） |
| 4 | GPT_JC_SPF | **竞彩胜平负** ← 用 |
| 5 | GPT_JC_RQ | **竞彩让球** ← 用 |

`pick` 是 **1-based**（home=1 / draw=2 / away=3），逆向自前端 `Yn()` 的 `pick = index + 1`。

> ⚠️ 前端 bundle 的 hash 会随发版变（`index-DIYsyTJY.js`）。协议变动时按上面的路径重新逆向：
> 搜 `ReqGuessingSubmit` / `MI_GUESSING` / `resolveProtoBaseURL`。

## 每日流程（11:30）

```
1. daily-forecast（Actions，每小时 :15/:45）产出 data/daily/<date>/predictions.json
2. Air 11:30 跑:  node scripts/njstats_challenge/submit.js --date <date> --submit
3. 提交结果写 ~/.football-engine/challenge_state.json（周配额 + 已提场次）
```

**Air 上只需要这一个定时任务。** 其他本地 hourly cron（auto_run.sh / run_realtime_monitor.sh）
必须全部停掉——它们与 ci-sync.yml 每小时同时写 `data/state/`，互相覆盖。

## 提交规则（2026-10-12 实测，改规则必须改这里 + STATUS.md 两处）

| 规则 | 值 | 出处 |
|---|---|---|
| 赔率带 | odds ∈ [1.60, 2.31] | STATUS.md「EV 策略」 |
| 每场上限 | 一票（每场仅一市场，提过即消失） | STATUS.md 实测规则 1 |
| 每日上限 | 5 场 | STATUS.md |
| 每周配额 | 28 | STATUS.md + 前端文案「每周28次预测机会」 |
| 可选市场 | 竞彩胜平负 + 竞彩让球（大小球只观察不提交） | STATUS.md 自动化 v2 |
| 低赔带豁免 | **只适用胜平负**，让球没有 | STATUS.md 实测规则 2 |
| 让球记录口径 | 水位口径（主队-1@0.95） | STATUS.md 实测规则 3 |

## 让球符号校验（为什么不能删）

`validateHandicapSign()` 检查 `line` 是主队让球数：负=主队让，正=主队被让，
范围 ±5。**符号反了 NJStats 不报错，只会静默记成相反的一场**，到结算才发现全错。
这是原 v2 提交器专门加的一道校验，v3.1 保留。

## 登录态怎么来（唯一需要人工的步骤）

```bash
# 在 Air 上执行一次：
# 1. 浏览器打开 https://www.njstats.cn 并登录
# 2. DevTools → Network → 任一条 /njserver/openApi/ 请求 → Request Headers
# 3. 复制 Authorization: Bearer 后面的 token，写到 ~/.football-engine/njstats_token.txt（一行）
```

token 过期后重新导出即可。**不要**把 token 提交进仓库（`~/.football-engine/` 在仓库外）。

## 状态文件（都在仓库外，不随 git 走）

| 文件 | 内容 |
|---|---|
| `~/.football-engine/challenge_state.json` | 周配额计数 + 已提 match_id（幂等：提过即跳过） |
| `~/.football-engine/njstats_token.txt` | Bearer token |
| `~/.football-engine/njstats_value_model.json` | 战绩累积（STATUS.md 记的 1,667 注） |

**Air 重装后**：本目录 + README 就是全部；token 需重新人工导出一次。

## 已知未修

- [ ] **`guessingId` 映射未实现**（最重要）：真实协议的 key 是 NJStats 侧的 `guessingId`，
      **不是**我们的 `match_id`。当前 `loadCandidates()` 用 `match_id` 占位，
      直接 `--submit` 会被服务端拒绝或提交到错误的场次。
      正确做法：先调 `GMI_MATCH_LIST`（modId=1012, msgId=1, listType）拿 guessingId，
      再按队名+日期 join 到我们的 match_id。**这个映射做完之前，只跑 dry-run。**
- [ ] `--probe` 通过 ≠ 能提交：probe 只验协议，不验 token 有效性和 guessingId 映射
- [ ] 低赔带豁免目前没实现（统一按 [1.60,2.31] 过滤）。STATUS.md 说豁免只适用胜平负，
      若要让低赔胜平负也能提，需要在 pickBest 里给 1x2 单独放宽下界——但那会改变提交行为，
      **需人拍板后再改**
- [ ] 让球的「真实给分」机制尚未校准（STATUS.md：待 10-13 结算校准）——在此之前
      让球票的 EV 排序可能高估其价值
- [ ] 前端 bundle hash 随发版变化，协议若变需重新逆向（搜 `ReqGuessingSubmit`）

