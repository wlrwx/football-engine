"""LGBM 影子训练门控测试（2026-08-30）

不依赖 lightgbm（本地无 libomp）——只测门控与数据重建路径；
真训练路径由注入的 trainer 桩覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.learning.lgbm_shadow import build_training_rows, shadow_train


def _make_daily(tmp_path: Path, n: int = 3):
    """构造 n 天 × 每天若干场的 daily predictions + 返回洁净账本记录"""
    clean = []
    for d_idx in range(n):
        date = f"2026-09-{d_idx + 1:02d}"
        dd = tmp_path / "daily" / date
        dd.mkdir(parents=True)
        preds = []
        for m in range(10):
            mid = f"{date}_测试{m:03d}"
            preds.append({
                "match_id": mid,
                "elo_home": 1500 + m * 5,
                "elo_away": 1500 - m * 5,
                "handicap": -0.5,
                "home_xg": 1.4 + m * 0.05,
                "away_xg": 1.1,
                "djyy_model_prob": {"home": 0.5, "draw": 0.25, "away": 0.25},
            })
            clean.append({"match_id": mid, "date": date, "chain": "v2",
                          "actual_idx": m % 3,
                          "final_prob": [0.4, 0.3, 0.3]})
        (dd / "predictions.json").write_text(json.dumps(preds, ensure_ascii=False))
    return clean


class _FakeTrainer:
    """可注入的桩：记录 train 调用，predict 返回均匀分布"""

    def __init__(self):
        self.train_calls = 0
        self.saved = False

    def train(self, X, y, eval_features=None, eval_labels=None):
        self.train_calls += 1

    def predict_single(self, feats):
        return [0.34, 0.33, 0.33]

    def save(self):
        self.saved = True


def test_build_training_rows_from_daily(tmp_path):
    clean = _make_daily(tmp_path, n=2)
    rows = build_training_rows(clean, tmp_path / "daily")
    assert len(rows) == 20
    assert rows[0]["label"] in (0, 1, 2)
    assert "elo_diff" in rows[0]["features"]


def test_below_min_samples_not_trained(tmp_path):
    clean = _make_daily(tmp_path, n=2)  # 20 行 < 500
    fake = _FakeTrainer()
    status = shadow_train(clean, tmp_path / "daily",
                          tmp_path / "lgbm_model.txt", trainer=fake,
                          config={"min_train_samples": 500})
    assert status["trained"] is False
    assert status["ready"] is False
    assert fake.train_calls == 0
    assert "500" in status["reason"]


def test_sufficient_samples_trains_and_evaluates(tmp_path):
    clean = _make_daily(tmp_path, n=60)  # 600 行 ≥ 门槛
    fake = _FakeTrainer()
    status = shadow_train(clean, tmp_path / "daily",
                          tmp_path / "lgbm_model.txt", trainer=fake,
                          config={"min_train_samples": 500, "holdout_frac": 0.3})
    assert status["trained"] is True
    assert fake.train_calls == 2  # 切分训练 + 全量重训
    assert fake.saved is True
    # 桩输出恒定均匀分布，final_prob=[0.4,0.3,0.3] —— lgbm vs fusion 的配对可计算
    assert status["holdout_n"] > 0
    assert status["holdout_brier_lgbm"] is not None


def test_trainer_exception_is_contained(tmp_path):
    clean = _make_daily(tmp_path, n=60)

    class _Boom(_FakeTrainer):
        def train(self, X, y, **kw):
            raise RuntimeError("boom")

    status = shadow_train(clean, tmp_path / "daily",
                          tmp_path / "lgbm_model.txt", trainer=_Boom(),
                          config={"min_train_samples": 500})
    assert status["trained"] is False
    assert "boom" in status["reason"] or "训练异常" in status["reason"]
