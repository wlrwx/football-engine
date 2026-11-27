from __future__ import annotations
"""LightGBM 第三模型层 - 非线性特征交互捕捉

设计原则:
  - 参数全部外部化到 config/prediction.json["lgbm"]
  - 训练数据: data/historical/matches.csv (历史56K场)
  - 特征: Elo差、赔率隐含概率、xG、form、联赛场均、DJYY模型概率
  - 每周 GitHub Actions 自动重训（rolling window）
  - 冷启动: 样本不足时返回 None，不干扰 ensemble
"""
from dataclasses import dataclass
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
    use_odds_features: bool = False  # 市场隔离：默认不使用竞彩目标市场赔率作特征
    use_xg_features: bool = True
    use_league_features: bool = True
    use_djyy_features: bool = True
    missing_as_nan: bool = True


# 标准特征列名（**原有 21 列的顺序不可动**：已训练的 booster 按列号取值，
# 中间插一列等于让整个模型读错位。侧挂特征只能追加在末尾。）
BASE_FEATURE_NAMES = [
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

# 侧挂特征（NJStats 旁路，见 engine/sources/njstats_side.py）。追加在末尾，
# 缺数据填 0。当前线上 booster 只有 21 列，predict_single 会按 booster 的
# num_feature() 截断，所以这份列表变长不会改变现有输出；下次影子重训吃到
# 22~27 列后自动生效。
SIDE_FEATURE_NAMES = [
    "side_open_home",     # NJStats 开盘去水：主
    "side_open_draw",     # NJStats 开盘去水：平
    "side_open_away",     # NJStats 开盘去水：客
    "side_ppg_home",      # 主队主场场均积分
    "side_ppg_away",      # 客队客场场均积分
    "side_ppg_diff",      # 两者之差（正=主队占优）
]

# 缺失指示列（追加在末尾，不破坏老模型前 21/27 列的列号映射）
MISSING_FEATURE_NAMES = [
    "xg_missing",         # 1.0=缺失 xG，进树向量按 NaN 处理
    "djyy_missing",       # 1.0=缺失 DJYY 概率
    "handicap_missing",   # 1.0=缺失让球线
    "rank_missing",       # 1.0=缺失联赛排名
]

FEATURE_NAMES = BASE_FEATURE_NAMES + SIDE_FEATURE_NAMES + MISSING_FEATURE_NAMES

_MISSING_FLAG_MAP = {
    "xg_home": "xg_missing",
    "xg_away": "xg_missing",
    "xg_diff": "xg_missing",
    "djyy_p_home": "djyy_missing",
    "djyy_p_draw": "djyy_missing",
    "djyy_p_away": "djyy_missing",
    "handicap": "handicap_missing",
    "rank_diff": "rank_missing",
}


def feature_dict_to_vector(
    feature_dict: dict,
    names: Optional[list[str]] = None,
    missing_as_nan: bool = True,
) -> list[float]:
    """将特征字典转为树模型输入向量；当 missing_as_nan=True 时把缺失标记对应列转为 np.nan。"""
    cols = names if names is not None else FEATURE_NAMES
    vec: list[float] = []
    for name in cols:
        flag_key = _MISSING_FLAG_MAP.get(name)
        if missing_as_nan and flag_key and float(feature_dict.get(flag_key, 0.0) or 0.0) == 1.0:
            vec.append(float(np.nan))
            continue
        val = feature_dict.get(name, 0.0)
        if val is None:
            vec.append(float(np.nan) if missing_as_nan else 0.0)
        else:
            vec.append(float(val))
    return vec


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
        self.best_iteration_: Optional[int] = None

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
        """保存模型到文件（2026-09-21 修复：train 产出 LGBMClassifier（sklearn 接口，
        无 save_model），须经 booster_ 导出为 Booster 文本格式——与 _load 的
        lgb.Booster(model_file=...) 对称；原生 Booster 对象仍直接 save_model）"""
        if not self._model:
            return
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(self._model, "booster_"):
            self._model.booster_.save_model(str(self.model_path))
        else:
            self._model.save_model(str(self.model_path))

    def train(self, features: np.ndarray, labels: np.ndarray,
              eval_features: Optional[np.ndarray] = None,
              eval_labels: Optional[np.ndarray] = None,
              n_estimators_override: Optional[int] = None):
        """训练模型

        Args:
            features: 训练特征矩阵 (n_samples, n_features)
            labels: 标签 (0=主胜, 1=平局, 2=客胜)
            eval_features: 验证集特征（可选）
            eval_labels: 验证集标签（可选）
            n_estimators_override: 全量重训时按早停最优迭代数限制树数（可选）
        """
        if not HAS_LGBM:
            raise ImportError("lightgbm 未安装，请 pip install lightgbm")

        if len(features) < self.config.min_train_samples:
            print(f"  [LGBM] 训练样本不足 ({len(features)} < {self.config.min_train_samples})，跳过")
            return

        cfg = self.config
        n_trees = (
            int(n_estimators_override)
            if n_estimators_override and int(n_estimators_override) > 0
            else cfg.n_estimators
        )
        params = {
            "objective": "multiclass",
            "num_class": 3,
            "metric": "multi_logloss",
            "n_estimators": n_trees,
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
        best_it = getattr(self._model, "best_iteration_", None)
        self.best_iteration_ = (
            int(best_it) if isinstance(best_it, (int, np.integer)) and int(best_it) > 0 else None
        )
        self._is_trained = True
        self.save()
        print(f"  [LGBM] 训练完成: {len(features)} 样本, "
              f"{self.best_iteration_ or n_trees} 棵树")

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

        # 只喂 booster 认得的列数：老模型 21 列时侧挂列自动截掉，输出与接线前逐位相同
        n = len(FEATURE_NAMES)
        num_feature = getattr(self._model, "num_feature", None)
        if callable(num_feature):
            try:
                n = min(n, int(num_feature()))
            except Exception:
                pass
        vec = np.array([
            feature_dict_to_vector(
                feature_dict,
                FEATURE_NAMES[:n],
                missing_as_nan=self.config.missing_as_nan,
            )
        ])
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
    include_market_odds: bool = False,
    side: Optional[dict] = None,
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
    # 市场隔离（P0）：目标市场（竞彩）赔率不应进入模型特征，否则等于用庄家价格预测庄家价格。
    # 只有显式传入参考市场（如 Pinnacle）赔率时才允许开启。
    if include_market_odds and odds and all(o and o > 0 for o in odds):
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

    # 侧挂（NJStats 旁路）：缺席一律 0（树模型里 0 与缺失同义，够用）
    for name in SIDE_FEATURE_NAMES:
        try:
            features[name] = float((side or {}).get(name, 0.0))
        except (TypeError, ValueError):
            features[name] = 0.0

    # 缺失指示位（字典值保持纯 float 以便 JSON 落盘与相等性比较；转矩阵时再按需映射 NaN）
    features["xg_missing"] = 0.0 if (xg_home is not None and xg_away is not None) else 1.0
    features["djyy_missing"] = 0.0 if (djyy_probs and djyy_probs.get("home")) else 1.0
    features["handicap_missing"] = 0.0 if handicap is not None else 1.0
    features["rank_missing"] = 0.0 if (rank_home and rank_away) else 1.0

    return features
