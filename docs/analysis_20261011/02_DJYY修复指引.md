# DJYY 失效的根因与两侧修复指引

> 2026-10-11 独立排查
> 结论：**数据源本身完全健康，问题在客户端两处静默失效 + CI 侧网络可达性**

---

## 0. 结论速览

| 项 | 状态 |
|---|---|
| `djyylive.com` 站点 | ✅ 存活，返回 HTTP 200，正在服务当日赛程 |
| `/api/leagues/fixtures` | ✅ 返回 181 场，**181/181 全部带 `_djyy_id`** |
| `/api/match/{id}/comparison` | ✅ 正常返回 `model.p_home/p_draw/p_away`，三项和 = 1.0000 |
| 连续 12 次快速请求 | ✅ 全部 200，无限流 |
| 本地生产链路 `enrich_from_djyy` | ✅ 6/6 场全部拿到 `model_probs` |
| 实盘账本 v2 链 | ❌ **529 场中 djyy_prob 命中 0 场** |

**不是数据源死了，是取数逻辑断了。**

---

## 1. 为什么整条链路是静默失效的

三个环节叠在一起，任一环节坏了都不会报错：

```
main.py:165  try:  ... except Exception: → djyy_enrichment = {}      ① 整体吞掉
manager.py   except Exception: fetch_fixtures → return {}            ② fetch 失败静默
manager.py   if time.monotonic() > deadline: break                    ③ 预算截断静默
manager.py   except Exception: continue    （逐场）                  ④ 逐场失败静默
```

结果：fusion 的 `djyy_weight=0.2` 名义权重长期挂着，但实际贡献为 0；
`fusion.py:136-142` 在 djyy 缺席时把权重**重新归一化**到 model/market 上，
于是有效权重变成 model 6.25% / market 93.75%，而配置文件里写的是 0.05/0.75/0.20。

**账本里 `djyy_prob` 长期为 null，没有任何告警。**

---

## 2. 已定位的两个独立缺陷（代码侧，已修）

### 缺陷 A：时间预算固定 180s，且截断是静默的

`manager.py:20` 的原注释：

> `# 2026-10-10 60→180s: 按天缓存下每日首轮需覆盖全量场次(30场×4接口), 180s 足够`

这个"足够"建立在**每接口约 1.5s** 的隐含假设上。实测：

| 指标 | 实测值 |
|---|---|
| `enrich_from_djyy` 处理 6 场 | 130.5s（关闭阵容后 73.4s） |
| 每场耗时（4 接口） | **21.8s** |
| 每接口耗时 | **5.4s** |
| 180s 预算实际能覆盖 | **8 场** |

而账本实测日均 **12.1 场**、峰值 **30 场**：

| 场次 | 需要时间 | 180s 能覆盖 | 丢失 |
|---|---|---|---|
| 10（中位） | 218s | 8 | 20% |
| 12（日均） | 262s | 8 | 33% |
| 30（峰值） | 654s | 8 | **73%** |

**即使在本地，这个缺陷也会让大部分场次拿不到 enrichment。** 与 CI 无关。

### 缺陷 B：所有失败路径都静默

`fetch_fixtures` 返回 0（CI 侧的真实故障模式）→ 直接 `return {}`，
无状态文件、无退出码、无告警。

### 附带：潜在 NameError

`info` 原先只在"预算未耗尽"分支内赋值，但函数末尾
`_extract_context(comparison, info, _lineups)` **无条件引用**它。
预算一过即 `NameError` → 被 `except Exception` 吞掉 → 该场静默丢失。

（这个是被我新写的测试抓出来的。）

---

## 3. 代码侧已完成的修复

全部在 `engine/sources/manager.py`：

| 修复 | 内容 |
|---|---|
| 自适应预算 | `djyy_time_budget(n)` 按场次算，`24s/场`，下限 180s、上限 900s |
| 环境变量 | `DJYY_ENRICH_MIN_BUDGET_S` / `DJYY_ENRICH_SEC_PER_MATCH` / `DJYY_ENRICH_MAX_BUDGET_S` |
| 截断可见 | 预算耗尽时写 `_stats["budget_exhausted"]` 并打印显式告警，不再静默 break |
| 失败可见 | 逐场 `except` 计数 + 打印前 3 个失败原因 |
| 状态落盘 | 新增 `_write_djyy_status()` → `data/dir/djyy_status.json`，含 `coverage` / `budget_exhausted` / `error` |
| 阵容开关 | `DJYY_FETCH_LINEUPS`（默认 **0**）。阵容是 store-only 不参与任何概率计算，却占每场 4 个接口的 1 个 |
| NameError 修复 | `info` 预初始化为 None |

**效果（实测）：** 21.8s/场 → **12.2s/场**，提速 44%；
自适应预算下 30 场可全覆盖（原 180s 只够 8 场）。

---

## 4. CI 侧仍需你操作的部分（我做不了）

`manager.py:400` 的诊断注释写的是「本地正常 / CI 为0」，
说明 2026-10-10 有人已经定位到 CI 侧问题。我从本地侧确认了：
**代码链路现在完全正常，所以剩下的只能是网络可达性差异。**

本地对 `djyylive.com` 连续 12 次请求无任何限流，所以**不是速率限制**。
响应头里有 `x-djyy-edge: MISS`，说明前面有一层自建边缘 Worker。
最可能是它对 GitHub Actions 的数据中心 IP 做了拦截。

### 建议按顺序排查

**① 在 Cloudflare 后台放行 GitHub 的 IP 段**

GitHub 官方维护 Actions 出口 IP 段（会变动，需定期同步）：
`https://api.github.com/meta` → `actions` 字段。

Dashboard → Security → WAF → Custom Rules，新建规则：

```
当    ip.src in { 上述 IP 段 }
并且  http.request.uri.path starts_with "/api/"
动作   Skip  (跳过托管规则 / Managed Rules)
```

注意是 **Skip** 不是 Block —— Actions 是正常客户端，只是被误伤。

**② 或者在边缘 Worker 里加一个共享密钥**

比 IP 白名单稳（GitHub 会换 IP）：

```js
// worker 侧
if (request.headers.get('x-djyy-key') !== env.DJYY_KEY) {
  return new Response('forbidden', { status: 403 });
}
```

```python
# engine/sources/djyy.py 的 HEADERS 里加一行
"X-DJYY-Key": os.environ["DJYY_KEY"],   # 从 GitHub Secrets 注入
```

**③ 无论哪种，都建议加一个存活探针**

`health-check.yml` 已经在跑（每 15 分钟），把 `djyy_status.json` 接进去：

```yaml
- name: DJYY health
  run: |
    python -c "
    import json,sys
    d=json.load(open('data/state/djyy_status.json'))
    print(d['date'], d['coverage'], d['healthy'])
    sys.exit(0 if d['healthy'] else 1)
    "
```

这样 CI 侧一旦又断，会**立刻红**，而不是像过去两个月一样静默地跑完。

---

## 5. 一个需要你判断的产品问题

修好之后，DJYY 会重新拿到 0.2 的权重。但请注意 `upgrade_tracker`
实测的运行时有效权重是 **model 6.25% / market 93.75%**，而模型本身
Brier 0.5925 比市场 0.5631 差。

**所以恢复 DJYY 并不自动等于变好。** 建议恢复后立刻做一次影子对比：

```bash
python -m engine.backtest.gate_test --refit-days 14
```

在 33,060 场上检验：把 DJYY 概率作为第 4 路特征喂进 walk-forward，
看它是否至少**不劣于**"纯市场"。如果劣于，这个权重就该保持 0，
并把 `config/prediction.json` 里的 `djyy_weight` 改成 0，
让配置不再说谎。

---

## 6. 复现命令

```bash
# 站点是否活着
curl -s -o /dev/null -w "%{http_code}\n" https://djyylive.com/data/league-matrix.json

# fixtures 是否返回
curl -s "https://djyylive.com/api/leagues/fixtures?date_from=$(date +%F)&date_to=$(date +%F)" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{len(d)} 场')"

# 某场模型概率
curl -s https://djyylive.com/api/match/<ID>/comparison \
  | python3 -c "import json,sys; m=json.load(sys.stdin)['model']; print(m)"

# 本地跑一遍生产取数链路
python -c "
import sys; sys.path.insert(0,'.')
from datetime import date, timedelta
from pathlib import Path
from engine.sources.manager import SourceManager
m = SourceManager(Path('/tmp/djyy_probe'))
enr = m.enrich_from_djyy([], date.today()+timedelta(days=1), {})
print(enr)
"
print(open('data/state/djyy_status.json').read())
```
