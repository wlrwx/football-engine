# 仓库体积策略（2026-10-23）

## 现状

`git count-objects -vH` 显示 `.git` 约 16.5MB，但工作树 142MB —— 大量运行时数据
被版本控制，且部分文件每天 append：

| 文件 | 大小 | 性质 |
|---|---|---|
| `data/state/fdc/parsed.jsonl` | 26MB | 抓取中间产物（可重抓） |
| `data/historical/matches.csv` | 3.9MB | 历史赛果（基本不变） |
| `data/historical/odds.csv` | 2.4MB | 历史赔率（基本不变） |
| `data/models/lgbm_model.txt` | 1.2MB | 模型权重（二进制文本，无 delta） |
| `data/state/review_ledger.jsonl` | 1.1MB | **append-only 账本**（只增不减） |
| `data/state/njstats/*_odds.jsonl` | 1.2MB × 2 | 站点赔率快照 |

## 已做的治理

1. **CI 守卫**：`.github/workflows/test.yml` 新增 `Repo size guard`，
   `scripts/check_repo_size.py` 对 `git ls-files` 里 >5MB 的文件报错
   （历史遗留白名单豁免）。新增大文件会在 PR 阶段就被拦下。
2. `.gitignore` 已经排除可重抓的原始赔率缓存（见既有注释）。

## 待裁决：历史清理

清历史需要 `git filter-repo` + force-push，会重写所有 commit（包括 CI 的
自动提交），属于不可逆操作，**未做**。建议路径（按风险从低到高）：

1. **不动历史**：只靠 CI 守卫拦住新增，仓库体积停在当前水位（推荐，风险 0）
2. **迁 LFS**：对 `data/models/*.txt`、`data/historical/*.csv` 走 LFS，
   历史不动、后续增量走 LFS（需全员重 clone）
3. **重写历史**：`git filter-repo --path data/state/fdc/parsed.jsonl --invert-paths`
   —— 体积能降下来，但所有 commit hash 变化，CI 的自动提交与账本链需要重新对齐

## 判定口径

- 单文件 >5MB 且不属于"不可再得"的数据 → 不入 git
- 每次运行都会变大的文件（账本、日志、快照）→ 只留派生结论，原始数据走 Release/对象存储
