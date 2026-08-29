# 深挖成果交接：4 个新模块（已通过自检）

> 由 OpenSquilla 深挖 JetQiao/football-prediction-skill、cnemri/world-cup-2026-predictor、
> Hicruben/world-cup-2026-prediction-model 三个仓库后提炼落地。全部为增量模块，
> 不破坏现有管线，自检均已通过（python3 直接 import + 跑通）。

## 1. engine/prediction/market_isolation.py — 市场隔离审计【P0 最重要】

**来源**：JetQiao 核心方法论（reference/target/benchmark 三市场隔离）。

**问题**：本地 `lgbm_model.py` 的 `build_features()` 直接把 `odds_home_impl/draw/away` 当特征。
如果这些赔率来自竞彩官方（target 市场），等于"用庄家的价格预测庄家的价格"，
价值发现退化（模型向市场收敛，EV 永远≈0）。

**接口**：
```python
from engine.prediction.market_isolation import (
    audit_features, strip_target_market_features, summarize,
)
clean, report = strip_target_market_features(features)  # 自动剔除违规特征
# report.status == 'PASS'/'FAIL', report.to_dict() 可入页面
```

**落地建议**：
1. `lgbm_model.py` 的 build_features 加参数 `include_market_odds: bool = True`，
   默认 **False**（隔离）；只有显式传入 Pinnacle/参考市场赔率时才允许入特征
2. main.py 里调用处传入 `include_market_odds=False`（用 sina 竞彩赔率时）
3. 周报/页面加"隔离审计"小节：`summarize(reports)`

## 2. engine/prediction/reliability_curve.py — 可靠性曲线 + ECE【P0】

**来源**：Hicruben（"说 70% 就真的发生 70%"）。本地有 Brier 但缺分箱可靠性曲线。

**接口**：
```python
from engine.prediction.reliability_curve import (
    compute_reliability, compute_outcome_reliability, save_report,
)
rep = compute_reliability(probs, actuals)   # 单类
reports = compute_outcome_reliability(prob_matrix, actual_idx)  # 胜平负三类
save_report(rep, path)  # JSON 供 build_site 画图
```

**落地建议**：
1. walk_forward.py 已算 ece 标量，补充调用本模块输出 bins 数据
2. build_site.py 加"校准可靠性"区块：每箱 (预测均值, 实际频率) 画对角线散点

## 3. engine/backtest/horizon_eval.py — 预测时点分桶【P1】

**来源**：JetQiao horizons（T-24h/T-6h/T-90m/收盘 同一语义）。

**问题**：本地每天 11:15 定时跑（开售即跑，距开赛常 12-16h），但从不评估
"哪个时点的预测最准/EV 最高"。

**接口**：
```python
from engine.backtest.horizon_eval import infer_horizon, evaluate_by_horizon
h = infer_horizon(as_of, kickoff)   # 't24h' | 't6h' | 't90m' | 'closing'
report = evaluate_by_horizon(records)  # records 需含 as_of/kickoff/probs/actual_idx/ev
```

**落地建议**：
1. 预测记录落盘时带上 `as_of` 和 `kickoff` 字段
2. 周报加"时点对比"：哪个时点 brier 最低、EV 最高 → 决定出手时机

## 4. engine/prediction/shrinkage_dc.py — 分层收缩 Dixon-Coles【P1】

**来源**：JetQiao（低样本球队向联赛均值收缩 + 时间衰减 + 联合拟合 rho）。
本地 rho_fitter.py 只 fit rho，本模块是完整 MLE 超集。

**接口**：
```python
from engine.prediction.shrinkage_dc import fit_shrinkage_dc, save_model, load_model
model = fit_shrinkage_dc(match_results)   # 默认属性名兼容 MatchResult
save_model(model, path)
model.predict_probs(home, away)  # → (主胜, 平, 客胜)
```

**落地建议**：
1. 接入 rho_fitter 同款调用点（main.py / learning），作为 ensemble 的新成员
2. 与现有 DixonColesModel 对比 brier，champion/challenger 决定是否上位

## 验证记录

```
[1] market_isolation: PASS | clean: ['elo_home', 'pinnacle_home_impl']
[2] reliability: ECE=0.0230 MCE=0.0399 bins=10
[3] horizon: t24h | buckets: [t24h, t6h, t90m, closing]
[4] shrinkage_dc: rho=-0.0914 n=200 teams=8 | T0 vs T1: [0.271, 0.303, 0.426]
```

## 优先级

- **P0 先做**：市场隔离（改 lgbm 特征）+ 可靠性曲线（页面展示）
- **P1 次之**：时点分桶接入周报、shrinkage_dc 加入 ensemble 对比
- **P2 后做**：页面重构（简洁大气）、公开 track record 页
