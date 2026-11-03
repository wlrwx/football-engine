"""确定性运维摘要（2026-11-03）：把巡检/对账从模型自动化里搬出来。

重点覆盖今天真实踩到的两种「零合格」：匹配5/最高-0.71% 是正常的价差不足，
匹配0 是数据洞（CI 回仓被拦时就是这个症状）。两者必须区分，否则要么天天误报、
要么把真故障当正常。
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("ops_digest", ROOT / "scripts" / "ops_digest.py")
ops = importlib.util.module_from_spec(SPEC)
sys.modules["ops_digest"] = ops
SPEC.loader.exec_module(ops)

BJT = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 3, 11, 40, tzinfo=BJT)


def loglines(bodies, start=None):
    """每个 body 造一轮：先一条 round 标记（真实 duty 每轮恰好一条），再一条内容。"""
    start = start or (NOW - timedelta(hours=23)).replace(minute=5, second=0)
    out = []
    for i, body in enumerate(bodies):
        ts = start + timedelta(hours=i // 2, minutes=30 * (i % 2))
        if not ops.ROUND_MARK_RE.search(body):
            out.append(ts.strftime("%m-%d %H:%M:%S ") + "mint(新)")
        out.append((ts + timedelta(seconds=1)).strftime("%m-%d %H:%M:%S ") + body)
    return "\n".join(out)


def rows_of(text):
    return ops.recent(ops.parse_log(text), NOW)


def test_full_48_rounds_has_no_missing_hour():
    text = loglines(["1x2: challenge: 零合格（池196/匹配5/候选5/最高-0.71%）——正常"] * 48)
    s = ops.round_summary(rows_of(text), NOW)
    assert s["rounds"] == 48 and s["missing_count"] == 0, s


def test_missing_hours_are_counted():
    text = loglines(["已推: duty: 快照"] * 44)          # 少 4 条 = 2 个整小时
    s = ops.round_summary(rows_of(text), NOW)
    assert s["missing_count"] == 2, s


def test_error_resolved_when_a_later_round_succeeded():
    text = loglines(["mint 失败: 401"] * 4 + ["1x2: challenge: 零合格（池1/匹配1/候选1/最高-1%）"] * 44)
    e = ops.error_summary(rows_of(text))
    assert e["mint"]["resolved"] is True


def test_unresolved_error_is_flagged():
    text = loglines(["1x2: challenge: 零合格（池1/匹配1/候选1/最高-1%）"] * 44 + ["需人工登录"] * 4)
    rows = rows_of(text)
    e = ops.error_summary(rows)
    assert e["mint"]["resolved"] is False
    flags = ops.build_red_flags(ops.round_summary(rows, NOW), e, None,
                                {"missing_fields": 0}, 1.0, "账目: 仓库未记 0 条：无", True)
    assert any(f["code"] == "error_mint" for f in flags), flags


def test_price_gap_zero_qualify_is_not_a_red_flag():
    body = "1x2: challenge: 零合格（ev_site 门槛2.0%/池196/匹配5/候选5/最高-0.71%）——正常"
    a = ops.attribution(rows_of(loglines([body] * 48)))
    assert a["kind"] == "price_gap" and a["matched"] == 5 and a["max_ev"] == -0.71
    flags = ops.build_red_flags({"missing_count": 0, "missing_hours": []}, {}, a,
                                {"missing_fields": 0}, 1.0, "账目: 仓库未记 0 条：无", True)
    assert flags == [], f"正常零合格被误报成红线: {flags}"


def test_positive_ev_with_plus_sign_is_parsed():
    # 实测日志里 EV 会写成「最高+0.88%」，只允许负号会把最新一轮误判成无法归因。
    body = "1x2: challenge: 零合格（ev_site 门槛2.0%/池179/匹配5/候选5/最高+0.88%）——正常"
    a = ops.attribution(rows_of(loglines([body] * 48)))
    assert a["max_ev"] == 0.88 and a["kind"] == "price_gap", a


def test_zero_matches_is_a_data_hole():
    body = "1x2: challenge: 零合格（ev_site 门槛2.0%/池199/匹配0/候选0）——正常"
    a = ops.attribution(rows_of(loglines([body] * 48)))
    assert a["kind"] == "coverage_gap"
    flags = ops.build_red_flags({"missing_count": 0, "missing_hours": []}, {}, a,
                                {"missing_fields": 0}, 1.0, "账目: 仓库未记 0 条：无", True)
    assert any(f["code"] == "prediction_coverage_gap" for f in flags), flags


def test_pools_split_by_exp_and_gate():
    state = {"week": "2026-W44", "used": 3, "quota": 28, "records": [
        {"gate": "ev_site", "ev_site": 0.03, "site_odds": 1.9},                    # 无 exp → E8
        {"gate": "ev_site", "exp": "E8", "ev_site": 0.04, "site_odds": 2.0},
        {"gate": "ev_site", "exp": "E10", "ev_site": 0.05, "site_odds": 1.5},
        {"gate": "edge", "exp": "E8"},                                            # 实验前旧注不计
        {"gate": "ev_site", "exp": "E10"},                                        # 缺字段
    ]}
    s = ops.state_summary(state)
    assert s["pools"] == {"E8": 2, "E10": 2}, s
    assert s["missing_fields"] == 1 and s["used"] == 3 and s["quota"] == 28


def test_stale_snapshot_and_account_gap_are_flagged():
    flags = ops.build_red_flags({"missing_count": 0, "missing_hours": []}, {}, None,
                                {"missing_fields": 0}, 30.0,
                                "账目: 快照 12 条/13.0h（可能过期） vs 仓库 7；仓库未记 5 条：A vs B",
                                False)
    codes = {f["code"] for f in flags}
    assert {"snapshot_not_committed", "account_gap", "today_site_odds_missing"} <= codes, codes


def test_credentials_are_masked_in_report():
    secret = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghij.klmnopqrst"
    assert secret not in ops.mask(f"mint 失败 {secret}")
    assert "<凭据已掩码>" in ops.mask(secret)


def test_cli_exit_codes_and_write(tmp_path, monkeypatch):
    txt, js = tmp_path / "d.txt", tmp_path / "d.json"
    monkeypatch.setattr(ops, "OUT_TXT", txt)
    monkeypatch.setattr(ops, "OUT_JSON", js)
    clean = {"red_flags": [], "report": ["巡检: 48/48"]}
    monkeypatch.setattr(ops, "collect", lambda: clean)
    assert ops.main(["--write", "--quiet"]) == 0
    assert json.loads(js.read_text(encoding="utf-8"))["red_flags"] == []
    assert "48/48" in txt.read_text(encoding="utf-8")

    monkeypatch.setattr(ops, "collect", lambda: {"red_flags": [{"code": "x"}], "report": ["红线: 1 条"]})
    assert ops.main(["--quiet"]) == 1

    def boom():
        raise RuntimeError("炸了")
    monkeypatch.setattr(ops, "collect", boom)
    assert ops.main(["--quiet"]) == 2
