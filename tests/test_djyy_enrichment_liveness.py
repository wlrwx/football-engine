"""DJYY enrichment 活性回归测试（2026-10-11）

背景：实测 v2 链 529 场 djyy_prob 命中率为 0，但 config 仍给 djyy 0.2 名义权重。
追查发现两个**互相独立**的缺陷：

  缺陷 A（时间预算）：DJYY_ENRICH_TIME_BUDGET 固定 180s，原注释假设每接口 1.5s，
    实测 5.4s/接口 → 21.8s/场 → 180s 只够 8 场。而账本实测日均 12.1 场、
    峰值 30 场 → 典型日 1/3、峰值日 73% 的场次永远拿不到 enrichment。
    且原实现 `break` 是**静默**的。

  缺陷 B（逐场异常）：`except Exception: continue` 把逐场失败全部吞掉，
    加上 fetch_fixtures 返回 0 时直接 `return {}`，整条链路失效时零告警。

这些测试锁住修复后的行为：预算自适应、截断可见、失败可见、状态落盘。
"""
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.sources import manager as mgr  # noqa: E402


# ---------- 预算自适应 ----------

def test_budget_scales_with_match_count():
    """场次越多预算越大，不能再是固定 180s。"""
    small = mgr.djyy_time_budget(5)
    mid = mgr.djyy_time_budget(15)
    big = mgr.djyy_time_budget(30)
    assert mid > small
    assert big > mid


def test_budget_covers_observed_latency():
    """实测 21.8s/场。典型日 12 场、峰值日 30 场必须被预算完全覆盖。"""
    per_match_observed = 21.8
    for n in (10, 12, 20, 30):
        assert mgr.djyy_time_budget(n) >= n * per_match_observed, \
            f"{n} 场在实测延迟下会被截断"


def test_budget_has_floor_and_ceiling():
    """有下限（空跑不忙等）和上限（第三方卡死不能拖垮流水线）。"""
    assert mgr.djyy_time_budget(0) == mgr.DJYY_ENRICH_MIN_BUDGET
    assert mgr.djyy_time_budget(10000) == mgr.DJYY_ENRICH_MAX_BUDGET


def test_budget_respects_env(monkeypatch):
    monkeypatch.setenv("DJYY_ENRICH_MIN_BUDGET_S", "600")
    monkeypatch.setenv("DJYY_ENRICH_MAX_BUDGET_S", "1200")
    import importlib
    mod = importlib.reload(mgr)
    try:
        assert mod.djyy_time_budget(1) == 600
        assert mod.djyy_time_budget(10000) == 1200
    finally:
        monkeypatch.undo()
        importlib.reload(mod)


# ---------- 失败必须可见 ----------

def test_fetch_failure_writes_status_not_silent(tmp_path):
    """fetch_fixtures 抛异常时，必须落盘错误状态并返回空，
    而不是悄无声息地让 djyy 权重变成死权重。"""
    class Boom(mgr.DJYYSource):
        def fetch_fixtures(self, target_date):
            raise RuntimeError("network unreachable")

        def fetch_match_comparison(self, i):
            return None

    m = mgr.SourceManager(tmp_path)
    m._djyy = Boom()
    out = m.enrich_from_djyy([], __import__("datetime").date(2026, 10, 12))

    assert out == {}
    status = json.loads((tmp_path / "djyy_status.json").read_text(encoding="utf-8"))
    assert status["healthy"] is False
    assert status["error"].startswith("fetch_fixtures:")
    assert "network unreachable" in status["error"]


def test_empty_fixtures_writes_status(tmp_path):
    """返回 0 场（CI 侧真实故障模式）也必须落状态，不能只 print。"""
    class Empty(mgr.DJYYSource):
        def fetch_fixtures(self, target_date):
            return []

        def fetch_match_comparison(self, i):
            return None

    m = mgr.SourceManager(tmp_path)
    m._djyy = Empty()
    m.enrich_from_djyy([], __import__("datetime").date(2026, 10, 12))

    status = json.loads((tmp_path / "djyy_status.json").read_text(encoding="utf-8"))
    assert status["healthy"] is False
    assert status["error"] == "fetch_fixtures_empty"


def test_budget_exhaustion_is_marked(tmp_path, monkeypatch):
    """预算耗尽必须写进状态文件并置 unhealthy——原实现是静默 break。"""
    monkeypatch.setenv("DJYY_ENRICH_MIN_BUDGET_S", "0")
    monkeypatch.setenv("DJYY_ENRICH_SEC_PER_MATCH", "0")
    import importlib
    mod = importlib.reload(mgr)
    try:
        class FakeFixture:
            def __init__(self, i):
                self.match_id = f"m{i}"
                self.home_team = f"H{i}"
                self.away_team = f"A{i}"

        class Slow(mgr.DJYYSource):
            def fetch_fixtures(self, target_date):
                return [FakeFixture(1)]

            def fetch_match_comparison(self, i):
                return {"model": {"p_home": .4, "p_draw": .3, "p_away": .3},
                        "bookmaker": {}}

        m = mod.SourceManager(tmp_path)
        m._djyy = Slow()
        mod.DJYY_ENRICH_MIN_BUDGET = -1.0   # 强制过期
        fixtures = [FakeFixture(1), FakeFixture(2)]
        m.enrich_from_djyy(fixtures, __import__("datetime").date(2026, 10, 12))
        status = json.loads((tmp_path / "djyy_status.json").read_text(encoding="utf-8"))
        assert status["budget_exhausted"] is True
        assert status["healthy"] is False
        assert "truncated_at" in status["detail"]
    finally:
        monkeypatch.undo()
        importlib.reload(mod)


def test_status_coverage_healthy_when_full(tmp_path):
    """全场覆盖时应判为 healthy，供 health-check workflow 消费。"""
    class FakeFixture:
        def __init__(self, i):
            self.match_id = f"m{i}"
            self.home_team = f"H{i}"
            self.away_team = f"A{i}"
            self._djyy_id = 1000 + i          # 匹配表靠它建，缺了就永远匹配不上

    class Ok(mgr.DJYYSource):
        def fetch_fixtures(self, target_date):
            return [FakeFixture(1), FakeFixture(2)]

        def fetch_match_comparison(self, i):
            return {"model": {"p_home": .4, "p_draw": .3, "p_away": .3},
                    "bookmaker": {}}
        def fetch_team_form(self, i, limit=5):
            return {"available": False}
        def fetch_match_info(self, i):
            return {"available": False}

    m = mgr.SourceManager(tmp_path)
    m._djyy = Ok()
    out = m.enrich_from_djyy([FakeFixture(1), FakeFixture(2)],
                             __import__("datetime").date(2026, 10, 12))
    assert len(out) == 2
    status = json.loads((tmp_path / "djyy_status.json").read_text(encoding="utf-8"))
    assert status["coverage"] == 1.0
    assert status["healthy"] is True
    assert status["detail"]["enriched"] == 2


# ---------- 阵容开关 ----------

def test_lineups_flag_default_off():
    """阵容是 store-only，默认关闭以省掉每场 1/4 的接口开销。"""
    assert mgr.DJYY_FETCH_LINEUPS is False


def test_lineups_flag_env(monkeypatch):
    monkeypatch.setenv("DJYY_FETCH_LINEUPS", "1")
    import importlib
    mod = importlib.reload(mgr)
    try:
        assert mod.DJYY_FETCH_LINEUPS is True
    finally:
        monkeypatch.undo()
        importlib.reload(mod)
