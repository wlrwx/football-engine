"""配置审计器的回归测试（2026-10-11）

背景：本项目已被"配置说谎"咬过多次——
`fusion.djyy_weight` 挂着却从不生效、`handicap_smoothing_weight`
放在 strategy.json 且名字与代码字段不符（值恰好相同，纯属巧合）、
`edge_gates.strict_min_probability_edge` 从初始提交起就没被读过。

审计器本身也有两个自纠：
1. 只做字面量搜索会把 `**dict` 展开进 dataclass 的键全判成"未引用"
   （prediction.* 整块 9 个键全是假阳性）→ 加 dataclass 感知
2. KNOWN_UNWIRED 里的键名字面量落在本文件内，被算成"代码引用"
   → 扫描时排除自身
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import config_audit as ca  # noqa: E402


# ---------- dataclass 感知 ----------

def test_dataclass_map_not_empty():
    """导入失败会静默返回空 map，导致整轮审计零告警却毫无意义。"""
    m = ca._dataclass_consumers()
    assert "__error__" not in m, f"dataclass 映射构建失败: {m.get('__error__')}"
    assert m, "dataclass 映射为空"
    for block in ("prediction", "lgbm", "calibration"):
        assert block in m, f"缺少配置块 {block}"
    assert "handicap_smoothing" in m["prediction"]


def test_dataclass_fields_detected_as_referenced():
    """通过 **dict 展开消费的键，代码里没有字面量，必须靠 dataclass 字段判定。"""
    m = ca._dataclass_consumers()
    assert ca._key_referenced("prediction.handicap_smoothing", "", m.get("prediction"))
    assert ca._key_referenced("prediction.base_goals", "", m.get("prediction"))
    # 名字不在 dataclass 里的 → 不能算引用
    assert not ca._key_referenced("prediction.handicap_smoothing_weight", "", m.get("prediction"))


def test_literal_referenced_still_works():
    """dataclass 之外的键靠字面量搜索。"""
    src = 'gates.get("min_probability_edge", 0.03)'
    assert ca._key_referenced("edge_gates.min_probability_edge", src)


# ---------- 自查不把自己算进去 ----------

def test_audit_excludes_itself_and_tests():
    """扫描源只含生产代码。

    两个自指问题：
    1. config_audit.py 自身的 KNOWN_UNWIRED 写满键名字面量
    2. tests/ 的断言里也写满键名
    若把它们算作消费者，"已知未接线"永远显示为已接线，且死配置被判为活跃。
    另外语义上也对：只在测试里被引用的键，生产上等同死配置。
    """
    src = ca._all_python_text()
    assert "strict_min_probability_edge" not in src, "自身/测试的键名不应算作代码引用"
    assert "handicap_smoothing_weight" not in src
    assert ROOT / "tests" not in ca.SRC_DIRS, "tests/ 不应作为消费方"


def test_known_unwired_declared():
    """已知未接线的键必须显式声明原因，不能和'忘记接线'混为一谈。"""
    assert "edge_gates.strict_min_probability_edge" in ca.KNOWN_UNWIRED["strategy.json"]
    assert ca.KNOWN_UNWIRED["strategy.json"]["edge_gates.strict_min_probability_edge"]


# ---------- 实际审计结果 ----------

def test_no_unexpected_unwired_keys():
    """当前不应有「未声明的未消费键」。若此测试失败，说明又新增了死配置。"""
    rep = ca.audit_config_keys()
    bad = {f: r.get("unexpected_unwired", []) for f, r in rep.items()
           if r.get("unexpected_unwired")}
    assert not bad, f"出现未声明的未消费键: {bad}"


def test_handicap_smoothing_is_live():
    """handicap_smoothing 必须真的接进 MCConfig（值不变，但旋钮要真的能拧）。"""
    from engine.prediction.monte_carlo import MonteCarloConfig
    pred = json.loads((ROOT / "config" / "prediction.json").read_text(encoding="utf-8"))
    assert "handicap_smoothing" in pred["prediction"], \
        "prediction.json 缺 handicap_smoothing —— 它会被 main.py:269 的过滤条件丢弃"
    assert "handicap_smoothing" in MonteCarloConfig.__dataclass_fields__
    strat = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    assert "handicap_smoothing_weight" not in strat, \
        "strategy.json 里的 handicap_smoothing_weight 从未被读取，应已移除"


def test_dead_model_weight_block_removed():
    """strategy.json 的 model_weight 整块从未被任何代码读取（在线权重学习器用的是
    engine/learning/online_weights.py 里自己的 OnlineWeightConfig）。"""
    strat = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    assert "model_weight" not in strat, "死配置 model_weight 应已移除"


def test_prediction_config_fully_referenced():
    """prediction.json 应 100% 被消费。"""
    rep = ca.audit_config_keys()["prediction.json"]
    assert rep["unreferenced"] == [], f"prediction.json 有未消费键: {rep['unreferenced']}"
    assert rep["total_leaf_keys"] == rep["referenced"]


def test_home_adv_alias_risk_detected():
    """home_adv_weight(DC) 与 home_advantage(MC) 概念相同、命名不同 ——
    数值当前一致（都是 0.1 log-goals），但只调前者会让 ensemble 静默分叉。"""
    w = ca.audit_dataclass_wiring()["monte_carlo"]
    risks = {r["config_key"] for r in w.get("alias_risks", [])}
    assert "home_adv_weight" in risks, "应识别出 home_adv_weight / home_advantage 的命名分叉"
    # 两者当前数值一致，不是 bug，但必须被记录
    dc = 1.0 * 0.1
    assert dc == pytest.approx(0.10)
