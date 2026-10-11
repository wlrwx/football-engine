"""CPPI 重置可审计性回归测试（2026-10-11）

## 背景

2026-08-04 的提交 `2b3e72212` 记录了 CPPI 资金账本崩坏
（current=66462 但 peak=377156/floor=264009，cushion 为负），
并做了两件事：一次性重置账本到基准 5000，以及给 `update()` 加了破产保护
（跌破 floor×0.5 自动重置棘轮）。

**但重置本身没有留下任何可审计痕迹**：CPPI 的 `history` 只有
`{bankroll, peak, floor}` 三个值，重置在曲线上只是又一次无法解释的突降。
于是"是谁、何时、因为什么触发的"完全查不到 —— 这正是当时需要翻
提交信息才能还原的原因。

现在重置会写入 `last_reset` 并在 history 条目上打标记。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.strategy.cppi import CPPIStrategy  # noqa: E402


@pytest.fixture()
def cp(tmp_path):
    return CPPIStrategy(tmp_path / "cppi.json", initial_bankroll=10000)


# ---------- 正常路径不留噪声 ----------

def test_no_reset_marker_on_normal_update(cp):
    cp.update(12000)
    cp.update(11500)
    cp.update(11000)
    assert cp.last_reset is None
    assert not any(h.get("reset") for h in cp.state.history), \
        "正常波动不应产生 reset 标记"
    assert cp.state.history, "正常波动仍应记录历史"


def test_peak_and_floor_advance_normally(cp):
    cp.update(20000)
    assert cp.state.peak_bankroll == pytest.approx(20000)
    assert cp.state.floor == pytest.approx(20000 * 0.7)
    assert cp.last_reset is None


# ---------- 重置路径必须留痕 ----------

def test_bankruptcy_reset_is_recorded(cp):
    """2026-08-04 那类重置必须留下完整记录。"""
    cp.update(12000)                     # peak=12000 floor=8400
    cp.update(2000)                      # 2000 < 8400*0.5 → 触发
    assert cp.last_reset is not None
    r = cp.last_reset
    assert "破产保护" in r["reason"]
    # from_* 是**变更前**的账本状态，to_* 是变更后 ——
    # "从 12000 掉到 2000" 才是重置事件的完整表述
    assert r["from_bankroll"] == pytest.approx(12000)
    assert r["from_floor"] == pytest.approx(8400)
    assert r["from_peak"] == pytest.approx(12000)   # 崩坏时的旧峰值正是问题所在
    assert r["to_bankroll"] == pytest.approx(2000)
    assert r["to_floor"] == pytest.approx(2000 * 0.7)
    assert "at" in r


def test_history_entry_carries_reset_flag(cp):
    """重置那一条 history 必须带 reset 标记，否则曲线上仍只是一次突降。"""
    cp.update(12000)
    cp.update(2000)
    last = cp.state.history[-1]
    assert last.get("reset") is True
    assert "破产保护" in last.get("reset_reason", "")
    assert last["reset_from"]["peak"] == pytest.approx(12000)
    assert last["reset_from"]["floor"] == pytest.approx(8400)


def test_reset_is_persisted_across_restart(tmp_path):
    """重置标记必须落盘，重启后仍可查。"""
    p = tmp_path / "cppi.json"
    a = CPPIStrategy(p, initial_bankroll=10000)
    a.update(12000)
    a.update(1500)
    a.save()

    disk = json.loads(p.read_text(encoding="utf-8"))
    flagged = [h for h in disk["history"] if h.get("reset")]
    assert flagged, "落盘的历史里必须有 reset 标记"
    assert flagged[-1]["reset_from"]["peak"] == pytest.approx(12000)


def test_status_report_exposes_last_reset(cp):
    cp.update(12000)
    cp.update(2000)
    rep = cp.status_report()
    assert rep.get("last_reset") is not None
    assert "破产保护" in rep["last_reset"]["reason"]


# ---------- 阈值语义 ----------

def test_bankruptcy_threshold_is_half_of_floor(cp):
    """触发条件是 new_bankroll < floor * 0.5（提交信息所述）。"""
    cp.update(12000)                     # floor = 8400 → 触发线 4200
    cp.update(4300)                      # 4300 > 4200 → 不触发
    assert cp.last_reset is None
    cp.update(4100)                      # 4100 < 4200 → 触发
    assert cp.last_reset is not None


def test_ratchet_reset_relaxes_floor(cp):
    """重置后 floor 应按新 bankroll 重算，否则仍是"永久锁死投注"。"""
    cp.update(12000)
    cp.update(2000)
    assert cp.state.floor == pytest.approx(2000 * 0.7)
    assert cp.state.floor < 2000, "重置后 floor 必须低于当前资产，否则无法继续投注"


# ---------- workflow cron 与阈值一致性 ----------

def test_health_check_cron_matches_threshold():
    """cron 与"未运行多久算异常"的阈值必须自洽。

    回归动机：cron 声明 */15 时把阈值设成 60 分钟看似合理，
    但实测公开仓库高频定时任务被限流、实际 4-5 小时才跑一次 ——
    那个 60 分钟阈值因此成为**永不生效的死逻辑**。
    """
    yml = (ROOT / ".github" / "workflows" / "health-check.yml").read_text(encoding="utf-8")
    code = "\n".join(l for l in yml.splitlines() if not l.strip().startswith("#"))
    # 已改为每小时
    assert "cron: '0 * * * *'" in code, "cron 应为每小时"
    assert 'if [ "$ELAPSED_MIN" -gt 90 ]' in code, "阈值应为 90 分钟（对应每小时一轮）"
    assert 'if [ "$ELAPSED_MIN" -gt 60 ]' not in code, "旧的 60 分钟阈值是死逻辑"
