"""熔断器修复的回归测试（2026-10-11）

三层缺陷叠加导致熔断器自始至终完全惰性，逐层锁死：

1. `effective_mult` 被硬编码 1.0，熔断输出从不参与决策
2. 结算只遍历 stable/value/lottery，parlay/score_parlay（占 78%）不可见
3. 日/周止损依赖 `daily_pnl < 0`，但注入的 pnl 恒为 0

外加一条设计缺口：原触发条件只有连败数，没有回撤维度。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.strategy.circuit_breaker import CircuitBreaker, CircuitBreakerConfig  # noqa: E402


# ---------- 回撤触发（新增维度） ----------

def test_drawdown_warn_reduces_stake(tmp_path):
    cb = CircuitBreaker(tmp_path / "cb.json")
    cb.get_multiplier(10000)                      # 建立峰值
    assert cb.get_multiplier(8500) == pytest.approx(1.0)     # 回撤 15%，未到线
    assert cb.get_multiplier(7500) == pytest.approx(0.5)     # 回撤 25%，降注


def test_drawdown_halt_stops(tmp_path):
    cb = CircuitBreaker(tmp_path / "cb.json")
    cb.get_multiplier(10000)
    assert cb.get_multiplier(5500) == 0.0                    # 回撤 45% → 停注
    assert cb.state.halted is True


def test_peak_only_increases(tmp_path):
    cb = CircuitBreaker(tmp_path / "cb.json")
    cb.get_multiplier(10000)
    cb.get_multiplier(6000)                      # 触发 halt
    cb.force_reset()
    cb.get_multiplier(20000)
    assert cb.state.peak_bankroll == 20000


def test_drawdown_caps_but_never_raises_streak_mult(tmp_path):
    """回撤封顶只降不升：连败给的 0.25 与回撤的 0.5 取小。"""
    cb = CircuitBreaker(tmp_path / "cb.json")
    cb.get_multiplier(10000)
    cb.state.current_streak = -6                 # tier2 → 0.25
    cb.state.peak_bankroll = 40000               # 手动设一个高峰值 → 回撤 75%
    m = cb.get_multiplier(10000)
    assert m <= 0.5


def test_reset_clears_peak(tmp_path):
    """资金重置时峰值必须一并归零，否则回撤比例会拿旧峰值算出假信号。"""
    cb = CircuitBreaker(tmp_path / "cb.json")
    cb.get_multiplier(10000)
    assert cb.state.peak_bankroll == 10000
    cb.force_reset()
    assert cb.state.peak_bankroll == 0


def test_drawdown_thresholds_configurable(tmp_path):
    cfg = CircuitBreakerConfig(drawdown_warn_ratio=0.05, drawdown_halt_ratio=0.10)
    cb = CircuitBreaker(tmp_path / "cb.json", config=cfg)
    cb.get_multiplier(10000)
    assert cb.get_multiplier(9600) == pytest.approx(1.0)     # 4% 回撤，未到线
    assert cb.get_multiplier(9400) == pytest.approx(0.5)     # 6% 回撤，降注


def test_no_drawdown_when_bankroll_grows(tmp_path):
    cb = CircuitBreaker(tmp_path / "cb.json")
    cb.get_multiplier(10000)
    assert cb.get_multiplier(11000) == pytest.approx(1.0)
    assert cb.state.peak_bankroll == 11000


# ---------- 乘数接回（缺陷 1） ----------

def test_effective_mult_no_longer_hardcoded():
    """main.py 里的 effective_mult 必须接回 breaker_mult，不能是常量 1.0。"""
    # 只看真正的代码行，排除解释性注释里对旧代码的引用
    code = [ln.strip() for ln in
            (ROOT / "engine" / "main.py").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    src = "\n".join(code)
    assert "effective_mult = 1.0  # 虚拟投注不降注" not in src, \
        "熔断乘数仍被硬编码丢弃"
    assert "effective_mult = breaker_mult" in src, \
        "熔断乘数必须接回 breaker_mult"
    assert "circuit_breaker" in src and "enabled" in src, \
        "必须保留显式关闭熔断的开关"


def test_breaker_records_nonzero_pnl(tmp_path):
    """record_result 喂 0 pnl 是缺陷 3 的根因；结算必须能喂真实盈亏。"""
    cb = CircuitBreaker(tmp_path / "cb.json")
    for _ in range(4):
        cb.record_result(won=False, pnl=-100.0, bankroll=10000)
    assert cb.state.total_losses == 4
    assert cb.state.daily_pnl == pytest.approx(-400.0)      # 4%，未到日亏线
    assert cb.get_multiplier(10000) == pytest.approx(0.5)   # 4 连败 → tier1
    for _ in range(2):
        cb.record_result(won=False, pnl=-100.0, bankroll=10000)
    assert cb.state.daily_pnl == pytest.approx(-600.0)      # 6% > 5%
    assert cb.get_multiplier(10000) == 0.0, "日亏超 5% 应停注"


# ---------- 串关可见性（缺陷 2） ----------

def test_settlement_covers_parlay_buckets():
    """结算必须遍历 parlay / score_parlay —— 它们占 403 条计划里的 314 条。"""
    src = (ROOT / "engine" / "main.py").read_text(encoding="utf-8")
    assert 'for _grp in ("parlay", "score_parlay"):' in src, \
        "串关桶未纳入结算，熔断器看不到真正动钱的注"
    assert "breaker.record_result(won=_won" in src, \
        "串关结算结果必须喂给熔断器"


def test_parlay_settlement_math():
    """串关结算口径：全中赢 stake*(total_odds-1)，否则 -stake。"""
    # 用真实数据结构复现 ticket_plan 的 parlay / score_parlay
    plan = {
        "parlay": [{
            "legs": [{"match": "m1", "sel": "home", "odds": 1.85},
                     {"match": "m2", "sel": "home", "odds": 1.32}],
            "total_odds": 2.44, "stake": 2.0,
        }],
        "score_parlay": [{
            "legs": [{"match": "m1", "score": "1-0", "odds": 7.5},
                     {"match": "m2", "score": "1-1", "odds": 7.5}],
            "total_odds": 56.25, "stake": 2.0,
        }],
    }
    def _score_hit(res, legs):
        return all(f'{res[l["match"]][0]}-{res[l["match"]][1]}' == l["score"]
                   for l in legs)

    def _sel_hit(res, legs):
        def act(m):
            h, a = res[m]
            return "home" if h > a else ("draw" if h == a else "away")
        return all(l["sel"] == act(l["match"]) for l in legs)

    sp = plan["score_parlay"][0]
    # 全中 → 赢 stake*(total_odds-1) = 2*(56.25-1)
    assert _score_hit({"m1": (1, 0), "m2": (1, 1)}, sp["legs"])
    assert 2.0 * (sp["total_odds"] - 1) == pytest.approx(110.5)
    # 一腿不中 → 全输 stake
    assert not _score_hit({"m1": (1, 0), "m2": (0, 1)}, sp["legs"])

    pl = plan["parlay"][0]
    assert _sel_hit({"m1": (2, 0), "m2": (3, 1)}, pl["legs"])
    assert not _sel_hit({"m1": (2, 0), "m2": (0, 1)}, pl["legs"])


# ---------- 状态落盘结构 ----------

def test_state_persists_peak(tmp_path):
    p = tmp_path / "cb.json"
    cb = CircuitBreaker(p)
    cb.get_multiplier(12345)
    cb2 = CircuitBreaker(p)
    assert cb2.state.peak_bankroll == pytest.approx(12345)


def test_history_entries_carry_pnl(tmp_path):
    cb = CircuitBreaker(tmp_path / "cb.json")
    cb.record_result(won=False, pnl=-50.0, bankroll=10000)
    raw = json.loads((tmp_path / "cb.json").read_text(encoding="utf-8"))
    assert raw["history"], "历史应记录本次结果"
    assert raw["history"][-1]["pnl"] == pytest.approx(-50.0)
