# 重复逻辑排查（2026-10-11）

## 为什么做这件事

parlay 那个 bug（3串4 容错票被记成亏损）的类型不是"某处写错"，而是
**同一份组合运算被复制了三份、各自漂移**。既然这个类型真出现过一次，
就有必要系统扫一遍，而不是等下一次。

方法：用 AST 扫出跨模块的同名函数，再逐一判断"是真重复还是合理同名"。

## 结论：扫了，只有 1 个是真 bug

| 函数 | 份数 | 判定 |
|---|---|---|
| 串关结算（`settle_ticket` / `main.py` 7b / `reconcile.parlay_pnl`） | 3 | ❌ **真 bug，已修** |
| `brier_score` / `_brier` | 6 | ✅ 数值一致（见下） |
| `layer_of` | 2 | ✅ 有意委派（`ev_report` → `odds_bands`） |
| `_odds_band` | 2 | ✅ 不同用途，非重复 |
| `reconcile` | 2 | ✅ 不同职责，非重复 |

### Brier × 6：实测一致，不是问题

四个单样本实现（`post_match` / `calibration_auto` / `lgbm_shadow` /
`accuracy_trend`）加一个向量化实现（`ts_split`）。

实测（500 组随机三分类样本）：

    4 个单样本实现之间不一致组数：0
    ts_split 向量化版  = 0.8095465371
    逐条 post_match 均值 = 0.8095465371
    差 0.00e+00

只在**畸形输入**上有差异（空 list / 长度≠3）。账本里概率恒为 3 元素，
所以不构成活 bug。**这是潜在健壮性不一致，不是当前缺陷**——
不要因为"看起来像重复"就去动它，改了反而增加无谓 diff。

### `_odds_band` × 2：不同用途

- `pipeline/helpers.py::_odds_band`（含 5.0+ 桶）→ 喂 `_extract_features`
  的 `odds_band`，供**组合挖掘**做模式挖掘
- `review/post_match.py::_odds_band`（已统一到 `odds_bands`）→ 账本分层，
  边界必须与 `favorite_band` 闸门对齐

两者服务的决策不同，分层不同是**设计意图**，合并反而会破坏组合挖掘的
现有分箱。

### `reconcile` × 2：不同职责

- `review/reconcile.py::reconcile(since, apply)` — 独立脚本，扫账本回填
- `review/reconciler.py::MatchReconciler.reconcile(target_date)` — 按日对账，
  被 `review.py` / `officer.py` 引用

后者是活路径，前者无人 import，不存在口径漂移到生产路径的风险。

## 排查方法（可复用）

```python
import ast, glob, collections
defs = collections.defaultdict(list)
for f in glob.glob('engine/**/*.py', recursive=True):
    for n in ast.walk(ast.parse(open(f).read())):
        if isinstance(n, ast.FunctionDef):
            defs[n.name].append(f)
# 跨模块同名 → 人工判断真重复 / 同名异义 / 有意委派
```

跨模块同名里绝大多数是 `__init__` / `main` / `save` 这类生命周期方法
（接口实现），无判断价值。**有判断价值的是"业务语义函数"**——
Brier、赔率分层、结算、概率分档这类。

