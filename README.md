# Sporttery Engine

竞彩足球概率分析与虚拟投注模拟系统。基于 Dixon-Coles 泊松模型 + 蒙特卡洛模拟，
市场概率主导融合，Kelly 准则做虚拟投注分配，全自动运行在 GitHub Actions 上，
零服务器成本。**仅用于数据分析与算法学习，不构成任何投注建议，所有输出均为虚拟记账。**

## 核心理念（2026-08 数据实证驱动）

1. **市场是基准**：426 场账本实证，纯市场（去水）Brier 0.583 优于任何模型流
   （0.646+）→ 融合权重市场主导（0.75），模型/第三方仅微调
2. **一切开关数据裁决**：融合链每一步都是 config 开关
   （`config/prediction.json["fusion"]["post_fusion"]`），
   由 `scripts/ablation_replay.py` 对已结算账本做配对回放决定去留
3. **自进化带护栏**：校准层/LGBM 的任何自动启用必须通过时间切分 +
   配对显著性检验，退化自动停用——详见 `docs/UPGRADE2_20260829.md`

## 架构

```
每日循环: 抓数据 → 融合预测(fusion.py, 全程trace) → 锁定 → 结算 → 自学习
每周循环: 回测 → 权重优化(champion/challenger) → 参数重拟合 → 周一体检
进化闭环: 洁净样本(chain=v2)累积 → 校准层自动复活/停用(calibration_auto)
                      → LGBM 影子训练+验证(lgbm_shadow) → 周度效果判定(tracker)
```

## 技术栈

- Python 3.12 / numpy / scikit-learn / LightGBM / joblib
- GitHub Actions（计算）+ GitHub Pages（报告）
- 无数据库，纯 CSV/JSON + git 版本化，账本 append-only

## 目录结构

```
engine/
├── prediction/     # dixon_coles / monte_carlo / fusion(纯函数+开关+trace) / lgbm
├── strategy/       # kelly / 水位信号闸门 / 热门区注量 / edge封顶 / 三票制
├── learning/       # fusion_optimizer(权重自调优) / calibration_auto(校准自进化)
│                   # lgbm_shadow(影子训练) / league_params / elo
├── backtest/       # walk_forward / horizon_eval / weekly_run
├── review/         # post_match(账本) / ev_report / reconciler
├── integrity/      # decision_bundle(SHA-256链) / plan_lock
└── sources/        # 可插拔数据源（体彩/新浪/DJYY/500彩票）
scripts/            # ablation_replay(消融回放) / upgrade_tracker(效果追踪)
                    # edge_calibration(EV校准体检) / fetch_*(数据抓取)
docs/               # 每轮升级的数字与理由（UPGRADE*.md）
```

## 快速开始

```bash
pip install -r requirements.txt
python -m engine.main --date today          # 每日预测
python -m engine.main --settle --date $(date -d yesterday +%F)  # 结算
python -m pytest tests/ -q                  # 测试（CI 同款另需 ruff --select F）
python scripts/upgrade_tracker.py           # 升级效果追踪
python scripts/ablation_replay.py           # 融合链消融回放
python scripts/edge_calibration.py          # EV 校准体检
```

## 演进史（数字说话）

| 日期 | 事件 | 关键数字 |
|---|---|---|
| 08-04 | 复盘闭环诊断：融合权重与信号质量倒挂 | final 45.0% < 市场 50.0% |
| 08-14 | 深挖批次：水位信号反向 bug、以市场为主导 | Brier 0.69→0.63 |
| 08-29 | 融合链重构+消融定参：关 lgbm/新鲜度/iso/temp/水位概率修正 | Brier 0.617→0.588(回放) |
| 08-30 | EV 校准体检：价值优势幻觉实证 | edge>0.10 桶 ROI -19% → 封顶 0.10 |

## 免责声明

本项目仅用于数据分析、算法学习和系统开发验证，不构成任何投注建议。
所有输出均为虚拟记账条目（shadow 模式）。
