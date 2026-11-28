#!/usr/bin/env python3
"""`scripts/train_serve_align_audit.py` 的测试（只读，不改任何文件）。

    .venv/bin/python -m pytest tests/test_train_serve_align_audit.py -q

写法沿用 tests/test_leak_audit.py：**断言判定结论与判定规则，不断言计数**。
命中数、行数、覆盖率只出现在 `counts_for_humans` 里；样本变多、代码挪行、
名单加字段都不会让 CI 变红。

和原审计脚本不同的是：本审计**确实找到了缺陷**（闸门空转、别名表断链、
死列接线失效等），所以这里没有「当前 0 violation」这种总括断言，而是

1. 对**安全性质**（目标市场隔离、赛后信息不敏感、市场不掺模型腿）断言 clean；
2. 对**缺陷判定**断言「检测器判对了 + 实害为 0」，并且给出正对照（canary），
   证明检测器不是空判——修好缺陷后测试仍然绿（判定翻转即被规则允许），
   但把缺陷藏起来（例如把检测器改成永远返回 clean）会红。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "train_serve_align_audit", ROOT / "scripts" / "train_serve_align_audit.py")
audit = importlib.util.module_from_spec(SPEC)
sys.modules["train_serve_align_audit"] = audit
SPEC.loader.exec_module(audit)

EXPECTED_IDS = {
    "matrix_target_market_isolation",
    "matrix_time_point_provenance",
    "matrix_post_match_insensitivity",
    "matrix_price_field_insensitivity",
    "side_snapshot_asof_gate",
    "side_index_pair_key",
    "matrix_dead_columns",
    "matrix_handicap_line_provenance",
    "serve_snapshot_quantization",
    "booster_gain_attribution",
    "devig_caliber_duplication",
    "devig_method_family_divergence",
    "league_name_caliber_duplication",
    "market_blend_weight_duplication",
    "side_feature_name_parity",
}


@pytest.fixture(scope="module")
def report() -> dict:
    return audit.build_report()


@pytest.fixture(scope="module")
def records():
    return audit.load_settled_records()


@pytest.fixture(scope="module")
def matrix(records):
    return audit.rebuild_matrix(records)


def _finding(report: dict, fid: str) -> dict:
    return next(f for f in report["findings"] if f["id"] == fid)


# ------------------------------------------------------------------ 报告骨架
def test_report_covers_the_audited_questions(report):
    assert {f["id"] for f in report["findings"]} == EXPECTED_IDS
    for f in report["findings"]:
        assert f["title"] and f["verdict"]
        assert f["status"] in {"clean", "by_design", "violation"}
        assert f["severity"] in audit.SEVERITY_ORDER
        assert f["note"]


def test_sample_size_matches_the_ledger_axes(report, records, matrix):
    # 账本行 → 训练行必须同向，且训练行不得多于账本行（不得凭空造样本）
    assert report["sample"]["ledger_rows"] == len(records)
    assert 0 < len(matrix) <= len(records)


def test_report_is_json_serialisable_and_read_only(report):
    ledger = ROOT / "data" / "state" / "review_ledger.jsonl"
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    code_before = {p: p.stat().st_mtime_ns for p in (ROOT / "engine").rglob("*.py")}
    text = json.dumps(report, ensure_ascii=False, default=str)
    assert json.loads(text)["findings"]
    after = hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert before == after
    assert code_before == {p: p.stat().st_mtime_ns for p in (ROOT / "engine").rglob("*.py")}


# ---------------------------------------------------------- 安全性质（必须 clean）
def test_target_market_columns_are_inert_in_real_matrix(report, matrix):
    from engine.prediction.lgbm_model import FEATURE_NAMES
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    f = _finding(report, "matrix_target_market_isolation")
    assert f["verdict"] == "inert"
    assert f["status"] == "clean"
    assert f["live_blacklisted_columns"] == []
    # 独立复算，不依赖脚本的自我结论
    banned = [n for n in FEATURE_NAMES if n in TARGET_MARKET_FEATURES]
    assert banned, "名单里至少要有一个特征名，否则本测试为空判"
    for r in matrix:
        for n in banned:
            assert float(r["features"].get(n, 0.0)) == 0.0


def test_matrix_does_not_consume_settlement_fields(report, matrix):
    f = _finding(report, "matrix_post_match_insensitivity")
    assert f["verdict"] == "insensitive"
    assert f["mutated_columns"] == []
    assert f["counts_for_humans"]["sentinel_assignments"] > 0  # 确实改到了字段


def test_matrix_does_not_consume_target_market_prices(report):
    f = _finding(report, "matrix_price_field_insensitivity")
    assert f["verdict"] == "insensitive"
    assert f["changed_columns"] == []
    assert "home_odds" in f["price_keys"]


def test_perturbing_a_live_input_does_move_the_matrix(records, matrix):
    """正对照：同一套机制改 handicap（训练侧真读的字段）必须报出变化。

    没有这条，上面两个「不敏感」判定可能只是重建路径根本没跑起来。
    """
    def mutate(p: dict) -> None:
        p["handicap"] = (p.get("handicap") or 0.0) + 1.0

    flipped = audit.diff_columns(
        matrix, audit.rebuild_with(mutate, records), columns=["handicap"])
    assert flipped, "改 handicap 后特征逐位不变 = 重建/比对机制失效，判定不可信"


def test_side_features_exclude_target_market_site_price(report):
    f = _finding(report, "side_feature_name_parity")
    assert f["verdict"] == "names_match_and_site_price_excluded"
    from engine.sources import njstats_side as side
    sig = side.SideSignal(
        home="A", away="B", kickoff="2026-01-01 00:00", snapshot_at="2026-01-01 00:00",
        open_fair=(0.5, 0.3, 0.2), site_fair=(0.9, 0.05, 0.05), home_ppg_home=3.0,
        away_ppg_away=0.0)
    feats = sig.to_features()
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    assert not (set(feats) & TARGET_MARKET_FEATURES)
    assert all(0.0 <= v <= 1.0 for k, v in feats.items() if k.startswith("side_open_"))


def test_market_blend_stays_zero_and_league_layer_unwired(report):
    """市场概率掺进模型腿的那一步：活值必须为 0，联赛级 getter 必须无人调用。

    这是目标市场赔率进入概率形成的第二条通道（不经过特征名），
    `market_isolation` 的名字黑名单看不见，所以只能这样钉住。
    """
    f = _finding(report, "market_blend_weight_duplication")
    assert f["live_config_value"] == 0.0, (
        "config/prediction.json[\"prediction\"][\"market_blend_weight\"] 一旦 > 0，"
        "竞彩赔率就按该权重掺进模型腿 —— 属改判据/改特征口径，必须人工登记后做")
    assert f["unwired_getter_callers"] == [], (
        "联赛级 get_market_blend/get_base_goals/get_home_adv 是 E5 量化后有意未接线的，"
        "接上等于把市场权重从 0.0 抬到 ≤0.5")
    assert f["verdict"] == "live_value_zero_and_league_layer_unwired"
    assert f["status"] != "violation"


def test_blend_scanner_finds_a_wired_getter_call():
    """接线守卫的正对照：接了 getter / 读了权重的源码必须被抓到。"""
    src = ("def build(cfg, mgr):\n"
           "    w = mgr.get_market_blend('英超')\n"
           "    return cfg.market_blend_weight\n")
    reads, calls = audit.scan_blend_consumers(src)
    assert calls == [2] and reads == [3]
    # 赋值不是读取，不该报
    assert audit.scan_blend_consumers("cfg.market_blend_weight = 0.5\n") == ([], [])


def test_booster_never_splits_on_disabled_target_market_columns(report):
    """重要性与 config 开关的一致性（无 lightgbm 时脚本自行 skip）。"""
    f = _finding(report, "booster_gain_attribution")
    if f["verdict"].startswith("skipped"):
        pytest.skip(f["note"])
    from engine.prediction.lgbm_model import FEATURE_NAMES
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    cfg = json.loads((ROOT / "config" / "prediction.json").read_text(encoding="utf-8"))
    if not cfg["lgbm"].get("use_odds_features", False):
        expected = TARGET_MARKET_FEATURES.intersection(FEATURE_NAMES)
        zero_gain = set(f["zero_gain_columns"])
        assert expected, "模型应包含若干目标市场派生列，才能验证闸门生效"
        assert expected <= zero_gain, (
            f"目标市场闸门关闭时这些列仍发生分裂：{sorted(expected - zero_gain)}"
        )


def test_booster_is_loaded_in_a_subprocess_with_model_str(report):
    """回归锁：绝不在测试进程里直接 `Booster(model_file=…)`。

    2026-11-16 的 `test (windows-latest)` CI：lightgbm 在原生层
    `Fatal Python error: Aborted`（basic.py:3800），Python 侧抓不住，
    整个 pytest 会话被打死（ubuntu 全绿，只有 windows 红）。所以 booster
    只能由子进程加载（崩了只降级成 skipped），且模型文本走
    `model_str=`（`read_text()` 已把 CRLF 归一成 LF）。
    """
    import ast as _ast
    src = audit.SELF.read_text(encoding="utf-8")
    tree = _ast.parse(src)
    booster_calls = [n for n in _ast.walk(tree) if isinstance(n, _ast.Call)
                     and getattr(n.func, "attr", "") == "Booster"]
    assert booster_calls, "本判定应该加载 booster（换了实现就同步改这条锁）"
    bad = [n.lineno for n in booster_calls
           if any(k.arg == "model_file" for k in n.keywords)]
    assert not bad, f"第 {bad} 行用 model_file= 直接读盘：Windows 上会原生 Abort"
    assert any(any(k.arg == "model_str" for k in n.keywords) for n in booster_calls)
    assert "subprocess.run" in src
    f = _finding(report, "booster_gain_attribution")
    if not f["verdict"].startswith("skipped"):
        assert f.get("isolated") is True, "booster 判定必须是子进程算出来的"


def test_booster_failure_degrades_to_skip_not_crash():
    """子进程失败必须降级成 skipped 判定，不能把审计报告整个搞挂。"""
    real = audit.subprocess.run
    try:
        audit.subprocess.run = lambda *a, **k: __import__("subprocess").CompletedProcess(
            a[0], 134, "", "Fatal Python error: Aborted")
        f = audit.audit_booster_sensitivity()
    finally:
        audit.subprocess.run = real
    assert f["verdict"].startswith("skipped_")
    assert f["status"] == "clean" and f["severity"] == "info"
    assert "134" in f["verdict"] or "Aborted" in f["verdict"]
    assert f["id"] == "booster_gain_attribution"


# ------------------------------------------------------------ 缺陷判定 + 反空判
def test_side_gate_verdict_matches_the_actual_parser(report, records):
    """闸门判定必须与生产 `_parse_ts` 的真实行为一致（改好解析器即自动翻绿）。"""
    from engine.sources import njstats_side as side
    f = _finding(report, "side_snapshot_asof_gate")
    samples = [str(p.get("as_of")) for d in audit.pred_dates(records)
               for p in audit.load_predictions(d) if p.get("as_of")]
    parsed = sum(1 for v in samples if side._parse_ts(v) is not None)
    fmt = f["asof_format"]
    if fmt["samples"] and fmt["parsed_by_production_gate"] == 0:
        # 脚本只抽尾部 40 天，判"死"就必须与全量一致（否则是抽样错觉）
        assert f["verdict"] == "gate_dead"
        assert parsed == 0, "全量里有能解析的 as_of，脚本却据尾部样本判死"
    elif parsed:
        assert f["verdict"] == "gate_live"
    # 无论闸门死活，实际偷看到未来快照的训练行必须为 0
    assert f["future_snapshot_rows_detected"] == []
    assert f["counts_for_humans"]["would_block_with_strict_gate"] == 0, (
        "出现非 0 = 训练行真拿到了晚于当时预测时刻的侧挂快照，是真·未来信息泄漏")


def test_features_as_of_blocks_later_snapshot_when_format_is_supported():
    """闸门本身的设计意图：快照晚于预测时刻 ⇒ 不给特征（用解析器认得的格式）。"""
    from engine.sources import njstats_side as side
    sig = side.SideSignal(
        home="A", away="B", kickoff="2026-01-02 00:00", snapshot_at="2026-01-01 12:00",
        open_fair=(0.5, 0.3, 0.2), home_ppg_home=2.0, away_ppg_away=1.0)
    assert sig.features_as_of("2026-01-01 06:00:00") is None      # 快照在未来 → 拦
    assert sig.features_as_of("2026-01-01 18:00:00") is not None  # 快照在过去 → 放行


def test_time_point_classifier_buckets(records):
    """合成小样本喂给时点判定：开赛后/缺时间戳必须分进对应桶（防空判）。"""
    rows = [{"match_id": f"m{i}", "features": {}} for i in range(4)]
    recs = [
        {"match_id": "m0", "as_of": "2026-01-01T10:00:00+08:00",
         "kickoff": "2026-01-01 15:00:00"},          # 赛前
        {"match_id": "m1", "as_of": "2026-01-01T20:00:00+08:00",
         "kickoff": "2026-01-01 15:00:00"},          # 开赛后 5 小时
        {"match_id": "m2", "as_of": "", "kickoff": "2026-01-01 15:00:00"},
        {"match_id": "m3", "as_of": "", "kickoff": ""},
    ]
    out = audit.audit_time_points(rows, recs)
    assert out["buckets"] == {"before_kickoff": 1, "after_kickoff": 1, "no_asof": 1,
                              "no_kickoff": 0, "no_asof_or_kickoff": 1}
    assert out["status"] == "violation"
    assert [r["match_id"] for r in out["post_kickoff_rows"]] == ["m1"]
    assert out["counts_for_humans"]["unverifiable"] == 2


def test_parse_ts_any_handles_both_house_formats():
    a = audit.parse_ts_any("2026-01-01T10:00:00+08:00")
    b = audit.parse_ts_any("2026-01-01 10:00:00")
    assert a is not None and a == b
    assert audit.parse_ts_any("") is None
    assert audit.parse_ts_any(None) is None


def test_real_matrix_time_points_are_reported(report):
    f = _finding(report, "matrix_time_point_provenance")
    b = f["buckets"]
    assert b["before_kickoff"] > 0
    # 开赛后的行只能来自 kickoff 缺失或空串绕过（engine/main.py:147-154 的 `if _ko:`）
    for r in f["post_kickoff_rows"]:
        assert r["minutes_after_kickoff"] > 0
    assert f["counts_for_humans"]["rows"] == sum(b.values())


def test_pair_key_collision_is_a_property_of_the_index_key(report):
    """归并键判定与 `side.key()` 的实际定义必须一致（防「计数偶然为 0」式空判）。"""
    from engine.sources import njstats_side as side
    f = _finding(report, "side_index_pair_key")
    k = side.key("甲", "乙")
    assert len(k) == 2 and k == ("甲", "乙"), "归并键若已含开赛信息，本判定应随之改写"
    assert f["counts_for_humans"]["index_slots"] > 0


# ---------------------------------------------------------------- devig 口径
def test_all_devig_implementations_agree_on_real_odds(report):
    """7 份 devig 的一致性不是注释，是可测的性质。"""
    f = _finding(report, "devig_caliber_duplication")
    assert f["max_abs_difference"] < 1e-9
    assert f["counts_for_humans"]["triples_tested"] > 1000


def test_devig_outputs_are_probabilities():
    for name, fn in audit.devig_impls().items():
        got = fn((2.0, 3.5, 4.2))
        vals = list(got.values()) if isinstance(got, dict) else list(got)
        assert abs(sum(float(v) for v in vals) - 1.0) < 1e-9, name


def test_guard_probe_detects_a_diverging_implementation():
    """守卫探测的正对照：故意放一份「不设防」的假实现进去，必须报出 2 种签名。"""
    impls = {"guarded": lambda t: None if min(t) <= 1.0 else
             tuple(x / sum(1 / y for y in t) for x in t),
             "unguarded": lambda t: tuple(1 / (t[0], t[1], t[2]))}
    sigs = audit.probe_guards(impls)
    assert len({tuple(v.values()) for v in sigs.values()}) == 2
    assert sigs["guarded"]["odds_1.00"] == "none"


def test_max_disagreement_helper_is_not_vacuous():
    """比对器的正/负对照：同口径 ⇒ 0；换成「忘记归一」的口径 ⇒ 明显 > 0。

    没有这条，`max_abs_difference < 1e-9` 也可能只是因为两边根本没比上。
    """
    triples = [("x", (2.0, 3.5, 4.2)), ("y", (1.8, 3.4, 4.6)), ("z", (5.0, 4.0, 1.7))]
    devig = audit.devig_impls()["engine/sources/njstats_side.py::_devig"]
    unnormalized = lambda t: tuple(1.0 / o for o in t)   # noqa: E731 - 另一种（错）口径
    assert audit.max_disagreement({"a": devig, "b": devig}, triples) == 0.0
    assert audit.max_disagreement({"a": devig, "b": unnormalized}, triples) > 0.01


def test_same_object_devig_calibers_differ_but_not_in_the_metric(report):
    """**同一对象（竞彩 HAD）**上的两份去水确实不等价：落盘锚 Shin vs 比例去水。

    规则：概率空间必须看得见差；若这个差在配对 Brier 上显著（|t| ≥ 2），
    判定就必须是 violation；测不出显著差时也不得把结论写成「口径无关」。
    注意本判定刻意不碰「回放 vs 服务」那组比较——那两边对象不同
    （回放基准是参考盘收盘），已由
    `docs/proposals/dc-market-blend-and-devig-review.md` 裁定不构成同对象漂移。
    """
    f = _finding(report, "devig_method_family_divergence")
    assert f["per_leg_abs_delta"]["mean"] > 1e-4, "两份口径若真相同，本判定应改成 by_design"
    st = f["paired_brier"]
    assert st["n"] > 500
    if abs(st["t"]) >= 2.0:
        assert f["status"] == "violation"
    else:
        assert "below" in f["verdict"] or "switched" in f["verdict"]


def test_paired_brier_helper_is_not_vacuous():
    """配对统计的正对照：喂一份明显更差的口径，必须报出显著差。"""
    pairs = []
    for i in range(400):
        label = i % 3
        q = 0.55 + 0.01 * (i % 30)          # 让逐场强度有变化，SE 才不为 0
        good = [q if k == label else (1 - q) / 2 for k in range(3)]
        pairs.append((good, [1 / 3, 1 / 3, 1 / 3], label))
    out = audit.paired_brier_shift(pairs)
    assert out["n"] == 400 and out["mean_delta"] < 0 and abs(out["t"]) > 5


def test_caliber_switch_rule_canary():
    """换口径判定的规则对照：干净切换⇒抓到；交错⇒不得报切换。"""
    switched = audit.caliber_switch_days({
        "2026-10-14": {"shin": 0, "proportional": 9, "other": 0},
        "2026-10-13": {"shin": 1, "proportional": 8, "other": 0},
        "2026-10-15": {"shin": 7, "proportional": 0, "other": 0},
        "2026-10-16": {"shin": 6, "proportional": 1, "other": 0}})
    assert switched["switched"] and switched["last_proportional_day"] == "2026-10-14"
    assert switched["first_shin_day"] == "2026-10-15"
    interleaved = audit.caliber_switch_days({
        "2026-10-14": {"shin": 5, "proportional": 1, "other": 0},
        "2026-10-15": {"shin": 0, "proportional": 6, "other": 0}})
    assert not interleaved["switched"]
    single = audit.caliber_switch_days({"2026-10-15": {"shin": 3, "proportional": 0, "other": 1}})
    assert not single["switched"] and single["last_proportional_day"] is None


def test_anchor_caliber_switch_is_recomputable_from_the_census(report, records):
    """「落盘锚中途换口径」必须能从逐日普查复算出来，不是脚本一句话结论。"""
    f = _finding(report, "devig_method_family_divergence")
    cs = f["caliber_switch"]
    census = cs["daily_census"]
    assert cs == {**cs, **{k: v for k, v in
                           audit.caliber_switch_days(census).items()
                           if k in ("last_proportional_day", "first_shin_day",
                                    "mixed_caliber_days")}}
    if cs["last_proportional_day"] and cs["first_shin_day"]:
        assert cs["last_proportional_day"] < cs["first_shin_day"]
        for day, v in census.items():
            if day <= cs["last_proportional_day"]:
                assert v["proportional"] >= v["shin"], day
            if day >= cs["first_shin_day"]:
                assert v["shin"] >= v["proportional"], day
        # 落盘锚没有任何口径标记字段 → 只能靠复算识别（这条断言防"加了标记还吹"）
        sample = audit.load_predictions(sorted(census)[0]) if census else []
        marked = [k for k in (sample[0] if sample else {})
                  if "devig" in k.lower() or "caliber" in k.lower()]
        assert marked == []
    else:
        assert f["verdict"] != "anchor_caliber_switched_mid_sample"
    # 普查桶必须能用生产函数独立复算（防「分类器坏掉 ⇒ 一切正常」的假绿）
    from engine.prediction.calibration import devig_multiplicative, select_devig_method
    ok_days = sorted(k for k, v in census.items() if sum(v.values()) >= 3)
    # 首尾都取：中途换口径的特征只在尾部显现，只查开头会漏（MUT 实验证实过）
    days = [ok_days[0], ok_days[-1]] if len(ok_days) > 1 else ok_days
    assert days, "普查里至少要有两天可复算"
    for day in days:
        recs_day = [r for r in records if str(r.get("date", "")) == day]
        recomputed = {"shin": 0, "proportional": 0, "other": 0}
        for r in recs_day:
            mf = r.get("market_fair")
            p = next((x for x in audit.load_predictions(day)
                      if x.get("match_id") == r.get("match_id")), None)
            t3 = (p or {}).get("home_odds"), (p or {}).get("draw_odds"), (p or {}).get("away_odds")
            if not mf or not all(isinstance(x, (int, float)) and x > 1.0 for x in t3):
                continue
            sh = [float(x) for x in select_devig_method(list(t3))]
            pr = [float(x) for x in devig_multiplicative(list(t3))]
            if max(abs(float(a) - b) for a, b in zip(mf, sh)) <= 1e-4:
                recomputed["shin"] += 1
            elif max(abs(float(a) - b) for a, b in zip(mf, pr)) <= 1e-4:
                recomputed["proportional"] += 1
            else:
                recomputed["other"] += 1
        assert recomputed == census[day], day


# ------------------------------------------------------------ 联赛名口径
def test_league_name_disagreements_are_recomputable(report):
    """两张表的分歧必须能用两张表本身复算出来（判定不依赖脚本内部状态）。"""
    from engine.learning.freshness import LEAGUE_NAME_MAP
    from engine.pipeline.helpers import _canon_league
    f = _finding(report, "league_name_caliber_duplication")
    recomputed = {d["raw"] for d in f["disagreeing_names"]}
    for name in recomputed:
        assert _canon_league(name) != LEAGUE_NAME_MAP.get(name, name)
    for name, c1, c2 in [(d["raw"], d["canon_league"], d["name_map"])
                         for d in f["disagreeing_names"]]:
        assert _canon_league(name) == c1 and LEAGUE_NAME_MAP.get(name, name) == c2
    # 断链的定义：LEAGUE_ALIASES 覆盖而先验查不到 → 落回默认值
    from engine.prediction.league_stats import DEFAULT_AVG_GOALS, league_stats
    for alias in f["alias_keys_that_break_prior_lookup"]:
        assert league_stats(alias)[0] == DEFAULT_AVG_GOALS
        assert league_stats(_canon_league(alias))[0] != DEFAULT_AVG_GOALS


def test_default_prior_share_is_reported_for_humans(report, matrix):
    """联赛先验落默认值的比例只报数，不写死阈值（矩阵会随样本变化）。"""
    f = _finding(report, "matrix_dead_columns")
    share = f["counts_for_humans"]["league_avg_goals_at_default"] / len(matrix)
    assert 0.0 <= share <= 1.0
    assert f["dead_by_gate"], "odds_* 四列必须被判为「闸门关死」而不是「接线失效」"
    assert set(f["dead_by_gate"]) <= set(audit.columns_stats(matrix))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
