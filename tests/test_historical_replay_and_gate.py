"""历史回放与门禁测试的正确性回归（2026-10-11）

新模块引入了两处最容易静默出错的核心计算，必须锁住：

1. `devig()` — 去抽水口径错了，后面所有"市场基准"都是错的，而且不会报错，
   只会安静地给出一个看起来合理的数字。
2. `paired_test()` — 符号约定（负 = 模型更好）在两次改写里被搞反过一次。
3. `isotonic_fit()` — PAV 实现。自己写是因为校准是本项目最核心的资产，
   必须是可解释的而不是黑箱，但自己写就必须有测试。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.backtest.historical_replay import (  # noqa: E402
    brier, devig, line_movement, log_loss, paired_test, bh_fdr, rps,
)
from engine.backtest.gate_test import isotonic_calibrate, isotonic_fit, apply_isotonic  # noqa: E402


# ---------- 去抽水 ----------

def test_devig_sums_to_one():
    for odds in [(2.10, 3.40, 3.80), (1.25, 5.50, 9.00), (4.00, 3.50, 1.90)]:
        for m in ("proportional", "power", "shin"):
            p = devig(odds, m)
            assert abs(sum(p) - 1.0) < 1e-9, f"{m} {odds} 和={sum(p)}"
            assert all(x >= 0 for x in p)


def test_devig_preserves_favourite_ordering():
    """1.25/5.50/9.00：去水后主胜仍必须最高。"""
    h, d, a = devig((1.25, 5.50, 9.00), "proportional")
    assert h > d > a


def test_devig_removes_vig():
    """含 10% 水位的赔率，去水后隐含概率应显著低于原始倒数和。"""
    odds = (2.10, 3.40, 3.80)
    raw_sum = sum(1 / o for o in odds)
    p = devig(odds, "proportional")
    # 无水位基准下，fair 概率应大致等于 1/odds / sum(1/odds)
    for k in range(3):
        assert abs(p[k] - (1 / odds[k]) / raw_sum) < 1e-9


def test_devig_unknown_method_raises():
    with pytest.raises(ValueError):
        devig((2.0, 3.0, 4.0), "magic")


# ---------- 评分 ----------

def test_brier_perfect_and_worst():
    y = 0
    assert brier(np.array([1.0, 0.0, 0.0]), y) == pytest.approx(0.0)
    assert brier(np.array([0.0, 0.0, 1.0]), y) == pytest.approx(2.0)


def test_logloss_penalises_confident_error_more():
    """过度自信的错误必须比同样误差的保守预测更痛。"""
    y = 0
    assert log_loss(np.array([0.30, 0.35, 0.35]), y) > log_loss(np.array([0.90, 0.05, 0.05]), y)


def test_rps_rewards_adjacent_over_far_error():
    """RPS 的意义就在这：错成相邻类别应比错成远端类别轻。

    注意必须用**端点类别**做测试。平局在 home/draw/away 序里居中，
    错成主胜与错成客胜距离相等 —— 这是 RPS 的正确性质，不是 bug。
    """
    y = 0   # home，端点
    near = rps(np.array([0.10, 0.80, 0.10]), y)      # 错成 draw（相邻）
    far = rps(np.array([0.10, 0.10, 0.80]), y)       # 错成 away（远端）
    assert near < far


def test_rps_draw_is_equidistant_from_both_ends():
    """平局居中：错成主胜与错成客胜的 RPS 应当相等。"""
    y = 1
    to_home = rps(np.array([0.80, 0.10, 0.10]), y)
    to_away = rps(np.array([0.10, 0.10, 0.80]), y)
    assert to_home == pytest.approx(to_away)


# ---------- 配对检验 ----------

def test_paired_test_sign_convention_negative_is_better():
    """模型更好 → 差值为负。这是全项目统一的符号约定。"""
    better = [-0.02, -0.03, -0.01, -0.04, -0.02] * 10
    worse = [0.02, 0.03, 0.01, 0.04, 0.02] * 10
    rb = paired_test(better)
    rw = paired_test(worse)
    assert rb["mean"] < 0 and rb["t"] < 0
    assert rw["mean"] > 0 and rw["t"] > 0
    assert rb["significant"] and rw["significant"]


def test_paired_test_ci_brackets_mean():
    r = paired_test([-0.01, -0.02, -0.03, -0.015] * 8)
    lo, hi = r["ci95"]
    assert lo < r["mean"] < hi


def test_paired_test_not_significant_for_noise():
    """纯零假设噪声：|t| 不应离谱。

    注意不能用 `significant is False` —— t 统计量在真零假设下仍有 5% 概率
    越过 1.96，那样写测试本身就不稳。改为断言 |t| 落在宽松界内。
    """
    rng = np.random.default_rng(7)
    noise = list(rng.normal(0, 0.05, 200))
    assert abs(paired_test(noise)["t"]) < 3.5


def test_paired_test_handles_tiny_n():
    r = paired_test([-0.01])
    assert r["n"] == 1 and r["mean"] is None


# ---------- BH-FDR ----------

def test_bh_fdr_controls_false_positives():
    """全为真的零假设时，不应有任何 reject。"""
    p = {f"L{i}": 0.5 for i in range(20)}
    out = bh_fdr(p, alpha=0.10)
    assert not any(v["reject"] for v in out.values())


def test_bh_fdr_rejects_real_signal():
    p = {f"L{i}": 0.9 for i in range(18)}
    p["L_real"] = 1e-6
    out = bh_fdr(p, alpha=0.10)
    assert out["L_real"]["reject"] is True


# ---------- isotonic ----------

def test_isotonic_fit_is_monotone_nondecreasing():
    x = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    y = np.array([0.9, 0.2, 0.8, 0.1, 0.6, 0.05])   # 故意乱序
    xp, yp = isotonic_fit(x, y)
    assert np.all(np.diff(xp) >= -1e-12), "拟合的 x 必须单调"
    assert np.all(np.diff(yp) >= -1e-12), "拟合的 y 必须单调"


def test_isotonic_already_monotone_is_unchanged():
    x = np.array([0.1, 0.2, 0.3])
    y = np.array([0.05, 0.15, 0.45])
    xp, yp = isotonic_fit(x, y)
    assert len(xp) == 3 and len(yp) == 3
    assert np.allclose(yp, y), "本来就单调时不应合并任何块"


def test_isotonic_calibrate_on_perfect_data_recovers_identity():
    """完美校准的数据：y = x，拟合应接近恒等。"""
    x = np.linspace(0.05, 0.95, 400)
    curve = isotonic_calibrate(x, x)
    assert curve["ok"] is True
    out = apply_isotonic(curve, list(x))
    assert np.max(np.abs(out - x)) < 0.05


def test_isotonic_flattens_overconfidence():
    """过度自信数据（y 远低于 x）应被压平 —— 这正是门禁测试要测的效果。"""
    rng = np.random.default_rng(3)
    x = np.concatenate([np.linspace(0.05, 0.3, 1500),
                        np.linspace(0.5, 0.95, 1500)])
    y = np.concatenate([rng.binomial(1, np.clip(x[:1500], .01, .99)),
                        rng.binomial(1, np.clip(x[1500:] * 0.45, .01, .99))])
    curve = isotonic_calibrate(x, y)
    out = apply_isotonic(curve, [0.8])
    assert out[0] < 0.6, "高置信区间的校准值必须被显著压低"


def test_apply_isotonic_degrades_gracefully():
    """曲线不可用时原样返回，不能抛异常让整条流水线挂掉。"""
    assert list(apply_isotonic({"ok": False}, [0.3, 0.6])) == [0.3, 0.6]


# ---------- 盘口移动 ----------

def test_line_movement_detects_direction():
    from engine.backtest.historical_replay import HistMatch
    from datetime import date
    ms = [
        # 市场收盘时把主胜从 40% 抬到 60%
        HistMatch(date=date(2024, 1, 1), league="X", home="A", away="B", hg=1, ag=0,
                  close=(1.60, 4.0, 5.0), open_=(2.40, 3.6, 3.2)),
    ]
    mv = line_movement(ms)
    assert mv["n"] == 1
    assert mv["home"]["mean"] > 0.05, "主胜去水概率应在收盘上升"
    assert mv["away"]["mean"] < 0
