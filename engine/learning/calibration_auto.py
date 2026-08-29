"""洁净数据驱动的校准层自动进化（2026-08-29）

背景：2026-08-29 消融回放证明，旧校准层（isotonic/temperature）在污染链
数据上拟合后对最终概率造成 +0.004 Brier 伤害，已被 config 开关关闭。
但校准本身不是罪——罪在拟合数据被旧链污染。本模块实现「自进化闭环」：

  1. 结算只把 chain=="v2"（概率层重构后）的账本样本喂给校准器
  2. 洁净样本 ≥ min_samples 时，时间顺序 60/40 切分，
     验证段配对检验「校准是否真的降低 Brier」
  3. 显著改善 → 自动启用；已启用但验证段退化 > rollback → 自动停用
  4. 决策与状态落盘 data/state/calibration_status.json，全程可审计

设计原则与 FusionOptimizer 相同：任何自动行为必须通过配对显著性检验，
绝不因样本噪声翻转。
"""

from __future__ import annotations

import math
import tempfile
from datetime import datetime
from pathlib import Path

DEFAULTS = {
    "min_samples": 100,        # 洁净样本低于此数不做启用决策
    "val_frac": 0.4,           # 时间顺序验证段占比
    "min_improvement": 0.002,  # 启用所需的最小 Brier 改善
    "min_z": 1.96,             # 配对显著性门槛（95%）
    "rollback_degradation": 0.03,  # 已启用时验证段退化超过此值 → 停用
}


def _brier(probs, actual_idx: int) -> float:
    return sum((p - (1.0 if i == actual_idx else 0.0)) ** 2 for i, p in enumerate(probs))


def _paired(records, calibrate_fn) -> tuple[float, float, int]:
    """验证段配对：返回 (mean improvement, SE, n)。improvement>0 = 校准更好。"""
    diffs = []
    for r in records:
        raw = list(r["final_prob"])
        act = r["actual_idx"]
        try:
            cal = list(calibrate_fn(tuple(raw)))
        except Exception:
            return 0.0, 0.0, 0
        diffs.append(_brier(raw, act) - _brier(cal, act))
    n = len(diffs)
    if n == 0:
        return 0.0, 0.0, 0
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / max(1, n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    return mean, se, n


def decide_calibration(clean_records: list[dict], current_enabled: bool,
                       config: dict | None = None) -> dict:
    """判定校准层（isotonic）是否应自动启用。

    clean_records: [{"date","final_prob","actual_idx"}, ...]（chain==v2）
    current_enabled: 当前状态文件里的 enabled

    返回 status dict（调用方负责落盘）。纯函数：不在生产状态上拟合。
    """
    cfg = {**DEFAULTS, **(config or {})}
    status = {
        "enabled": current_enabled,
        "clean_n": len(clean_records),
        "val_delta_brier": 0.0,
        "t": 0.0,
        "reason": "",
        "decided_at": datetime.now().isoformat(timespec="seconds"),
    }

    if len(clean_records) < int(cfg["min_samples"]):
        status["reason"] = f"洁净样本不足 ({len(clean_records)} < {cfg['min_samples']})，维持现状"
        return status

    # 时间顺序切分（记录须已按 date 排序，调用方保证；这里再做一次稳定排序）
    recs = sorted(clean_records, key=lambda r: (r.get("date", ""), r.get("match_id", "")))
    split = int(len(recs) * (1 - float(cfg["val_frac"])))
    train, val = recs[:split], recs[split:]
    if len(train) < 30 or len(val) < 20:
        status["reason"] = f"切分后样本不足 (train {len(train)} / val {len(val)})，维持现状"
        return status

    from engine.prediction.isotonic_cal import IsotonicCalibrator
    scratch = Path(tempfile.gettempdir()) / f"cal_auto_{id(cfg)}.pkl"
    if scratch.exists():
        scratch.unlink()
    calibrator = IsotonicCalibrator(scratch)
    try:
        import numpy as np
        calibrator.fit(
            np.array([r["final_prob"] for r in train]),
            np.array([r["actual_idx"] for r in train]),
        )
        if not calibrator.is_fitted:
            status["reason"] = "训练段未拟合出有效校准器，维持现状"
            return status
        mean, se, n = _paired(val, calibrator.calibrate)
    finally:
        if scratch.exists():
            scratch.unlink()

    status["val_delta_brier"] = round(mean, 5)
    status["t"] = round(mean / se, 2) if se > 0 else 0.0

    if not current_enabled:
        if mean > max(float(cfg["min_improvement"]), float(cfg["min_z"]) * se):
            status["enabled"] = True
            status["reason"] = f"验证段显著改善 (Δ={mean:+.4f}, t={status['t']}) → 自动启用"
        else:
            status["reason"] = f"验证段无显著改善 (Δ={mean:+.4f}, t={status['t']}) → 维持关闭"
    else:
        if mean < -float(cfg["rollback_degradation"]):
            status["enabled"] = False
            status["reason"] = f"验证段退化超限 (Δ={mean:+.4f}) → 自动停用"
        else:
            status["reason"] = f"已启用且未退化 (Δ={mean:+.4f}) → 维持启用"
    return status
