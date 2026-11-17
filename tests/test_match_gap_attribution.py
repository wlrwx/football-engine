"""匹配缺口归因脚本（issue #28，2026-11-18）。

这里测的是**归因口径本身**，不是站点数据：桶必须互斥且加总等于板面场数、
「别名可挽回」必须由开赛时刻背书、缺赔率与没落库这两类得单独成桶。
之所以能用纯合成数据测，是因为脚本把所有输入都做成了参数（root/day），
真仓库数据只在人工跑 `--root .` 时消费——CI 上没有 23 张快照也不能红。
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("match_gap_attribution",
                                              ROOT / "scripts" / "match_gap_attribution.py")
mga = importlib.util.module_from_spec(SPEC)
sys.modules["match_gap_attribution"] = mga
SPEC.loader.exec_module(mga)

BJT = timezone(timedelta(hours=8))
DAY = "2026-11-01"
SNAP_AT = "2026-11-01 23:35:00"


def ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT).timestamp())


def make_repo(tmp_path: Path, pool, preds_by_day, results=None, truth=None, total=None,
              policy=None):
    """搭一个最小仓库：池子快照 + 两天预测 + 赛果 + 服务端真值 + policy。"""
    (tmp_path / mga.SNAP_DIR).mkdir(parents=True, exist_ok=True)
    (tmp_path / mga.SNAP_DIR / f"{DAY}.json").write_text(json.dumps({
        "snapshot_at": SNAP_AT, "total": total if total is not None else len(pool),
        "with_odds": len(pool), "matches": pool,
    }, ensure_ascii=False), encoding="utf-8")
    for day, preds in preds_by_day.items():
        d = tmp_path / "data" / "daily" / day
        d.mkdir(parents=True, exist_ok=True)
        if preds is not None:
            (d / "predictions.json").write_text(json.dumps(preds, ensure_ascii=False), encoding="utf-8")
    for day, rows in (results or {}).items():
        d = tmp_path / "data" / "daily" / day
        d.mkdir(parents=True, exist_ok=True)
        (d / "results.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    if truth is not None:
        td = tmp_path / mga.TRUTH_DIR
        td.mkdir(parents=True, exist_ok=True)
        (td / f"{DAY}.json").write_text(json.dumps({"snapshot_at": SNAP_AT, "records": truth},
                                                   ensure_ascii=False), encoding="utf-8")
    if policy is not None:
        pf = tmp_path / mga.POLICY_PATH
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def pred(home, away, kickoff, comp="测试联赛", probs=(0.5, 0.3, 0.2), odds=(1.9, 3.4, 4.0)):
    return {"match_id": f"{kickoff[:10]}_周日001", "competition": comp, "home_team": home,
            "away_team": away, "kickoff": kickoff, "home_win_prob": probs[0],
            "draw_prob": probs[1], "away_win_prob": probs[2], "home_odds": odds[0],
            "draw_odds": odds[1], "away_odds": odds[2],
            "market_fair": [0.45, 0.30, 0.25]}


def item(home, away, kickoff, odds=(1.8, 3.5, 4.2), sid="s001"):
    return {"sid": sid, "home": home, "away": away, "kick_ts": ts(kickoff), "odds": list(odds)}


# ------------------------------------------------------------------ 相似度
def test_side_similarity_covers_transliteration_but_not_squads():
    # 生产四档（精确/归一化/互含）都对不上，只有相似度能捞回来的一条：实测 10-24 板
    assert mga.side_similarity("沃尔夫斯堡", "沃夫斯堡") == "similar"
    # 「布里斯托城 / 布里斯托尔城」：中间插一个字，互含失效，只能靠相似度
    assert mga.side_similarity("布里斯托城", "布里斯托尔城") == "similar"
    # 成年队与 U19 永远不算同一支队（10-21 板上两者同时在售）
    assert mga.side_similarity("皇家贝蒂斯U19", "皇家贝蒂斯") is None
    # 不同球队不得被 ratio 捞成同一支
    assert mga.side_similarity("国际米兰", "米兰") is None or mga.side_similarity("国际米兰", "AC米兰") == "contain"


def test_pair_similarity_requires_both_sides():
    ok, differs = mga.pair_similarity("波鸿", "沃尔夫斯堡", "波鸿", "沃夫斯堡")
    assert (ok, differs) == (True, True)
    assert mga.pair_similarity("波鸿", "沃尔夫斯堡", "波鸿", "汉堡") == (False, False)


# ------------------------------------------------------------------ 池侧归因
def test_production_match_is_counted_as_production_not_alias(tmp_path):
    """队名互含的（生产第三档能捞到）必须算 matched，不能被诊断规则抢走。"""
    repo = make_repo(tmp_path, [item("美因茨", "云达不莱梅", "2026-11-02 03:00:00")],
                     {DAY: [pred("美因茨", "不莱梅", "2026-11-02 03:00:00")],
                      "2026-11-02": []})
    r = mga.classify_day(repo, DAY)
    assert r["pool_buckets"] == {"4_matched_production": 1}
    assert r["alias_evidence"] == []
    assert r["matched_from_dir"] == {"D": 1}   # 这条预测来自销售日 D 的目录


def test_alias_bucket_needs_kickoff_agreement(tmp_path):
    """同名不同写 + 开赛时刻一致 = 别名可挽回；时刻差几小时 = 只算疑似。"""
    pool = [item("波鸿", "沃尔夫斯堡", "2026-11-02 03:00:00", sid="a1"),
            item("谢周三", "阿克林顿", "2026-11-02 03:00:00", sid="a2")]
    preds = {DAY: [], "2026-11-02": [pred("波鸿", "沃夫斯堡", "2026-11-02 03:05:00"),
                                     pred("谢周三", "阿克宁顿", "2026-11-02 19:00:00")]}
    r = mga.classify_day(make_repo(tmp_path, pool, preds), DAY)
    assert r["pool_buckets"]["5_alias_recoverable"] == 1
    assert r["pool_buckets"]["6_alias_unconfirmed"] == 1
    levels = {e["pool"]: e["level"] for e in r["alias_evidence"]}
    assert levels["波鸿 vs 沃尔夫斯堡"] == "time"
    assert levels["谢周三 vs 阿克林顿"] == "no_time"


def test_youth_squad_entry_is_not_an_alias(tmp_path):
    """池子上是 U19、我们预测的是成年队：时刻不同、梯队标记不同，不能算别名丢失。"""
    pool = [item("皇家贝蒂斯U19", "费耶诺德U19", "2026-11-02 03:00:00")]
    preds = {DAY: [pred("皇家贝蒂斯", "费耶诺德", "2026-11-02 03:00:00")], "2026-11-02": []}
    r = mga.classify_day(make_repo(tmp_path, pool, preds), DAY)
    assert "5_alias_recoverable" not in r["pool_buckets"]
    assert "6_alias_unconfirmed" not in r["pool_buckets"]


def test_window_and_scope_buckets(tmp_path):
    """开球在 D+2 之后 / 竞彩没开售 / 距开赛不足 45 分钟，三类各归各桶。"""
    pool = [item("萨布塔洛", "米亚尔比", "2026-11-04 00:45:00", sid="b1"),      # D+3 → 视野外
            item("佩雷拉", "茨高", "2026-11-02 08:00:00", sid="b2"),            # 无竞彩开售证据
            item("尼斯拉德尼基", "诺维帕扎尔", "2026-11-01 23:50:00", sid="b3")]  # 已不足 45min
    preds = {DAY: [], "2026-11-02": []}
    r = mga.classify_day(make_repo(tmp_path, pool, preds), DAY)
    assert r["pool_buckets"]["7_beyond_forecast_window"] == 1
    assert r["pool_buckets"]["10_no_sporttery_evidence"] == 1
    assert r["pool_buckets"]["2_kickoff_too_close"] == 1


def test_sold_by_sporttery_but_not_predicted(tmp_path):
    """赛果名单里有、预测里没有 → 单列一桶，不能混进「不在竞彩范围」。"""
    pool = [item("泰港", "北京国安", "2026-11-02 08:00:00")]
    make_repo(tmp_path, pool, {DAY: [], "2026-11-02": []},
              results={"2026-11-01": [{"match_id": "2026-11-01_周日009", "home_team": "泰港",
                                       "away_team": "北京国安", "home_score": 0, "away_score": 1}]})
    r = mga.classify_day(tmp_path, DAY)
    assert r["pool_buckets"]["8_sold_not_predicted_exact"] == 1
    assert "10_no_sporttery_evidence" not in r["pool_buckets"]


def test_sold_but_spelled_differently(tmp_path):
    """池子写法与竞彩写法不同（布里斯托城 / 布里斯托尔城）→ variant 桶，与 exact 分开。"""
    pool = [item("布里斯托城", "狼队", "2026-11-02 04:00:00")]
    make_repo(tmp_path, pool, {DAY: [], "2026-11-02": []},
              results={"2026-11-01": [{"match_id": "2026-11-01_周日003", "home_team": "布里斯托尔城",
                                       "away_team": "狼队", "home_score": 1, "away_score": 0}]})
    assert mga.classify_day(tmp_path, DAY)["pool_buckets"]["9_sold_not_predicted_variant"] == 1


def test_already_submitted_uses_server_truth(tmp_path):
    pool = [item("奥摩尼亚", "奈梅亨", "2026-11-02 01:45:00")]
    make_repo(tmp_path, pool, {DAY: [], "2026-11-02": []},
              truth=[{"home": "奥摩尼亚", "away": "奈梅亨", "market": 1, "dir": 1}])
    r = mga.classify_day(tmp_path, DAY)
    assert r["pool_buckets"] == {"3_already_submitted": 1}


def test_no_site_odds_counted_from_total(tmp_path):
    """快照的 matches 只存带赔率的条目，缺的那部分必须按 total − len(matches) 补进桶 0，
    否则「板上有、站点没开盘」这一类会凭空消失，漏斗加总也对不上板面场数。"""
    pool = [item("甲队", "乙队", "2026-11-02 05:00:00")]
    make_repo(tmp_path, pool, {DAY: [], "2026-11-02": []}, total=len(pool) + 37)
    r = mga.classify_day(tmp_path, DAY)
    assert r["pool_buckets"]["0_no_site_odds"] == 37
    assert sum(r["pool_buckets"].values()) == r["pool_board_total"]


def test_buckets_are_exhaustive_and_disjoint(tmp_path):
    """不变量：池侧各桶之和 = 板面场数。少一类或多算都会在这里红。"""
    pool = [item("甲队", "乙队", "2026-11-02 05:00:00", sid="c1"),
            item("丙队", "丁队", "2026-10-31 01:00:00", sid="c2"),
            {"sid": None, "home": "无id", "away": "对手", "kick_ts": ts("2026-11-02 05:00:00"),
             "odds": [2.0, 3.0, 4.0]}]
    r = mga.classify_day(make_repo(tmp_path, pool, {DAY: [], "2026-11-02": []}, total=9), DAY)
    assert sum(r["pool_buckets"].values()) == r["pool_board_total"] == 9
    assert r["pool_buckets"]["1_pool_data_missing"] == 1


def test_missing_predictions_day_is_recorded(tmp_path):
    """D+1 没落库 = 那一刻生产手里没有明天的预测（11-16 快照匹配 0 的成因），必须显式记下来。"""
    pool = [item("甲队", "乙队", "2026-11-02 05:00:00")]
    make_repo(tmp_path, pool, {DAY: [], "2026-11-02": None})
    r = mga.classify_day(tmp_path, DAY)
    assert r["missing_pred_days"] == ["2026-11-02"]
    assert r["pred_days"]["2026-11-02"] is None


# ------------------------------------------------------------------ 预测侧
def test_prediction_side_separates_past_and_absent(tmp_path):
    """已开赛、板上没这场、别名丢的，三种「没匹配上」在预测侧必须分得开。"""
    pool = [item("波鸿", "沃尔夫斯堡", "2026-11-02 03:05:00")]
    preds = {DAY: [pred("凌晨早场", "已结束队", "2026-11-01 20:00:00"),
                   pred("站点没有", "也没有", "2026-11-02 06:00:00")],
             "2026-11-02": [pred("波鸿", "沃夫斯堡", "2026-11-02 03:05:00")]}
    r = mga.classify_day(make_repo(tmp_path, pool, preds), DAY)
    assert r["pred_buckets"]["3_kickoff_already_past"] == 1
    assert r["pred_buckets"]["6_not_on_pool_board"] == 1
    assert r["pred_buckets"]["4_alias_recoverable"] == 1
    assert sum(r["pred_buckets"].values()) == r["pred_total"] == 3


# ------------------------------------------------------------------ 闸门重放
def test_replay_candidate_uses_policy_ev_site_gate(tmp_path):
    """ev_site = P × 站点价 − 1；门槛与上界从 policy 读，脚本不参与判定。"""
    gates = {"gate_mode": "ev_site", "ev_floor": 0.02, "min_sporttery_odds": 0.0, "gate_delta": 0.0}
    p = pred("甲队", "乙队", "2026-11-02 05:00:00", probs=(0.55, 0.25, 0.20), odds=(1.9, 3.4, 4.0))
    hit = mga.replay_candidate(p, [1.80, 3.5, 4.2], gates)      # 0.55*1.8-1 = -0.01 → 不合格
    assert hit["qualified"] is False and hit["dir"] == 0
    hit2 = mga.replay_candidate(p, [2.10, 3.5, 4.2], gates)     # 0.55*2.1-1 = +0.155 → 合格
    assert hit2["qualified"] is True
    hit3 = mga.replay_candidate(p, [5.0, 3.5, 4.2], gates)      # 0.55*5-1 = +1.75 → 超上界，脏盘口
    assert hit3["qualified"] is False
    assert hit3["e10_low_price"] is False
    low = pred("甲队", "乙队", "2026-11-02 05:00:00", odds=(1.35, 4.4, 6.0))
    assert mga.replay_candidate(low, [1.5, 4.4, 6.0], gates)["e10_low_price"] is True
    assert mga.replay_candidate({"home_team": "x"}, [1.5], gates) is None


def test_load_policy_gates_reads_frozen_values(tmp_path):
    (tmp_path / mga.POLICY_PATH).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / mga.POLICY_PATH).write_text(json.dumps(
        {"gate_mode": "ev_site", "ev_floor": 0.02, "min_sporttery_odds": 0.0,
         "gate_hint": "tighten", "n_settled": 106}), encoding="utf-8")
    g = mga.load_policy_gates(tmp_path)
    assert g["gate_mode"] == "ev_site" and g["ev_floor"] == 0.02
    assert g["min_sporttery_odds"] == 0.0 and g["gate_delta"] == 0.005
    # 坏文件不能让归因猜一套参数出来
    (tmp_path / mga.POLICY_PATH).write_text("{ not json", encoding="utf-8")
    assert mga.load_policy_gates(tmp_path)["gate_mode"] == "edge"


# ------------------------------------------------------------------ 汇总
def test_alias_candidates_only_take_time_verified_rows(tmp_path):
    days = [{"day": "2026-11-01", "alias_evidence": [
        {"level": "time", "pool": "波鸿 vs 沃尔夫斯堡", "ours": "波鸿 vs 沃夫斯堡",
         "pool_kickoff": "2026-11-02 03:00", "competition": "德乙", "diff": ["沃尔夫斯堡→沃夫斯堡"]},
        {"level": "time", "pool": "美因茨 vs 沃尔夫斯堡", "ours": "美因茨 vs 沃夫斯堡",
         "pool_kickoff": "2026-11-02 21:30", "competition": "德甲", "diff": ["沃尔夫斯堡→沃夫斯堡"]},
        {"level": "no_time", "pool": "X vs 阿克林顿", "ours": "X vs 阿克宁顿",
         "pool_kickoff": "2026-11-02 03:00", "competition": "英锦标", "diff": ["阿克林顿→阿克宁顿"]},
    ]}]
    out = mga.alias_candidates(days)
    assert len(out) == 1, f"只该留下时间对得上的那条别名，实际 {out}"
    assert out[0]["pool_name"] == "沃尔夫斯堡" and out[0]["ours_name"] == "沃夫斯堡"
    assert out[0]["n_days"] == 1 and out[0]["n_matches"] == 2
    assert sorted(out[0]["competitions"]) == ["德乙", "德甲"]


def test_derived_ratios_and_totals(tmp_path):
    pool = [item("波鸿", "沃尔夫斯堡", "2026-11-02 03:05:00"),
            item("佩雷拉", "茨高", "2026-11-02 08:00:00")]
    preds = {DAY: [], "2026-11-02": [pred("波鸿", "沃夫斯堡", "2026-11-02 03:05:00")]}
    make_repo(tmp_path, pool, preds, policy={"gate_mode": "ev_site", "ev_floor": 0.02,
                                             "min_sporttery_odds": 0.0})
    rep = mga.build_report(tmp_path, [DAY])
    assert rep["n_days"] == 1 and rep["pool_board_total"] == 2
    assert rep["derived"]["alias_time_verified"] == 1
    assert rep["derived"]["pred_bettable"] == 1
    assert rep["alias_candidates"][0]["pool_name"] == "沃尔夫斯堡"
    assert mga.missing_snapshot_days(["2026-11-01", "2026-11-04"]) == ["2026-11-02", "2026-11-03"]
    text = mga.render_text(rep)
    assert "【关键比率】" in text and "别名可挽回" in text


def test_parse_duty_log_counts_rounds_without_echoing_secrets(tmp_path):
    """日志行只有 MM-DD：这里验证按天聚合与「≤7 轮次占比」，并确认 JWT 串不会被带出。"""
    log = tmp_path / "duty.log"
    body = "\n".join([
        "11-16 00:05:21 mint(无变化)",
        "11-16 00:05:22 challenge: 零合格（ev_site 门槛2.0%/池196/匹配5/候选5/最高-0.71%）——正常",
        "11-16 01:05:22 challenge: 零合格（ev_site 门槛2.0%/池199/匹配0/候选0）——正常",
        "11-16 02:05:22 challenge: 提交 2 注 → 10/28 ev_site 门槛2.0%（匹配6/候选4）",
        "11-17 00:05:22 challenge: 零合格（ev_site 门槛2.0%/池50/匹配3/候选1/最高+1.00%）——正常",
        "11-17 00:06:00 refresh: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpartabcde12345",
    ])
    log.write_text(body, encoding="utf-8")
    out = mga.parse_duty_log(log)
    assert out["available"] and out["days"] == 2 and out["rounds_total"] == 4
    assert out["rounds_matched_le7"] == 4
    assert out["matched_max_median"] == 6      # 偶数天取上中位：11-16 峰值 6 / 11-17 峰值 3
    # 提交行（`匹配6/候选4`）没有「池」字段：拿注数 2 冒充池容量会把 11-16 的池最大压成 2
    assert out["pool_max_median"] == 199
    import re
    assert not re.search(r"eyJ", json.dumps(out, ensure_ascii=False))


def test_parse_duty_log_reports_missing_file_as_unavailable(tmp_path):
    out = mga.parse_duty_log(tmp_path / "nope.log")
    assert out["available"] is False and "读不到" in out["reason"]
