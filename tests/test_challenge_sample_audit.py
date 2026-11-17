"""E8/E10 样本完整性只读审计的测试（2026-11-17 每日探索 · scout）。

只测**判定逻辑**：合成小样本喂进各个纯函数，断言红旗/敞口的开关。
不碰仓库数据、不碰网络、不写 data/state/。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import challenge_sample_audit as csa  # noqa: E402
import challenge_reconcile as cr  # noqa: E402

D0 = "2026-11-10"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _iso(day: str, offset: int = 0) -> str:
    return (date.fromisoformat(day) + timedelta(days=offset)).isoformat()


def _rec(sid, home, away, pick="主胜@2.00", *, exp="E8", odds=2.2,
         day: str = D0, gate="ev_site", at: str | None = None):
    return {"sid": sid, "match": f"{home} vs {away}", "pick": pick, "market": "1x2",
            "play": "1x2", "line": None, "p": 0.5, "mkt_p": 0.45, "edge": 0.05,
            "ev": 1.0, "via": "api-ci", "gate": gate, "exp": exp, "site_odds": odds,
            "ev_site": 0.1, "mode": "normal",
            "submitted_at": at or f"{day} 12:35:00"}


def _pred(match_id, home, away):
    return {"match_id": match_id, "home_team": home, "away_team": away}


def _use_tmp(tmp_path, monkeypatch):
    """把审计与它复用的对账模块都指到 tmp，绝不允许碰仓库里的 data/。"""
    monkeypatch.setattr(csa, "ROOT", tmp_path)
    monkeypatch.setattr(csa, "STATE_PATH", tmp_path / "data/state/challenge_state.json")
    monkeypatch.setattr(csa, "NJ_DIR", tmp_path / "data/state/njstats")
    monkeypatch.setattr(cr, "STATE", tmp_path / "data/state")
    monkeypatch.setattr(cr, "DAILY", tmp_path / "data/daily")


# ---------------------------------------------------------------- 边界

def test_audit_never_imports_engine():
    src = (ROOT / "scripts" / "challenge_sample_audit.py").read_text(encoding="utf-8")
    bad = [m for m in re.findall(r"^\s*(?:from|import)\s+(engine[.\w]*)", src, re.M)
           if m != "engine.beijing_time"]
    assert not bad, f"审计脚本与裁判层同侧，不许 import engine.*：{bad}"


def test_json_output_refuses_data_state(tmp_path):
    # 红线：审计只读数，绝不往 data/state/ 里落状态文件
    import pytest
    banned = tmp_path / "data" / "state" / "audit.json"
    banned.parent.mkdir(parents=True)
    monkey_root = tmp_path
    old = csa.ROOT
    csa.ROOT = monkey_root
    try:
        with pytest.raises(SystemExit):
            csa.guard_out_path(str(banned))
        ok = csa.guard_out_path(str(tmp_path / "out" / "a.json"))
        assert ok.name == "a.json"
    finally:
        csa.ROOT = old


# ---------------------------------------------------------------- 轴 3

def test_no_duplicates_on_clean_ledger():
    recs = [_rec("s1", "Alpha", "Beta"), _rec("s2", "Gamma", "Delta")]
    d = csa.duplicate_bets(recs)
    assert d["sid_dups"] == {} and d["exact_fixture_dups"] == [] and d["alias_fixture_dups"] == []


def test_duplicate_bets_catch_sid_exact_and_alias():
    recs = [
        _rec("s1", "Alpha", "Beta"),
        _rec("s1", "Alpha", "Beta", day=_iso(D0, 1)),              # 同 sid 两注
        _rec("s2", "Gamma", "Delta"),
        _rec("s3", "利勒斯特罗姆", "皇家社会"),                        # 别名/子串同场
        _rec("s4", "利勒斯特伦", "皇家社会"),
    ]
    d = csa.duplicate_bets(recs)
    assert d["sid_dups"] == {"s1": 2}
    assert len(d["exact_fixture_dups"]) == 1
    assert len(d["alias_fixture_dups"]) == 1


# ---------------------------------------------------------------- 轴 5

def _snap(day, recs, count=None):
    return {"day": day, "snapshot_at": f"{day} 23:35:00", "count": count if count is not None
            else len(recs), "local_records": None, "records": recs}


def _srv(sid, home, away, odds="2.20", market=1, direction=1):
    return {"sid": sid, "home": home, "away": away, "market": market, "dir": direction,
            "odds": odds}


def test_truth_truncation_detected():
    # 提交时刻严格随 i 递增；快照只装「最近 60 条」= s010..s069，s000..s009 隐身
    local = [_rec(f"s{i:03d}", f"H{i:03d}", f"A{i:03d}",
                  at=f"{D0} {10 + i // 6:02d}:{(i % 6) * 10:02d}:00") for i in range(70)]
    snaps = [_snap(_iso(D0, -k), [_srv(f"s{j:03d}", f"H{j:03d}", f"A{j:03d}")
                                 for j in range(10, 70)]) for k in range(5)]
    st = csa.server_truth_vs_records(local, snaps)
    assert st["truncated_suspected"] is True
    # 隐身的那组提交时间必须整体早于可见的那组——这才叫「只返回最近 60 条」
    assert st["visible_submitted_range"][0] > st["invisible_submitted_range"][1]
    assert st["pinned_days_at_page_size"] == 5
    assert st["max_count"] == 60
    assert st["invisible_to_live_check"] == 10


def test_truth_not_flagged_below_page_size():
    local = [_rec(f"s{i}", f"H{i}", f"A{i}") for i in range(5)]
    snaps = [_snap(_iso(D0, -i), [_srv(f"s{j}", f"H{j}", f"A{j}") for j in range(5)])
             for i in range(5)]
    assert csa.server_truth_vs_records(local, snaps)["truncated_suspected"] is False


def test_server_only_split_by_name_and_odds_agreement():
    local = [_rec("s1", "Alpha", "Beta"), _rec("s2", "Gamma", "Delta"),
             {"match": "Old vs Legacy", "pick": "胜@2.98", "note": "无 sid 的老记录"}]
    snaps = [_snap(D0, [_srv("s1", "Alpha", "Beta", odds="2.20"),
                        _srv("s2", "Gamma", "Delta", odds="9.99"),      # 价不一致
                        _srv("s9", "Ghost", "Fixture"),                 # 对不回
                        _srv("s8", "Old", "Legacy")])]                  # 无 sid 但队名对得上
    st = csa.server_truth_vs_records(local, snaps)
    assert len(st["server_only"]) == 2
    assert st["server_only_matched_by_name"] == 1
    assert [x["match"] for x in st["server_only_unmatched"]] == ["Ghost vs Fixture"]
    assert st["bet_odds_agree_with_server"] == 1
    assert st["bet_odds_differ_from_server"] == 1
    assert st["local_only_sids"] == []


def test_phantom_local_bet_is_visible():
    local = [_rec("s1", "Alpha", "Beta"), _rec("sX", "Phantom", "Ghost")]
    snaps = [_snap(D0, [_srv("s1", "Alpha", "Beta")])]
    st = csa.server_truth_vs_records(local, snaps)
    assert st["local_only_sids"] == ["sX"]


# ---------------------------------------------------------------- 轴 1

def test_snapshot_gap_counts_missing_days(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    want_from = date(2026, 11, 1)
    for kind in ("site_odds", "my_predictions", "community"):
        d = tmp_path / "data/state/njstats" / kind
        d.mkdir(parents=True)
        (d / "2026-11-01.json").write_text('{"snapshot_at": "2026-11-01 23:35:00"}', encoding="utf-8")
    g = csa.snapshot_gaps(want_from, date(2026, 11, 5))
    assert g["site_odds"]["missing_days"] == ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    assert g["site_odds"]["coverage_pct"] == 0.2
    assert g["site_odds"]["kept_snapshot_hour_bjt_median"] == 23 + 35 / 60


def test_gap_window_starts_at_each_kinds_own_first_file(tmp_path, monkeypatch):
    """11-08 才上线的 strategies 不能把之前的历史全报成缺口——那是噪音，会淹掉真缺口。"""
    _use_tmp(tmp_path, monkeypatch)
    d = tmp_path / "data/state/njstats/strategies"
    d.mkdir(parents=True)
    for day in ("2026-11-08", "2026-11-09", "2026-11-10"):
        (d / f"{day}.json").write_text('{"snapshot_at": "2026-11-08 00:05:00"}', encoding="utf-8")
    g = csa.snapshot_gaps(date(2026, 10, 22), date(2026, 11, 10))
    assert g["strategies"]["missing_days"] == []
    assert g["strategies"]["first_file"] == "2026-11-08"
    assert g["strategies"]["want_days"] == 3


# ---------------------------------------------------------------- 轴 2

def _judged_setup(tmp_path, monkeypatch, recs, preds, results, ledger):
    _use_tmp(tmp_path, monkeypatch)
    for (day, p) in preds:
        _write(cr.DAILY / day / "predictions.json", p)
    for (day, r) in results:
        _write(cr.DAILY / day / "results_sina.json", r)
    lines = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in ledger)
    (cr.STATE / "review_ledger.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (cr.STATE / "review_ledger.jsonl").write_text(lines, encoding="utf-8")
    _write(cr.STATE / "challenge_state.json", {"records": recs})
    return csa.judgments(recs)


def test_unsettled_bets_still_counted_in_pace_n(tmp_path, monkeypatch):
    # 队名等长且互不包含：match_pair_review 是子串匹配，H1/H15 会互相冒充
    recs = [_rec("s001", "Alpha", "Beta")]
    for i in range(1, 60):
        recs.append(_rec(f"s{i:03d}", f"H{i:03d}", f"A{i:03d}"))
    preds = [(D0, [_rec_mid(r) for r in recs])]
    results = [(D0, [{"home_team": r["match"].split(" vs ")[0],
                      "away_team": r["match"].split(" vs ")[1],
                      "home_score": 2, "away_score": 0,
                      "match_time": f"{D0} 20:00:00"} for r in recs[:-1]])]
    ledger = [{"match_id": f"{D0}_x{i}", "actual_idx": 0} for i in range(len(recs) - 1)]
    judged = _judged_setup(tmp_path, monkeypatch, recs, preds, results, ledger)
    cov = csa.settlement_coverage(recs, judged, date.fromisoformat(D0))
    assert cov["E8"]["n_pool"] == 60
    assert cov["E8"]["n_unsettled_in_n"] == 1
    assert cov["E8"]["target_reached_while_unsettled"] is True
    flags = csa.red_flags({"server_truth": csa.server_truth_vs_records(recs, []),
                           "snapshot_gaps": {}, "settlement": cov, "handicap": {
                               "n_actual_push_bets": 0, "push_counted_as_hit": True,
                               "push_bets": [], "n_handicap_bets": 0, "n_integer_line": 0,
                               "n_handicap_in_ev_site_pool": 0},
                           "payout_math": csa.payout_math(recs, judged)})
    hit = [f for f in flags if "可收尾" in f["text"]]
    assert hit and hit[0]["level"] == "note"      # 到期条件本就是「已提交满 60」，不是红灯


def _rec_mid(rec):
    h, a = rec["match"].split(" vs ")
    return _rec_pred(rec["sid"], h, a)


def _rec_pred(sid, home, away):
    return {"match_id": f"{D0}_{sid}", "home_team": home, "away_team": away,
            "home_win_prob": 0.5, "draw_prob": 0.25, "away_win_prob": 0.25,
            "home_odds": 2.0, "draw_odds": 3.2, "away_odds": 3.6,
            "market_fair": [0.45, 0.28, 0.27]}


def test_settled_without_exact_score_is_counted(tmp_path, monkeypatch):
    """1x2 靠账本判定，所以「没有比分」不丢样本；但让球必须有比分——要看得见。"""
    recs = [_rec("s1", "Alpha", "Beta")]
    judged = _judged_setup(tmp_path, monkeypatch, recs,
                           [(D0, [_rec_pred("s1", "Alpha", "Beta")])], [],
                           [{"match_id": f"{D0}_s1", "actual_idx": 0}])
    assert judged[0]["hit"] is True and judged[0]["score"] is False
    cov = csa.settlement_coverage(recs, judged, date.fromisoformat(D0))
    assert cov["E8"]["judged_without_exact_score"] == 1


# ---------------------------------------------------------------- 轴 4

def test_push_rule_applies_to_both_sides():
    """整数线走盘：主让 1 球 2-1、客受让 1 球 1-0，现口径都算未命中。
    这条测试是**记录现状**（与站点 push 口径不同，见诊断文档 §4），不是背书。"""
    from challenge_review import judge_hit, parse_pick

    assert judge_hit(parse_pick("主队-1@0.95"), 0, 2, 1) is False
    assert judge_hit(parse_pick("客+1@0.95"), 0, 1, 0) is False
    # 半球线永不走盘：客+0.5 遇 1-0 = 输，遇 0-0 = 赢，两种都不是 push 分支
    assert judge_hit(parse_pick("客+0.5@0.95"), 0, 1, 0) is False
    assert judge_hit(parse_pick("客+0.5@0.95"), 1, 0, 0) is True


def test_push_rule_documented_and_push_bet_detected(tmp_path, monkeypatch):
    """现口径：整数让球线的走盘算未命中（tests/test_challenge_reconcile.py 已钉住）。
    审计要做的是把「有注落在走盘上」这件事量出来，不是偷偷改判据。"""
    recs = [_rec("s1", "Alpha", "Beta", pick="主队-1@0.95", gate="ev_site", exp="E8")]
    judged = _judged_setup(tmp_path, monkeypatch, recs,
                           [(D0, [_rec_pred("s1", "Alpha", "Beta")])],
                           [(D0, [{"home_team": "Alpha", "away_team": "Beta",
                                   "home_score": 1, "away_score": 0,
                                   "match_time": f"{D0} 20:00:00"}])],
                           [{"match_id": f"{D0}_s1", "actual_idx": 0}])
    hc = csa.handicap_exposure(recs, judged)
    assert hc["push_counted_as_hit"] is False          # 规则本身（未改）
    assert hc["n_handicap_bets"] == 1 and hc["n_integer_line"] == 1
    assert hc["n_actual_push_bets"] == 1               # 这 1 注确实净胜==线
    assert hc["push_bets"] == ["Alpha vs Beta"]
    flags = csa.red_flags({"server_truth": {"truncated_suspected": False, "server_only_unmatched": [],
                                            "pinned_days_at_page_size": 0, "n_sid_local": 0,
                                            "invisible_to_live_check": 0},
                           "snapshot_gaps": {}, "settlement": {}, "handicap": hc,
                           "payout_math": {}})
    assert any(f["level"] == "red" and "走盘" in f["text"] for f in flags)


def test_half_line_handicap_is_not_a_push():
    recs = [_rec("s1", "Alpha", "Beta", pick="客+0.5@0.95")]
    judged = [{"play": "handicap", "line": 0.5, "score_line": (1, 0), "gate": "ev_site",
               "hit": False, "match": "Alpha vs Beta"}]
    assert csa.handicap_exposure(recs, judged)["n_actual_push_bets"] == 0


# ---------------------------------------------------------------- 记账口径

def test_payout_math_flags_unpriced_judged_bet(tmp_path, monkeypatch):
    recs = [_rec("s1", "Alpha", "Beta", odds=2.0),
            {"sid": "s2", "match": "Gamma vs Delta", "pick": "主胜@3.00", "gate": "ev_site",
             "exp": "E8", "site_odds": None, "submitted_at": f"{D0} 12:35:00"}]
    judged = _judged_setup(tmp_path, monkeypatch, recs,
                           [(D0, [_rec_pred("s1", "Alpha", "Beta"),
                                  _rec_pred("s2", "Gamma", "Delta")])],
                           [], [{"match_id": f"{D0}_s1", "actual_idx": 0},
                                {"match_id": f"{D0}_s2", "actual_idx": 0}])
    m = csa.payout_math(recs, judged)["E8"]
    assert m["judged"] == 2 and m["priced_judged"] == 1 and m["roi_denominator_gap"] == 1
    flags = csa.red_flags({"server_truth": {"truncated_suspected": False, "server_only_unmatched": [],
                                            "pinned_days_at_page_size": 0, "n_sid_local": 0,
                                            "invisible_to_live_check": 0},
                           "snapshot_gaps": {}, "settlement": {},
                           "handicap": {"n_actual_push_bets": 0, "push_counted_as_hit": True,
                                        "push_bets": [], "n_handicap_bets": 0,
                                        "n_integer_line": 0, "n_handicap_in_ev_site_pool": 0},
                           "payout_math": {"E8": m}})
    assert any("ROI 分母" in f["text"] and f["level"] == "red" for f in flags)


def test_break_even_denominator_drift_is_reported(tmp_path, monkeypatch):
    """面板的打平线算在已提交池，命中率算在已结算池——收尾读数必须看得见这个差。"""
    recs = [_rec("s1", "Alpha", "Beta", odds=1.5), _rec("s2", "Gamma", "Delta", odds=3.0),
            _rec("s3", "Kappa", "Lambda", odds=1.5)]
    judged = [{"hit": True, "sid": "s1"}, {"hit": None, "sid": "s2"}, {"hit": True, "sid": "s3"}]
    m = csa.payout_math(recs, judged)["E8"]
    assert abs(m["break_even_printed_on_pool"] - (2 / 1.5 + 1 / 3) / 3) < 1e-9
    assert abs(m["break_even_on_settled"] - (1 / 1.5 + 1 / 1.5) / 2) < 1e-9
    assert abs(m["break_even_denominator_drift_pp"] - (m["break_even_printed_on_pool"]
                                                       - m["break_even_on_settled"]) * 100) < 1e-9
    assert abs(m["break_even_denominator_drift_pp"]) > 10
    flags = csa.red_flags({"server_truth": {"truncated_suspected": False, "server_only_unmatched": []},
                           "snapshot_gaps": {}, "settlement": {},
                           "handicap": {"n_actual_push_bets": 0, "push_bets": [],
                                        "n_handicap_bets": 0, "n_integer_line": 0,
                                        "n_handicap_in_ev_site_pool": 0,
                                        "push_counted_as_hit": True},
                           "payout_math": {"E8": m}})
    assert any("已提交池" in f["text"] and f["level"] == "note" for f in flags)


def test_target_reached_while_unsettled_is_note_not_red():
    hc = {"n_actual_push_bets": 0, "push_bets": [], "n_handicap_bets": 0, "n_integer_line": 0,
          "n_handicap_in_ev_site_pool": 0, "push_counted_as_hit": True}
    cov = {"E8": {"n_pool": 61, "n_judged": 60, "n_unsettled_in_n": 1,
                  "unsettled_matches": [], "judged_without_exact_score": 0,
                  "settled_no_score_matches": [], "reaches_target_60": True,
                  "target_reached_while_unsettled": True, "pace_line": ""},
           "E10": {"n_pool": 0, "n_judged": 0, "n_unsettled_in_n": 0, "unsettled_matches": [],
                   "judged_without_exact_score": 0, "settled_no_score_matches": [],
                   "reaches_target_60": False, "target_reached_while_unsettled": False,
                   "pace_line": ""}}
    flags = csa.red_flags({"server_truth": {"truncated_suspected": False, "server_only_unmatched": []},
                           "snapshot_gaps": {}, "settlement": cov, "handicap": hc,
                           "payout_math": {}})
    hit = [f for f in flags if "可收尾" in f["text"]]
    assert hit and hit[0]["level"] == "note"


def test_break_even_uses_mean_implied_probability():
    recs = [_rec("s1", "Alpha", "Beta", odds=1.5), _rec("s2", "Gamma", "Delta", odds=3.0)]
    judged = [{"hit": True, "gate": "ev_site", "sid": "s1", "match": "Alpha vs Beta"},
              {"hit": True, "gate": "ev_site", "sid": "s2", "match": "Gamma vs Delta"}]
    m = csa.payout_math(recs, judged)["E8"]
    assert abs(m["break_even_printed_on_pool"] - (1 / 1.5 + 1 / 3) / 2) < 1e-9
    assert abs(m["break_even_flat_stake"] - 1 / 2.25) < 1e-9
    assert m["break_even_overstated_pp"] > 0


# ---------------------------------------------------------------- 轴 5b：市场码

def test_market_code_conflict_detected():
    """本地 pick 写成 1x2、服务端记 market=4 → 判定用的市场与结算方不一致，必须数出来。"""
    recs = [
        {"match": "老队A vs 老队B", "pick": "主胜@1.73"},              # 无 sid，按队名回落
        _rec("s2", "Handi", "Home", pick="主队-1@0.95"),               # 本地已标让球 → 不算冲突
        {"match": "Missing vs Ghost", "pick": "主胜@2.00"},
    ]
    snaps = [_snap(D0, [_srv("x1", "老队A", "老队B", odds="1.73", market=4),
                        _srv("s2", "Handi", "Home", odds="0.95", market=2),
                        _srv("x3", "Nobody", "Here", odds="2.00", market=5),
                        _srv("s9", "Plain", "Game", odds="2.00", market=1)])]
    m = csa.market_code_conflicts(recs, snaps)
    assert m["non_1x2_server_bets"] == 3 and m["union_size"] == 4
    assert m["n_conflicts"] == 1
    # 让球那注：record 的 market 字段写着 1x2（历史污染形态），但 pick 是主队-1 →
    # judge_hit 按比分判，属于「字段标错、判定没错」，不能和「判错市场」混成一件事
    assert [x["sid"] for x in m["label_only_mismatch"]] == ["s2"]
    assert m["judged_as_1x2_but_server_other_market"][0]["match"] == "老队A vs 老队B"
    assert m["judged_as_1x2_but_server_other_market"][0]["server_market"] == 4
    assert [x["match"] for x in m["server_only_unregistered"]] == ["Nobody vs Here"]
    assert m["n_conflicts_in_ev_site_pool"] == 0
    flags = csa.red_flags({"server_truth": {"truncated_suspected": False, "server_only_unmatched": []},
                           "snapshot_gaps": {}, "settlement": {}, "payout_math": {},
                           "handicap": {"n_actual_push_bets": 0, "push_bets": [], "n_handicap_bets": 0,
                                        "n_integer_line": 0, "n_handicap_in_ev_site_pool": 0,
                                        "push_counted_as_hit": True},
                           "market_codes": m})
    hit = [f for f in flags if "非 1x2 市场码" in f["text"]]
    assert hit and hit[0]["level"] == "note"


def test_market_code_conflict_in_pool_is_red():
    recs = [_rec("s1", "Alpha", "Beta", pick="主胜@2.00")]
    snaps = [_snap(D0, [_srv("s1", "Alpha", "Beta", odds="2.20", market=4)])]
    m = csa.market_code_conflicts(recs, snaps)
    assert m["n_conflicts"] == 1 and m["n_conflicts_in_ev_site_pool"] == 1
    flags = csa.red_flags({"server_truth": {"truncated_suspected": False, "server_only_unmatched": []},
                           "snapshot_gaps": {}, "settlement": {}, "payout_math": {},
                           "handicap": {"n_actual_push_bets": 0, "push_bets": [], "n_handicap_bets": 0,
                                        "n_integer_line": 0, "n_handicap_in_ev_site_pool": 0,
                                        "push_counted_as_hit": True},
                           "market_codes": m})
    assert any(f["level"] == "red" and "冻结池内" in f["text"] for f in flags)


# ---------------------------------------------------------------- 到期后窗口

def test_deadline_drift_counts_bets_after_crossing():
    """登记判据写的是「满 60 注（先到为准）」；一轮最多 K=6 注，所以越线天然是批量。
    审计只把「越线后又进了几注」数出来，不动任何判据。"""
    # 前 56 注各占一分钟；第 57-60 注同一轮（共享 10:00:00，第 60 注在这里越线）；
    # 之后还有 4 注分别在 11:05 / 12:05 / 13:05 / 14:05 —— 这就是「先到之后又收的注」
    recs = []
    for i in range(64):
        if i < 56:
            at = f"2026-11-16 09:{i:02d}:00"
        elif i < 60:
            at = "2026-11-16 10:00:00"
        else:
            at = f"2026-11-16 {11 + i - 60:02d}:05:00"
        recs.append(_rec(f"e{i:03d}", f"P{i:03d}", f"Q{i:03d}", exp="E10", at=at))
    d = csa.pool_deadline_drift(recs)
    assert d["E10"]["n_pool"] == 64
    assert d["E10"]["crossed_at"] == "2026-11-16 10:00:00"
    assert d["E10"]["bets_after_target"] == 4
    assert d["E10"]["bets_at_crossing_timestamp"] == 4
    assert d["E8"]["n_pool"] == 0 and d["E8"]["crossed_at"] is None


def test_deadline_drift_flag_is_a_note_not_red():
    recs = [_rec(f"e{i:03d}", f"P{i:03d}", f"Q{i:03d}", exp="E10",
                 at=f"2026-11-16 {10 + i // 6:02d}:00:00") for i in range(62)]
    hc = {"n_actual_push_bets": 0, "push_bets": [], "n_handicap_bets": 0, "n_integer_line": 0,
          "n_handicap_in_ev_site_pool": 0, "push_counted_as_hit": True}
    rep = {"server_truth": {"truncated_suspected": False, "server_only_unmatched": []},
           "snapshot_gaps": {}, "settlement": {}, "handicap": hc, "payout_math": {},
           "deadline_drift": csa.pool_deadline_drift(recs)}
    flags = csa.red_flags(rep)
    hit = [f for f in flags if "先到时点" in f["text"]]
    assert hit and hit[0]["level"] == "note"
    assert not any(f["level"] == "red" and "先到时点" in f["text"] for f in flags)


# ---------------------------------------------------------------- 收盘窗口

def test_close_window_stats_split_by_era():
    # median 取 v[n//2]：偶数样本偏上，和脚本里其它分位数一个口径
    st = csa.close_window_stats([1.0, 2.0, 20.0, 40.0], window_h=6)
    assert st == {"n": 4, "median_h": 20.0, "within_close_window": 2,
                  "within_close_window_pct": 0.5}
    assert csa.close_window_stats([])["n"] == 0
    assert csa.close_window_stats([])["median_h"] is None


def test_close_window_note_only_when_sparse():
    hc = {"n_actual_push_bets": 0, "push_bets": [], "n_handicap_bets": 0, "n_integer_line": 0,
          "n_handicap_in_ev_site_pool": 0, "push_counted_as_hit": True}
    rep = {"server_truth": {"truncated_suspected": False, "server_only_unmatched": []},
           "snapshot_gaps": {}, "settlement": {}, "handicap": hc, "payout_math": {},
           "odds_close": {"hourly_archive_since": "2026-11-09", "close_window_h": 6,
                          "era_stats": {"日内序列完整": {"n": 10, "median_h": 12.9,
                                                    "within_close_window": 4,
                                                    "within_close_window_pct": 0.4}}}}
    flags = csa.red_flags(rep)
    assert any("收盘价本来就取不到" in f["text"] for f in flags)
    rep["odds_close"]["era_stats"]["日内序列完整"]["within_close_window_pct"] = 0.9
    assert not any("收盘价本来就取不到" in f["text"] for f in csa.red_flags(rep))


def test_empty_state_does_not_crash(tmp_path, monkeypatch, capsys):
    """没有 records、没有快照的仓库（新 checkout / 拔除后）也必须能跑完并打印。"""
    _use_tmp(tmp_path, monkeypatch)
    (tmp_path / "data/state").mkdir(parents=True, exist_ok=True)
    rc = csa.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "样本完整性审计" in out and "红旗" in out
    assert "（无）" in out                       # 空仓库不该有红旗，也不该崩
    assert "打平线（算在已提交池）—" in out        # 空池的百分比要能渲染


# ---------------------------------------------------------------- 渲染 / CLI

def _fake_report():
    return {"generated_at": "x", "baseline": "y", "records": 0, "pool_sizes": {"E8": 0, "E10": 0},
            "duplicates": csa.duplicate_bets([]),
            "server_truth": csa.server_truth_vs_records([], []),
            "snapshot_gaps": {}, "settlement": {}, "handicap": csa.handicap_exposure([], []),
            "payout_math": {}, "red_flags": [{"level": "red", "text": "红灯"},
                                             {"level": "note", "text": "注记"}]}


def test_render_shows_both_levels():
    out = csa.render(_fake_report())
    assert "红灯" in out and "注记" in out and "红旗" in out


def test_ci_exit_code_only_for_red(tmp_path, monkeypatch):
    rep = _fake_report()
    rep["red_flags"] = [{"level": "note", "text": "只是注记"}]
    monkeypatch.setattr(csa, "build_report", lambda **kw: rep)
    assert csa.main(["--ci"]) == 0
    rep["red_flags"] = [{"level": "red", "text": "红"}]
    assert csa.main(["--ci"]) == 1
    assert csa.main([]) == 0
