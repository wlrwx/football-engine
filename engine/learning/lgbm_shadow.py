"""LGBM 影子训练与复活评估（2026-08-30）

历史问题（docs/UPGRADE2_20260829.md）：
  1. LGBM 无训练调用点 → is_available 恒 False，从未参与过预测
  2. 若当时直接接通，会用污染链账本 + train/serve 特征不一致的旧特征训练

本模块的闭环（对齐 calibration_auto 的进化模式）：
  - 训练数据 = chain=="v2" 洁净样本，特征从每日 predictions.json 的
    **预测时冻结值**重建（elo/xg/handicap/djyy），与 serve 端 build_features
    同源同参 → train/serve 零偏移（form/league/rank 两端同为常数，天然一致）
  - 洁净样本 ≥ min_train_samples（默认 500）才训练
  - 训练后时间顺序 70/30 切分做影子验证：holdout 上 LGBM 概率 vs 生产融合概率
    的配对 Brier——只有显著更好才标记 ready=true（人工翻 config 启用，
    绝不自动进生产，模型流历史信誉太差）
  - 状态落盘 data/state/lgbm_status.json，周报可查
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from engine.prediction.lgbm_model import build_features

DEFAULTS = {
    "min_train_samples": 500,   # 洁净样本门槛（对齐 config lgbm.min_train_samples）
    "holdout_frac": 0.3,
    "min_improvement": 0.002,
    "min_z": 1.96,
}


def _brier(p, a):
    return sum((x - (1.0 if i == a else 0.0)) ** 2 for i, x in enumerate(p))


def build_training_rows(clean_records: list[dict], daily_root: Path) -> list[dict]:
    """从每日 predictions.json 重建洁净样本的 (特征, 标签)。

    只用预测时冻结字段，与 serve 端 build_features 同参调用。
    缺字段的记录跳过（返回行数可能少于输入）。
    """
    by_date: dict[str, dict[str, dict]] = {}
    rows = []
    for r in clean_records:
        d = r.get("date", "")
        if d not in by_date:
            pf = daily_root / d / "predictions.json"
            try:
                preds = json.loads(pf.read_text(encoding="utf-8"))
                by_date[d] = {m.get("match_id", ""): m for m in preds}
            except Exception:
                by_date[d] = {}
        p = by_date[d].get(r.get("match_id", ""))
        if not p:
            continue
        try:
            feats = build_features(
                elo_home=float(p.get("elo_home") or 1500),
                elo_away=float(p.get("elo_away") or 1500),
                handicap=p.get("handicap"),
                xg_home=p.get("home_xg"),
                xg_away=p.get("away_xg"),
                djyy_probs=p.get("djyy_model_prob"),
                include_market_odds=False,
            )
        except Exception:
            continue
        rows.append({"features": feats, "label": int(r["actual_idx"]),
                     "final_prob": list(r["final_prob"])})
    return rows


def _paired(diffs):
    n = len(diffs)
    if n == 0:
        return 0.0, 0.0
    m = sum(diffs) / n
    var = sum((x - m) ** 2 for x in diffs) / max(1, n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    return m, se


def shadow_train(clean_records: list[dict], daily_root: Path, model_path: Path,
                 lgbm_cfg=None, config: dict | None = None,
                 trainer=None) -> dict:
    """洁净样本足够 → 训练 + 影子验证；不足 → 只报状态。

    trainer 可注入（测试用）；缺省用 LGBMModel。
    返回 status dict（调用方落盘 lgbm_status.json）。绝不改动生产融合。
    """
    cfg = {**DEFAULTS, **(config or {})}
    status = {
        "trained": False, "ready": False, "clean_n": len(clean_records),
        "usable_rows": 0, "holdout_n": 0,
        "holdout_brier_lgbm": None, "holdout_brier_fusion": None,
        "delta": None, "t": None, "reason": "", "trained_at": None,
    }

    rows = build_training_rows(clean_records, daily_root)
    status["usable_rows"] = len(rows)
    min_n = int(cfg["min_train_samples"])
    if len(rows) < min_n:
        status["reason"] = (f"洁净可用样本 {len(rows)} < {min_n}，"
                            f"按 ~15 场/天约 {max(0, (min_n - len(rows))) // 15 + 1} 天后达标")
        return status

    try:
        if trainer is None:
            from engine.prediction.lgbm_model import LGBMModel
            model = LGBMModel(model_path, config=lgbm_cfg)
            trainer = model
        import numpy as np

        rows_sorted = rows  # clean_records 已按时间排序
        split = int(len(rows_sorted) * (1 - float(cfg["holdout_frac"])))
        train_rows, hold_rows = rows_sorted[:split], rows_sorted[split:]

        def _matrix(rs):
            keys = sorted(rs[0]["features"].keys())
            X = np.array([[r["features"][k] for k in keys] for r in rs])
            y = np.array([r["label"] for r in rs])
            return X, y

        Xtr, ytr = _matrix(train_rows)
        Xho, yho = _matrix(hold_rows)

        trainer.train(Xtr, ytr, eval_features=Xho, eval_labels=yho)

        # 影子验证：holdout 上 lgbm vs 生产融合概率（配对）
        diffs = []
        lgbm_b, fus_b = [], []
        for r, x in zip(hold_rows, Xho):
            pred = trainer.predict_single(dict(zip(sorted(hold_rows[0]["features"].keys()), x)))
            if not pred:
                continue
            pl = list(pred)[:3]
            s = sum(pl)
            pl = [v / s for v in pl]
            lgbm_b.append(_brier(pl, r["label"]))
            fus_b.append(_brier(r["final_prob"], r["label"]))
            diffs.append(fus_b[-1] - lgbm_b[-1])
        status["holdout_n"] = len(diffs)
        status["holdout_brier_lgbm"] = round(sum(lgbm_b) / len(lgbm_b), 4) if lgbm_b else None
        status["holdout_brier_fusion"] = round(sum(fus_b) / len(fus_b), 4) if fus_b else None
        m, se = _paired(diffs)
        status["delta"] = round(m, 5)
        status["t"] = round(m / se, 2) if se > 0 else 0.0

        if m > max(float(cfg["min_improvement"]), float(cfg["min_z"]) * se):
            status["ready"] = True
            status["reason"] = (f"影子验证显著优于生产融合 (Δ={m:+.4f}, t={status['t']}) "
                                f"→ 具备启用条件，人工评估后翻 config fusion.post_fusion.lgbm_blend")
        else:
            status["reason"] = f"影子验证未显著优于生产融合 (Δ={m:+.4f}, t={status['t']}) → 保持关闭"

        # 生产模型 = 全量洁净样本重训（供 ready 后启用时使用）
        Xall, yall = _matrix(rows_sorted)
        trainer.train(Xall, yall)
        try:
            trainer.save()
        except Exception:
            pass
        status["trained"] = True
        from datetime import datetime
        status["trained_at"] = datetime.now().isoformat(timespec="seconds")
    except ImportError as e:
        status["reason"] = f"环境缺依赖（{e}），训练推迟到 CI 侧执行"
    except Exception as e:
        status["reason"] = f"训练异常: {e}"
    return status
