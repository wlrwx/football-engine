"""market_fair_method 观测标记与富化回填（2026-11-17，#102 草案 D）。

落盘 market_fair 在 2026-10-14 及以前是 proportional、之后是 Shin，历史记录没有
字段标明——此标记让新行自证口径。关键不变式：**方法与锚必须成对搬运**，
CI 回填 market_fair 时若不带 method（或相反），跨口径切换日的行又会失配。
"""
import importlib


def test_import_engine_main():
    importlib.import_module("engine.main")


def _main():
    return importlib.import_module("engine.main")


def test_enrich_fields_keeps_pair_together():
    m = _main()
    assert "market_fair" in m._ENRICH_FIELDS
    assert "market_fair_method" in m._ENRICH_FIELDS, "只搬锚不搬方法 = 口径又不可证"


def test_carry_backfills_missing_pair_only():
    m = _main()
    preds = [{"match_id": "a", "market_fair": None, "market_fair_method": None},
             {"match_id": "b", "market_fair": [0.5, 0.3, 0.2], "market_fair_method": "shin"}]
    prev = {"a": {"market_fair": [0.4, 0.35, 0.25], "market_fair_method": "shin"},
            "b": {"market_fair": [0.9, 0.05, 0.05], "market_fair_method": "proportional"}}
    n = m._carry_enrich_fields(preds, prev)
    assert n == 2, "a 缺的一对要回填"
    assert preds[0]["market_fair"] == [0.4, 0.35, 0.25]
    assert preds[0]["market_fair_method"] == "shin"
    # b 新值优先：有锚有方法时绝不覆盖（proportional 旧值不得污染 Shin 新行）
    assert preds[1]["market_fair"] == [0.5, 0.3, 0.2]
    assert preds[1]["market_fair_method"] == "shin"


def test_carry_ignores_unknown_match_and_other_fields():
    m = _main()
    preds = [{"match_id": "zzz"}]
    prev = {"other": {"market_fair": [1]}}
    assert m._carry_enrich_fields(preds, prev) == 0
    assert preds[0] == {"match_id": "zzz"}
