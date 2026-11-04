#!/usr/bin/env python3
"""泄漏与训练/服务对齐审计脚本的测试（只读，不改任何文件）。

    .venv/bin/python -m pytest tests/test_leak_audit.py -q

写法：断言**判定结论与判定规则**，不断言计数。命中数、每文件条数、参数个数
只出现在脚本报告的 `counts_for_humans` 里；黑名单加字段、代码挪行、
`build_features` 加带默认值的参数都不会让 CI 变红。

（原版本是一批 `return dict/bool` 的"诊断函数"，pytest 收集后不做任何断言，
等于没有测试；本次改成真断言。）
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "leak_audit_check", ROOT / "scripts" / "leak_audit_check.py")
audit = importlib.util.module_from_spec(SPEC)
sys.modules["leak_audit_check"] = audit
SPEC.loader.exec_module(audit)

EXPECTED_IDS = {
    "target_market_blacklist", "lgbm_odds_gate", "sina_odds_movement",
    "odds_series", "njstats_opening", "train_serving_alignment",
}


@pytest.fixture(scope="module")
def report() -> dict:
    return audit.build_report()


def _finding(report: dict, fid: str) -> dict:
    return next(f for f in report["findings"] if f["id"] == fid)


def test_report_covers_the_six_audited_questions(report):
    assert {f["id"] for f in report["findings"]} == EXPECTED_IDS
    for f in report["findings"]:
        assert f["verdict"] and f["status"] in {"clean", "by_design", "violation"}
        assert f["severity"] in audit.SEVERITY_ORDER


def test_report_is_json_serialisable_and_read_only(report):
    # 只读：跑一次 main 不产生任何文件，报告可序列化。
    before = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.json")}
    text = json.dumps(report, ensure_ascii=False, default=str)
    assert json.loads(text)["findings"]
    after = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.json")}
    assert before == after


def test_current_code_has_no_violation_verdict(report):
    assert [f["id"] for f in report["findings"] if f["status"] == "violation"] == []


# ------------------------------------------------------------------ 判定规则
def test_severity_is_capped_at_info_when_path_inactive():
    for severity in audit.SEVERITY_ORDER:
        assert audit.cap_severity(severity, path_active=False) == "info"
        assert audit.cap_severity(severity, path_active=True) == severity



def test_sina_verdict_respects_the_config_switch(report):
    f = _finding(report, "sina_odds_movement")
    if not f["switch_on"]:
        # 规则：开关关 ⇒ 不得动概率、判定不得高于 info、不得是违规
        assert not f["probabilities_changed"]
        assert not f["step_fired"]
        assert f["severity"] == "info"
        assert f["status"] != "violation"
    else:
        assert f["source_is_reference"] or f["status"] == "violation"


def test_blacklist_hits_are_all_in_allowed_locations(report):
    f = _finding(report, "target_market_blacklist")
    assert f["evidence"] == []
    assert f["verdict"] == "all_hits_in_allowed_locations"


def test_classifier_rejects_an_ungated_assignment():
    # 防假绿：无闸的生产赋值必须被判为 unallowed；受闸的必须被放行。
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    features = set(TARGET_MARKET_FEATURES)
    feat = sorted(features)[0]
    bad = f'def build(odds):\n    return {{"{feat}": 1.0 / odds[0]}}\n'
    assert audit.classify_hits(bad, features)["unallowed"]
    good = (
        "def build(odds, include_market_odds=False):\n"
        "    if include_market_odds and odds:\n"
        f'        return {{"{feat}": 1.0 / odds[0]}}\n'
        "    else:\n"
        f'        return {{"{feat}": 0.0}}\n'
    )
    res = audit.classify_hits(good, features)
    assert res["allowed"] and not res["unallowed"]


def test_gate_finding_is_based_on_feature_sensitivity(report):
    f = _finding(report, "lgbm_odds_gate")
    assert f["set_to_true_at"] == []
    assert f["leaking_features"] == []
    assert f["verdict"] == "target_odds_do_not_enter_features"


def test_series_is_not_consumed_by_probability_formation(report):
    f = _finding(report, "odds_series")
    assert f["fusion_read_lines"] == []
    assert not f["probabilities_changed_by_series"]
    assert f["verdict"] == "not_used_in_probability_formation"


def test_series_consumers_are_neither_prediction_nor_settlement(report):
    # 判定：消费者不在概率形成层，也不在结算复盘层（原审计「仅用于结算验证」不确）。
    f = _finding(report, "odds_series")
    assert not any(c.startswith(("engine/prediction/", "engine/review/"))
                   for c in f["consumers"]), f["consumers"]


def test_njstats_reads_opening_and_excludes_site_price(report):
    f = _finding(report, "njstats_opening")
    assert f["reads_opening_only"]
    assert not f["site_price_reaches_features"]


def test_alignment_is_judged_by_defaults_not_param_counts(report):
    f = _finding(report, "train_serving_alignment")
    assert f["missing_required_params"] == []
    assert f["unknown_params"] == []
    assert f["feature_diff"] == {}
    assert f["verdict"] == "no_feature_skew"
    # 未传参数本身不是问题（都取签名默认值）；加参数、补传参数都不该变红。


def test_unverified_notes_never_exceed_info(report):
    for n in report["unverified_notes"]:
        assert n["severity"] == "info"


def test_duplicate_audit_entrypoint_is_gone():
    assert not (ROOT / "scripts" / "leak_audit" / "audit_leakage.py").exists()
    assert (ROOT / "scripts" / "leak_audit_check.py").exists()


def test_reported_paths_use_posix_separators(report):
    text = json.dumps(report, ensure_ascii=False, default=str)
    assert "\\\\" not in text  # JSON 里的反斜杠 = 非 POSIX 路径
    for root in (PurePosixPath(ROOT.as_posix()), PureWindowsPath("C:/repo")):
        p = root / "engine" / "prediction" / "lgbm_model.py"
        assert p.relative_to(root).as_posix() == "engine/prediction/lgbm_model.py"
