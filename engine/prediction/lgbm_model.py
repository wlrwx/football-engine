from __future__ import annotations
"""LightGBM 第三模型层 - 非线性特征交互捕捉

设计原则:
  - 参数全部外部化到 config/prediction.json["lgbm"]
  - 训练数据: data/historical/matches.csv (历史56K场)
  - 特征: Elo差、赔率隐含概率、xG、form、联赛场均、DJYY模型概率
  - 每周 GitHub Actions 自动重训（rolling window）
  - 冷启动: 样本不足时返回 None，不干扰 ensemble
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import lightgbm as lgb
    HAS_LGBM = True
except (ImportError, OSError):
    HAS_LGBM = False
    lgb = None  # type: ignore


@dataclass
class LGBMConfig:
    """LightGBM 配置（全部可由 optimizer 调整）"""
    n_estimators: int = 300
    learning_rate: float = 0.05
    max_depth: int = 6
    num_leaves: int = 31
    min_child_samples: int = 50
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    reg_alpha: float = 0.1
    reg_lambda: float = 1.0
    # 训练窗口
    train_window_days: int = 730  # 2年滚动窗口
    min_train_samples: int = 500  # 低于此数不训练，返回None
    # 特征开关
    use_odds_features: bool = True
    use_xg_features: bool = True
    use_league_features: bool = True
    use_djyy_features: bool = True


# 标准特征列名
FEATURE_NAMES = [
    "elo_diff",           # Elo差（主-客）
    "elo_home",           # 主队Elo
    "elo_away",           # 客队Elo
    "odds_home_impl",     # 赔率隐含主胜概率
    "odds_draw_impl",     # 赔率隐含平局概率
    "odds_away_impl",     # 赔率隐含客胜概率
    "odds_overround",     # 赔率溢水率
    "handicap",           # 让球数
    "home_form_pts",      # 主队近5场积分
    "away_form_pts",      # 客队近5场积分
    "form_diff",          # 积分差
    "league_avg_goals",   # 联赛场均进球
    "league_home_win_rate",  # 联赛主胜率
    "xg_home",            # 主队xG（DJYY）
    "xg_away",            # 客队xG（DJYY）
    "xg_diff",            # xG差
    "djyy_p_home",        # DJYY模型主胜概率
    "djyy_p_draw",        # DJYY模型平局概率
    "djyy_p_away",        # DJYY模型客胜概率
    "is_knockout",        # 是否淘汰赛
    "rank_diff",          # 排名差（如有）
]


class LGBMModel:
    """LightGBM 第三模型层

    作为 ensemble 的第三路信号（DC + MC + LGBM）。
    捕捉参数化模型难以表达的非线性特征交互，
    如"主力伤缺×客场×密集赛程"的联合效应。
    """

    def __init__(self, model_path: Path, config: Optional[LGBMConfig] = None):
        self.model_path = model_path
        self.config = config or LGBMConfig()
        self._model = None
        self._is_trained = False

        # 尝试加载已有模型
        if model_path.exists():
            self._load()

    def _load(self):
        """加载已保存的模型"""
        if not HAS_LGBM:
            return
        try:
            self._model = lgb.Booster(model_file=str(self.model_path))
            self._is_trained = True
        except Exception:
            self._is_trained = False

    def save(self):
        """保存模型到文件"""
        if self._model and self.model_path.parent.exists():
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            self._model.save_model(str(self.model_path))

    def train(self, features: np.ndarray, labels: np.ndarray,
              eval_features: Optional[np.ndarray] = None,
              eval_labels: Optional[np.ndarray] = None):
        """训练模型

        Args:
            features: 训练特征矩阵 (n_samples, n_features)
            labels: 标签 (0=主胜, 1=平局, 2=客胜)
            eval_features: 验证集特征（可选）
            eval_labels: 验证集标签（可选）
        """
        if not HAS_LGBM:
            raise ImportError("lightgbm 未安装，请 pip install lightgbm")

        if len(features) < self.config.min_train_samples:
            print(f"  [LGBM] 训练样本不足 ({len(features)} < {self.config.min_train_samples})，跳过")
            return

        cfg = self.config
        params = {
            "objective": "multiclass",
            "num_class": 3,
            "metric": "multi_logloss",
            "n_estimators": cfg.n_estimators,
            "learning_rate": cfg.learning_rate,
            "max_depth": cfg.max_depth,
            "num_leaves": cfg.num_leaves,
            "min_child_samples": cfg.min_child_samples,
            "subsample": cfg.subsample,
            "colsample_bytree": cfg.colsample_bytree,
            "reg_alpha": cfg.reg_alpha,
            "reg_lambda": cfg.reg_lambda,
            "verbose": -1,
            "n_jobs": -1,
        }

        callbacks = [lgb.log_evaluation(period=0)]  # 静默
        if eval_features is not None and eval_labels is not None:
            callbacks.append(lgb.early_stopping(stopping_rounds=30, verbose=False))

        self._model = lgb.LGBMClassifier(**params)
        fit_kwargs = {"callbacks": callbacks}
        if eval_features is not None:
            fit_kwargs["eval_set"] = [(eval_features, eval_labels)]

        self._model.fit(features, labels, **fit_kwargs)
        self._is_trained = True
        self.save()
        print(f"  [LGBM] 训练完成: {len(features)} 样本, "
              f"{cfg.n_estimators} 棵树")

    def predict_proba(self, features: np.ndarray) -> Optional[np.ndarray]:
        """预测概率

        Args:
            features: 特征矩阵 (n_samples, n_features)

        Returns:
            概率矩阵 (n_samples, 3) [主胜, 平局, 客胜]，或 None（模型未训练）
        """
        if not self._is_trained or self._model is None:
            return None

        if not HAS_LGBM:
            return None

        try:
            if hasattr(self._model, "predict_proba"):
                return self._model.predict_proba(features)
            else:
                # Booster 对象用 predict
                raw = self._model.predict(features)
                if raw.ndim == 2:
                    return raw
                return raw.reshape(-1, 3)
        except Exception:
            return None

    def predict_single(self, feature_dict: dict) -> Optional[tuple[float, float, float]]:
        """单场预测（便捷接口）

        Args:
            feature_dict: {feature_name: value} 字典

        Returns:
            (p_home, p_draw, p_away) 或 None
        """
        if not self._is_trained:
            return None

        # 构造特征向量
        vec = np.array([[feature_dict.get(name, 0.0) for name in FEATURE_NAMES]])
        probs = self.predict_proba(vec)
        if probs is not None and len(probs) > 0:
            return float(probs[0][0]), float(probs[0][1]), float(probs[0][2])
        return None

    @property
    def is_available(self) -> bool:
        """模型是否可用（已训练且lightgbm已安装）"""
        return HAS_LGBM and self._is_trained

    @property
    def feature_importance(self) -> Optional[dict[str, float]]:
        """特征重要性"""
        if not self._is_trained or self._model is None:
            return None
        try:
            if hasattr(self._model, "feature_importances_"):
                imp = self._model.feature_importances_
            else:
                imp = self._model.feature_importance()
            return {name: float(v) for name, v in zip(FEATURE_NAMES, imp)}
        except Exception:
            return None


def build_features(
    elo_home: float,
    elo_away: float,
    odds: Optional[tuple] = None,
    handicap: Optional[float] = None,
    home_form: float = 0.0,
    away_form: float = 0.0,
    league_avg_goals: float = 2.5,
    league_home_win_rate: float = 0.45,
    xg_home: Optional[float] = None,
    xg_away: Optional[float] = None,
    djyy_probs: Optional[dict] = None,
    is_knockout: bool = False,
    rank_home: Optional[int] = None,
    rank_away: Optional[int] = None,
) -> dict:
    """从原始数据构造标准特征字典

    供 LGBMModel.predict_single() 使用。
    """
    features = {}

    # Elo 特征
    features["elo_home"] = elo_home
    features["elo_away"] = elo_away
    features["elo_diff"] = elo_home - elo_away

    # 赔率隐含概率
    if odds and all(o and o > 0 for o in odds):
        raw_probs = [1.0 / o for o in odds]
        overround = sum(raw_probs)
        features["odds_home_impl"] = raw_probs[0] / overround
        features["odds_draw_impl"] = raw_probs[1] / overround
        features["odds_away_impl"] = raw_probs[2] / overround
        features["odds_overround"] = overround - 1.0
    else:
        features["odds_home_impl"] = 0.0
        features["odds_draw_impl"] = 0.0
        features["odds_away_impl"] = 0.0
        features["odds_overround"] = 0.0

    # 让球
    features["handicap"] = handicap or 0.0

    # Form
    features["home_form_pts"] = home_form
    features["away_form_pts"] = away_form
    features["form_diff"] = home_form - away_form

    # 联赛
    features["league_avg_goals"] = league_avg_goals
    features["league_home_win_rate"] = league_home_win_rate

    # xG（DJYY）
    features["xg_home"] = xg_home or 0.0
    features["xg_away"] = xg_away or 0.0
    features["xg_diff"] = (xg_home or 0.0) - (xg_away or 0.0)

    # DJYY 模型概率
    if djyy_probs and djyy_probs.get("home"):
        features["djyy_p_home"] = djyy_probs["home"]
        features["djyy_p_draw"] = djyy_probs.get("draw", 0.0)
        features["djyy_p_away"] = djyy_probs.get("away", 0.0)
    else:
        features["djyy_p_home"] = 0.0
        features["djyy_p_draw"] = 0.0
        features["djyy_p_away"] = 0.0

    # 情境
    features["is_knockout"] = 1.0 if is_knockout else 0.0

    # 排名差
    if rank_home and rank_away:
        features["rank_diff"] = float(rank_away - rank_home)  # 正=主队排名高
    else:
        features["rank_diff"] = 0.0

    return features
