# football-data.co.uk 授权说明与接入现状（2026-10-28）

## 1. 它是不是收费商用 API

**不是。** 站方自述「My data is free」，提供 Excel/CSV 下载，给个人做量化分析；
没有 API key、没有计费、没有 SLA。站上推广的 TheStatsAPI / OddsRelay 是它推荐的
**第三方付费服务**，与本项目无关——我们没有用它们。

### 授权与使用条件（据站点 about / disclaimer）

| 项 | 事实 | 我们的做法 |
|---|---|---|
| 价格 | 免费下载，无需注册 | 直接抓 CSV |
| 许可 | 未声明开源许可证；disclaimer 只做**责任与博彩合规免责**，未授予再分发许可 | 视为「自用数据」：只在本机/私有仓库缓存，不对外分发、不打包进任何发布产物 |
| 署名 | 未强制，但站方是个人维护的免费数据源 | 文档与脚本头注释均注明来源 football-data.co.uk |
| 内容风险 | 数据不保证正确；含博彩赔率，站点声明仅供信息参考 | 只用于内部回测/CLV 判据，不对外发布赔率 |

结论：**可以合规使用于自用分析，但不能当商用 API、不能二次分发原始 CSV。**

## 2. 接口与覆盖

| 类型 | URL | 覆盖 |
|---|---|---|
| 赛季制 | `https://football-data.co.uk/mmz4281/{2526,2627}/{DIV}.csv` | E0/E1/D1/D2/SP1/SP2/I1/I2/F1/F2/N1/P1/SC0/G1/T1/SW1 |
| 日历年 | `https://football-data.co.uk/new/{CODE}.csv` | JPN/CHN/BRA/FIN/NOR/SWE/USA/ARG/RO1/POL/RUS/MEX/AUT（2012 至今） |

关键列：`PSCH/PSCD/PSCA`（Pinnacle 收盘）、`B365CH`、`MaxCH`、`AvgCH`、`FTHG/FTAG`。

**域名必须不带 www**：带 www 的入口在部分网络下 TLS 握手被中断
（`SSLEOFError: UNEXPECTED_EOF_WHILE_READING`），实测 12 个联赛文件全抓不下来；
换成不带 www 后 43/45 成功。

## 3. 本次修掉的三处真实缺陷

| # | 缺陷 | 影响 | 修法 |
|---|---|---|---|
| 1 | **缓存永不刷新**：`if dest.exists() and size>5000: continue` | 数据自 2026-10-09 停更 19 天，CLV 面板停在 10-08；新增 272 注全部匹配失败，却只被记成 `league_uncovered/no_en/no_match` | 每次都重下；失败才回退旧缓存并记 `status.json` |
| 2 | **代理写死** `127.0.0.1:7897`（本机 Clash） | CI runner 无此端口 → 必然失败 → workflow `\|\| true` 吞掉 → 云端「每天在刷新」是幻觉 | 改 `FDC_PROXY` 环境变量，未设置即直连 |
| 3 | **CLV 脚本没有任何 workflow 调用** | `data/state/clv_pinnacle.json` 停在 10-23（ok=258），新注不进面板 | ci-sync 增加「重算下注级 CLV」步骤 |

附带：`data/state/fdc/*.csv` 与 `parsed.jsonl` 不再入库（43 个 CSV + 26MB，
一旦恢复每日刷新会以约 10MB/天膨胀历史），改用 Actions 缓存传递，仓库里只留
`status.json` 与 `clv_pinnacle.json`；`health-check` 增加「最新数据 >8 天」报警。

## 4. 最终结果（2026-10-28 本机真跑）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 文件刷新 | 0/45（从不刷新） | **43/45**（SW1 两季返回 HTTP 300，站方改名） |
| 最新数据日期 | 2026-10-09 | **2026-10-27** |
| parsed 行数 | 60,912 | **61,704** |
| CLV 可匹配注 | 258 / 984（26%） | **384 / 1256（30.6%）** |
| CLV 均值 | −6.27%（t=−11.35） | **−6.91%（t=−15.29）** |
| 面板最新匹配日 | 2026-10-08 | **2026-10-26** |

**基准口径必须说明**：384 条全部是 `benchmark=b365`。源数据里 Pinnacle 收盘价
（PSCH）在 2025 年之后基本为空（BRA 2026 年 319 行，PSCH 非空 0），退到 B365
收盘是唯一选择。所以「−6.91%」的准确表述是**「相对 B365 收盘」**，不是
「相对 Pinnacle 尖锐收盘」。报告已按 `benchmark_stats` 分开统计。

## 5. 复现

```bash
# 抓取（直连；本机需走代理时 export FDC_PROXY=http://127.0.0.1:7897）
python scripts/fetch_football_data.py
cat data/state/fdc/status.json        # files_ok / latest_date / failed

# 重算 CLV
python scripts/clv_pinnacle.py
python -c "import json;d=json.load(open('data/state/clv_pinnacle.json'));print(d['overall'],d['benchmark_stats'])"
```
