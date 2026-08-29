"""校准层自动进化决策测试（2026-08-29）

decide_calibration 的四种裁决路径：
  A. 洁净样本不足 → 维持现状（不动）
  B. 原始概率已良校准 → 无显著改善 → 维持关闭
  C. 原始概率系统性过自信（isotonic 能修复）→ 显著改善 → 自动启用
  D. 已启用但验证段退化超限 → 自动停用（回滚）
"""

from __future__ import annotations

import random

from engine.learning.calibration_auto import decide_calibration


def _norm3(x, y, z):
    s = x + y + z
    return [x / s, y / s, z / s]


def _mk_records(specs):
    """specs: list of (probs, actual, date) → 决策函数入参格式"""
    return [
        {"match_id": f"m{i}", "date": d, "final_prob": list(p), "actual_idx": a}
        for i, (p, a, d) in enumerate(specs)
    ]


def _gen(rng, n, mode):
    """生成 (probs, actual) 对。

    mode="overconfident": raw 比 true 更极端（isotonic 可修复）
    mode="calibrated": raw 即 true（无改进空间）
    mode="underconfident_val": 验证段欠自信（让学到的校准反向伤害）
    """
    out = []
    for i in range(n):
        qh = 0.35 + 0.30 * rng.random()
        q = _norm3(qh, 0.25, 1 - qh - 0.25)
        u = rng.random()
        a = 0 if u < q[0] else (1 if u < q[0] + q[1] else 2)
        if mode == "overconfident":
            p = _norm3(q[0] ** 3.0, q[1] ** 3.0, q[2] ** 3.0)
        elif mode == "underconfident_val":
            p = _norm3(q[0] ** 0.6, q[1] ** 0.6, q[2] ** 0.6)
        else:
            p = q
        out.append((p, a, f"2026-09-{1 + i // 20:02d}"))
    return out


CFG = {"min_samples": 100, "val_frac": 0.4, "min_improvement": 0.002,
       "min_z": 1.96, "rollback_degradation": 0.03}


def test_insufficient_samples_keeps_state():
    rng = random.Random(1)
    specs = _gen(rng, 50, "overconfident")
    status = decide_calibration(_mk_records(specs), current_enabled=False, config=CFG)
    assert status["enabled"] is False
    assert "不足" in status["reason"]
    assert status["clean_n"] == 50


def test_calibrated_raw_stays_disabled():
    rng = random.Random(2)
    specs = _gen(rng, 300, "calibrated")
    status = decide_calibration(_mk_records(specs), current_enabled=False, config=CFG)
    assert status["enabled"] is False
    assert status["val_delta_brier"] <= CFG["min_improvement"] + 0.005


def test_overconfident_raw_auto_enables():
    rng = random.Random(3)
    specs = _gen(rng, 300, "overconfident")
    status = decide_calibration(_mk_records(specs), current_enabled=False, config=CFG)
    assert status["enabled"] is True, f"reason={status['reason']} delta={status['val_delta_brier']}"
    assert status["val_delta_brier"] > CFG["min_improvement"]


def test_rollback_when_degraded():
    # 构造性极端：train 段全主胜(raw_home=0.7 → isotonic 学到 0.7→1.0)，
    # val 段全客胜同分布 raw → 校准把 home 推向 1.0 造成重创 → 必须停用
    specs = []
    for i in range(180):
        specs.append(([0.7, 0.15, 0.15], 0, f"2026-09-{1 + i // 20:02d}"))
    for i in range(120):
        specs.append(([0.7, 0.15, 0.15], 2, f"2026-10-{1 + i // 20:02d}"))
    status = decide_calibration(_mk_records(specs), current_enabled=True, config=CFG)
    assert status["enabled"] is False, f"reason={status['reason']} delta={status['val_delta_brier']}"
    assert status["val_delta_brier"] < -CFG["rollback_degradation"]


def test_enabled_and_healthy_stays_enabled():
    # train 过自信（校准学得动），val 也过自信（校准仍有益）→ 维持启用
    rng = random.Random(5)
    train = _gen(rng, 180, "overconfident")
    val = _gen(rng, 120, "overconfident")
    specs = train + val
    status = decide_calibration(_mk_records(specs), current_enabled=True, config=CFG)
    assert status["enabled"] is True
    assert status["val_delta_brier"] >= -CFG["rollback_degradation"]
