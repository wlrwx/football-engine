"""upgrade_tracker 记账口径回归测试（2026-10-11）

锁住四个曾经出错、且会误导自动 agent（PR#4 的 ACCEPT/REJECT 输入）的行为：

1. `v2_vs_market_same_period` 曾填入 **市场自己的分数**，键名却读起来像"v2 的分"。
2. `market_reference` 取自 legacy 子集，与 v2 子集**不是同一批比赛**，不可相减。
3. `baseline_pre_upgrade.n` 曾硬编码 426。
4. 从未做过 final vs market 的配对检验（Q2 缺失）。

另外锁住"有效权重"必须按归一化后计算，且 DJYY 是否生效由账本实测决定。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import upgrade_tracker as ut  # noqa: E402


# ---------- 配对检验 ----------

def _rec(final, market, actual=0, **kw):
    return {"final_prob": final, "market_fair": market, "actual_idx": actual,
            "date": "2026-01-01", "match_id": "x", **kw}


def test_paired_vs_market_reports_both_sides_not_just_one():
    """核心回归：配对结果必须同时给出 final 和 market 的分。
    旧版只把 market 的分塞进误导性键位，导致读者以为那是模型的成绩。"""
    recs = [_rec([0.7, 0.2, 0.1], [0.6, 0.2, 0.2], actual=0) for _ in range(40)]
    r = ut.paired_vs_market(recs)
    assert r["n"] == 40
    assert "brier_final" in r and "brier_market" in r
    assert r["brier_final"] != r["brier_market"]


def test_paired_vs_market_delta_sign_convention():
    """delta = final - market，**负 = 模型更好**。符号约定必须稳定。

    注意：配对差必须有方差，否则 se=0 → t=0 → 永远判成 TIED。
    所以这里让每场的强弱有变化（真实数据也是这样）。
    """
    # 模型稳定优于市场 → delta 应为负且显著
    good = []
    for i in range(80):
        m = 0.55 + 0.004 * (i % 7)          # 模型概率有抖动
        k = 0.34 + 0.003 * (i % 5)          # 市场概率有抖动
        good.append(_rec([m, 0.2, 1 - m - 0.2], [k, 0.33, 1 - k - 0.33], actual=0))
    r = ut.paired_vs_market(good)
    assert r["delta_brier"] < 0
    assert r["verdict"] == "BEATING"
    assert r["significant"] is True

    # 反之
    bad = []
    for i in range(80):
        m = 0.34 + 0.003 * (i % 5)
        k = 0.55 + 0.004 * (i % 7)
        bad.append(_rec([m, 0.33, 1 - m - 0.33], [k, 0.2, 1 - k - 0.2], actual=0))
    r2 = ut.paired_vs_market(bad)
    assert r2["delta_brier"] > 0
    assert r2["verdict"] == "LOSING"


def test_paired_vs_market_tied_when_identical():
    """完全相同的预测与市场 → 配对差全为 0 → TIED，不应被判成任何一边。"""
    same = [_rec([0.5, 0.3, 0.2], [0.5, 0.3, 0.2], actual=0) for _ in range(40)]
    r = ut.paired_vs_market(same)
    assert r["delta_brier"] == pytest.approx(0.0, abs=1e-9)
    assert r["verdict"] == "TIED"
    assert r["significant"] is False


def test_paired_vs_market_excludes_rows_without_market():
    """没有 market_fair 的记录不能进配对样本，否则会污染 n 和均值。"""
    recs = [_rec([0.7, 0.2, 0.1], [0.6, 0.2, 0.2]) for _ in range(30)]
    recs += [_rec([0.7, 0.2, 0.1], None) for _ in range(10)]
    for r in recs[30:]:
        r["market_fair"] = None
    out = ut.paired_vs_market(recs)
    assert out["n"] == 30


def test_paired_vs_market_reports_ci_and_per_match():
    """必须带 CI 和逐场胜负，否则无法判断差异大小。"""
    recs = []
    for i in range(50):
        good = i % 2 == 0
        if good:
            recs.append(_rec([0.9, 0.05, 0.05], [0.4, 0.35, 0.25], actual=0))
        else:
            recs.append(_rec([0.35, 0.3, 0.35], [0.5, 0.3, 0.2], actual=0))
    r = ut.paired_vs_market(recs)
    assert r["matches_better"] + r["matches_worse"] == 50
    assert len(r["ci95_delta"]) == 2


# ---------- 有效权重 ----------

def test_effective_weights_renormalize_when_djyy_dead(tmp_path, monkeypatch):
    """DJYY 数据源挂掉时，名义 0.2 是死权重，真实比例应是 model/market 归一化后的值。

    实测：champion 0.05/0.75/0.20，djyy 命中率 0 →
    model 0.05/0.80 = 6.25%, market 0.75/0.80 = 93.75%
    """
    fw = tmp_path / "fusion_weights.json"
    fw.write_text(json.dumps({"champion": {"model": 0.05, "market": 0.75, "djyy": 0.2}}))
    monkeypatch.setattr(ut, "FUSION_W", fw)
    monkeypatch.setattr(ut, "PRED_CFG", tmp_path / "nonexistent.json")

    dead = [{"djyy_prob": None} for _ in range(100)]
    out = ut.effective_weights(dead)
    eff = out["effective"]
    assert eff["djyy"] == 0.0
    assert eff["model"] == pytest.approx(0.0625, abs=1e-4)
    assert eff["market"] == pytest.approx(0.9375, abs=1e-4)
    assert out["djyy_hit_rate"] == 0.0
    assert out["note"], "死权重必须给出告警，不能静默"


def test_effective_weights_djyy_live_keeps_three_way(tmp_path, monkeypatch):
    """DJYY 正常供数时，三路权重原样生效。"""
    fw = tmp_path / "fusion_weights.json"
    fw.write_text(json.dumps({"champion": {"model": 0.05, "market": 0.75, "djyy": 0.2}}))
    monkeypatch.setattr(ut, "FUSION_W", fw)
    monkeypatch.setattr(ut, "PRED_CFG", tmp_path / "nonexistent.json")

    live = [{"djyy_prob": {"home": .5, "draw": .3, "away": .2}} for _ in range(100)]
    out = ut.effective_weights(live)
    eff = out["effective"]
    assert eff["model"] == pytest.approx(0.05, abs=1e-4)
    assert eff["market"] == pytest.approx(0.75, abs=1e-4)
    assert eff["djyy"] == pytest.approx(0.20, abs=1e-4)


# ---------- 两个判定必须独立 ----------

def test_q1_and_q2_verdicts_are_separate_fields():
    """'IMPROVED' 只描述 vs 旧链（工程回归），不能暗示系统有 edge。
    市场判定必须有自己独立的字段。"""
    src = (ROOT / "scripts" / "upgrade_tracker.py").read_text(encoding="utf-8")
    assert "verdict_vs_market" in src, "必须存在独立的市场判定字段"
    assert "paired_vs_market" in src, "必须存在配对的市场检验"
    # 误导性键位必须已移除
    assert '"v2_vs_market_same_period": mkt' not in src


def test_market_reference_labeled_as_different_subset():
    """market_reference 来自 legacy 子集，必须显式标注不可与 v2 相减。"""
    src = (ROOT / "scripts" / "upgrade_tracker.py").read_text(encoding="utf-8")
    assert "market_reference_frozen" in src
    assert "不可与" in src or "不可" in src


def test_baseline_n_not_hardcoded():
    """旧版硬编码 n=426。这里断言源码里不再出现该字面量赋值。"""
    src = (ROOT / "scripts" / "upgrade_tracker.py").read_text(encoding="utf-8")
    assert '"n": ref and 426' not in src
    assert "ref and 426" not in src
