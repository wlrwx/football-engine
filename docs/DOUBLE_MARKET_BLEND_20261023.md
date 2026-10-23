# 市场概率双重计入修复（2026-10-23）

## 问题

同一份市场赔率在预测链里被用了两次：

1. **DC 内部混合**：`engine/prediction/dixon_coles.py:77` 用
   `prediction.market_blend_weight`（原 0.28）把去水市场概率混进模型自身概率；
2. **融合层再混**：`engine/prediction/fusion.py` 的三路融合再给市场一个权重
   （生产 champion 0.75）。

实际市场暴露 = `fusion.market_weight + fusion.model_weight × market_blend_weight`
≈ `0.75 + 0.05 × 0.28 = 0.764`，高于配置意图的 0.75 约 1.4pp。模型那 6.25% 的
边际信息里，还有 28% 是市场自己的回音 —— 等于把"模型贡献"进一步稀释。

## 处理

`config/prediction.json` 的 `prediction.market_blend_weight` 由 `0.28` 改为 `0.0`：
市场只在融合层进入一次；DC 输出回归纯球队实力口径。

## 影响与回滚

- **影响**：DC 模型概率不再被市场收缩 → 模型与市场的分歧变大；因模型权重仅
  ~6.25%，对最终概率的影响量级为 1pp 上下，但对"模型是否真有边际信息"的
  判断更干净（不再自己跟自己比）。
- **回滚**：把 `market_blend_weight` 改回 `0.28` 即可，无需改代码。
- **回归保护**：`tests/test_prediction_semantics.py::test_market_is_counted_once`
  会阻止"两条路径同时开启"的配置再次出现。

## 待观察

改后建议观察 `review_ledger` 里 `brier_model` 与 `brier_final` 的变化：
若模型 Brier 明显变差而 final 不变，说明原内混主要在给模型"续命"，
那就该回到 fusion_optimizer 的权重裁决而不是继续双重计入。
