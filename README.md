# Sporttery Engine

竞彩足球概率分析与虚拟投注模拟系统。基于 Dixon-Coles 泊松模型 + 蒙特卡洛模拟，结合 Kelly 准则做虚拟投注分配，全自动运行在 GitHub Actions 上，零服务器成本。

## 架构

```
每日循环: 抓数据 → 预测 → 锁定 → 结算 → 更新Elo
每周循环: 回测 → 参数优化 → 模型训练 → champion/challenger 评估
每月循环: 月度报告 → 人工审核 → 策略调整
```

## 技术栈

- Python 3.12
- GitHub Actions（计算）+ GitHub Pages（报告）
- numpy / scikit-learn / joblib
- 无数据库，纯 CSV/JSON + git 版本化

## 快速开始

```bash
pip install -r requirements.txt
python -m engine.main --date today
```

## 目录结构

```
engine/
├── sources/        # 数据源插件（可插拔）
├── prediction/     # 预测模型（dixon_coles / monte_carlo / ensemble）
├── strategy/       # 投注策略（kelly / risk / recommendation）
├── learning/       # 自学习（champion_challenger / elo / param_optimizer）
├── backtest/       # 回测框架
└── integrity/      # 不可变决策链
```

## 免责声明

本项目仅用于数据分析、算法学习和系统开发验证，不构成任何投注建议。所有输出均为虚拟记账条目。
