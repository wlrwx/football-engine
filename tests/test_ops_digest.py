"""确定性运维摘要（2026-11-03）：把巡检/对账从模型自动化里搬出来。

重点覆盖真实踩到的几种「零合格」：匹配5/最高-0.71% 是正常的价差不足；
匹配0 还要再分两种——daily 已跑但当天无场次（体彩凌晨未开售，等下一次即可）
vs manifest 都没有（根本没跑/回仓被拦，才是数据洞）。混在一起要么天天误报、
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


# ---------- 服务端真值空快照（2026-11-08） ----------
#
# 11-04→11-07 实测：半失效的旧 JWT 下 msgId=1 全板返回 232 场，msgId=11
# 「我已预测」却静默返回 0 条。降级探测只看 board 所以照旧放行，账目行按
# 服务端条数遍历所以报「仓库未记 0 条」——四天里每轮都「无红线」。

def _flags(rows, **kw):
    base = {"missing_count": 0, "missing_hours": []}
    return ops.build_red_flags(base, {}, None, {"missing_fields": 0}, 1.0,
                               "账目: 快照 60 条/1.0h vs 仓库 60；仓库未记 0 条",
                               True, **kw)


def test_truth_check_flags_only_reported_zero_with_local_bets():
    rows = rows_of(loglines(["server-snapshot: 服务端真值 0 条而本地有 60 条注单"]))
    assert ops.truth_check(rows) == {"count": 0, "local_records": 60}
    codes = {f["code"] for f in _flags(rows, truth=ops.truth_check(rows))}
    assert "truth_snapshot_empty" in codes


def test_truth_check_uses_the_last_self_report_not_the_worst():
    """一次轮询一条，看最后一次。上午坏、下午好 → 不该再报警。"""
    bad, good = ("server-snapshot: 服务端真值 0 条而本地有 60 条注单",
                 "server-snapshot: 服务端真值 60 条")
    assert ops.truth_check(rows_of(loglines([bad, good]))) is None
    assert ops.truth_check(rows_of(loglines([good, bad])))["local_records"] == 60


def test_truth_check_ignores_clean_and_absent_reports():
    assert ops.truth_check(rows_of(loglines(["server-snapshot: 服务端真值 60 条"]))) is None
    # 本地账也是空的 = 账号真没投过，说真话，不是 JWT 半失效
    assert ops.truth_check(rows_of(loglines(["server-snapshot: 服务端真值 0 条"]))) is None
    assert ops.truth_check(rows_of(loglines(["1x2: 已推: 2 注"]))) is None


def test_truth_flag_action_is_the_human_fix_and_reaches_the_report():
    rows = rows_of(loglines(["server-snapshot: 服务端真值 0 条而本地有 61 条注单"]))
    flags = _flags(rows, truth=ops.truth_check(rows))
    hit = next(f for f in flags if f["code"] == "truth_snapshot_empty")
    assert "61" in hit["detail"]
    # 修法是用户重新登录；不能写成「改账本」或「重跑快照」这类自动可做的动作
    assert "重新登录" in hit["action"] and "别改账本" in hit["action"]
    ctx = {"rounds": {"rounds": 48, "expect": 48, "missing_count": 0, "missing_hours": []},
           "errors": {}, "state": {"week": "2026-W45", "used": 0, "quota": 28, "records": 0,
                                   "pools": {"E8": 0, "E10": 0}, "missing_fields": 0},
           "snap_age": 1.0, "reconcile": ["账目: …"], "attribution": None, "red_flags": flags}
    assert any("truth_snapshot_empty" in line for line in ops.render(ctx))


def test_duty_log_line_and_digest_regex_stay_in_lockstep(tmp_path, monkeypatch):
    """跨模块契约：红线读的是 duty 写日志的**原话**，改措辞就得同时改正则。

    这类「一边改文案、一边改解析」的漂移不会报错，只会让红线永远不响——
    所以这里用真 duty 函数产出真日志，再喂给真 ops.truth_check。
    """
    spec = importlib.util.spec_from_file_location(
        "njstats_duty_for_truth", ROOT / "scripts" / "njstats_duty.py")
    duty = importlib.util.module_from_spec(spec)
    sys.modules["njstats_duty_for_truth"] = duty
    spec.loader.exec_module(duty)

    monkeypatch.setattr(duty, "ROOT", tmp_path)
    monkeypatch.setattr(duty.api, "my_predictions", lambda *a, **k: [])
    monkeypatch.setattr(duty.api, "board_with_odds", lambda *a, **k: [])
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "challenge_state.json").write_text(
        json.dumps({"records": [{"match": "A vs B"}, {"match": "C vs D"}]}), encoding="utf-8")
    lines = []
    monkeypatch.setattr(duty, "log", lines.append)

    duty._maybe_server_records_snapshot()
    truth_line = next(l for l in lines if "server-snapshot" in l)
    rows = ops.parse_log(NOW.strftime("%m-%d %H:%M:%S ") + truth_line)
    assert ops.truth_check(rows) == {"count": 0, "local_records": 2}


# ---------- 真值快照断更（2026-11-09） ----------
#
# 11-05→11-07 的真实形状是**三天没有文件**：私有端点非 200 → my_predictions()
# 返回 None → 旧代码静默 return，既不写日志也不写文件。只盯日志的规则抓不到这种。

def _snap_dir(tmp_path, monkeypatch, files: dict):
    state = tmp_path / "state"
    d = state / "njstats" / "my_predictions"
    d.mkdir(parents=True)
    (state / "challenge_state.json").write_text("{}", encoding="utf-8")
    for name, stamp in files.items():
        (d / name).write_text(json.dumps({"snapshot_at": stamp, "count": 3}), encoding="utf-8")
    monkeypatch.setattr(ops.cr, "STATE", state)
    return d


def _flags_with_age(age, records=60, errors=None):
    return ops.build_red_flags({"missing_count": 0, "missing_hours": []}, errors or {},
                               None, {"missing_fields": 0, "records": records}, 1.0,
                               "账目: …", True, truth_age=age)


def test_truth_snapshot_age_reads_file_timestamp(tmp_path, monkeypatch):
    now = datetime(2026, 11, 9, 1, 0, tzinfo=BJT)
    _snap_dir(tmp_path, monkeypatch, {
        "2026-11-05.json": "2026-11-05 08:00:00",
        "2026-11-08.json": "2026-11-08 23:30:00",
    })
    assert round(ops.truth_snapshot_age_h(now)) == round(1.5)
    # 只看最新一份：把最新那份拿掉，年龄就退回到 11-05（89h 前）
    (tmp_path / "state" / "njstats" / "my_predictions" / "2026-11-08.json").unlink()
    assert round(ops.truth_snapshot_age_h(now)) == 89


def test_truth_snapshot_age_none_without_files_or_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.cr, "STATE", tmp_path / "empty")
    assert ops.truth_snapshot_age_h() is None
    _snap_dir(tmp_path, monkeypatch, {"2026-11-09.json": ""})
    assert ops.truth_snapshot_age_h() is None, "时间戳读不出等于不可判定，按断更处理"


def test_stale_truth_snapshot_is_flagged_when_local_bets_exist():
    codes = {f["code"] for f in _flags_with_age(50.0)}
    assert "truth_snapshot_stale" in codes
    assert "truth_snapshot_stale" in {f["code"] for f in _flags_with_age(None)}, "从未落盘同罪"
    assert "truth_snapshot_stale" not in {f["code"] for f in _flags_with_age(2.0)}


def test_stale_truth_not_flagged_without_local_bets():
    """本地没注单时，旧快照没有对账价值——这是"还没开始玩"，不是故障。"""
    assert "truth_snapshot_stale" not in {f["code"] for f in _flags_with_age(50.0, records=0)}


def test_stale_truth_is_suppressed_when_vendor_5xx_already_reported():
    """厂商 5xx 已经由 error_upstream 报了（修法是等），不要同一根因报两条。"""
    errs = {"upstream": {"count": 12, "resolved": False, "action": "等厂商恢复"}}
    codes = {f["code"] for f in _flags_with_age(50.0, errors=errs)}
    assert "error_upstream" in codes and "truth_snapshot_stale" not in codes
    # 厂商恢复后 error_upstream 不再出现，断更快照仍要被抓住
    assert "truth_snapshot_stale" in {f["code"] for f in _flags_with_age(50.0)}


# ---------- 「匹配0」的两种成因（2026-11-09） ----------
#
# 当天实测：体彩只在售 4 场，编号日期全是 11-10/11-11 → 当日竞彩无在售，
# daily 跑过（import_manifest 在）但没有 predictions.json，属正常。
# 旧代码一律报 prediction_coverage_gap 并写死「数据洞」，派单器当晚就建了张查不出故障的工单。
# 区分依据是 daily 自己的产物，不是时钟：manifest 在=跑过没产出；manifest 不在=没跑/没回仓。

def _zero_match_flags(daily):
    body = "1x2: challenge: 零合格（ev_site 门槛2.0%/池77/匹配0/候选0）——正常"
    a = ops.attribution(rows_of(loglines([body] * 48)))
    return ops.build_red_flags({"missing_count": 0, "missing_hours": []}, {}, a,
                               {"missing_fields": 0}, 1.0, "账目: 仓库未记 0 条：无", True,
                               daily=daily)


def test_daily_ran_but_no_predictions_is_pending_not_a_data_hole():
    flags = _zero_match_flags({"predictions": False, "manifest": True})
    codes = {f["code"] for f in flags}
    assert "prediction_pending" in codes
    assert "prediction_coverage_gap" not in codes, "daily 已跑过就别喊回仓被拦"
    hit = next(f for f in flags if f["code"] == "prediction_pending")
    assert "import_manifest" in hit["detail"]
    assert "无在售" in hit["action"] and "CI 是否红" in hit["action"]


def test_missing_manifest_still_is_the_real_data_hole():
    """manifest 也不在 = 根本没跑/没回仓（11-04 分支保护那次就是这个形状）。"""
    codes = {f["code"] for f in _zero_match_flags({"predictions": False, "manifest": False})}
    assert "prediction_coverage_gap" in codes and "prediction_pending" not in codes


# 2026-11-13 实测：Clash 代理进程死 → ssh 别名全失败，duty 整轮 pull/push 报
# 「Could not read from remote repository」。旧规则把 push 失败归成 credentials、
# 叫人更新 github_token——但 remote 走 SSH，token 根本无关，纯误导。
def test_push_failure_is_git_not_credentials():
    text = loglines(["1x2: challenge: 零合格（池1/匹配1/候选1/最高-1%）"] * 44
                    + ["pull 失败（未进入 rebase）: fatal: Could not read from remote repository.（本地提交待下一轮重放）",
                       "push 失败（3 次重试）: duty: challenge state 更新"] * 2)
    rows = rows_of(text)
    e = ops.error_summary(rows)
    assert "git" in e and "credentials" not in e, e
    flags = ops.build_red_flags(ops.round_summary(rows, NOW), e, None,
                                {"missing_fields": 0}, 1.0, "账目: 仓库未记 0 条：无", True)
    codes = {f["code"] for f in flags}
    assert "error_git" in codes and "error_credentials" not in codes, codes
    hit = next(f for f in flags if f["code"] == "error_git")
    assert "ssh -T" in hit["action"] and "别先改 token" in hit["action"]


def test_real_auth_failure_still_credentials():
    text = loglines(["1x2: challenge: 零合格（池1/匹配1/候选1/最高-1%）"] * 44
                    + ["ci-sync: Bad credentials"] * 4)
    e = ops.error_summary(rows_of(text))
    assert "credentials" in e and "git" not in e, e


def test_predictions_exist_but_zero_overlap_is_not_a_data_hole():
    """2026-11-10 实例：预测已产出（2 场），但站点池里写的是「诺斯郡」、我们写「诺茨郡」，
    别名漏一条 → 零命中。这是「没对上」，既不是没产出，也不是数据洞。"""
    flags = _zero_match_flags({"predictions": True, "manifest": True})
    codes = {f["code"] for f in flags}
    assert "pool_no_overlap" in codes
    assert not ({"prediction_pending", "prediction_coverage_gap"} & codes)
    hit = next(f for f in flags if f["code"] == "pool_no_overlap")
    assert "译名" in hit["action"] and "诺茨郡" in hit["action"]
    assert "别改门槛" in hit["action"]


def test_pending_flag_reaches_the_report():
    flags = _zero_match_flags({"predictions": False, "manifest": True})
    ctx = {"rounds": {"rounds": 48, "expect": 48, "missing_count": 0, "missing_hours": []},
           "errors": {}, "state": {"week": "2026-W46", "used": 0, "quota": 28, "records": 0,
                                   "pools": {"E8": 0, "E10": 0}, "missing_fields": 0},
           "snap_age": 1.0, "reconcile": ["账目: …"], "attribution": None, "red_flags": flags}
    assert any("prediction_pending" in line for line in ops.render(ctx))


# ---------- 登录态提前预警（2026-11-09） ----------
#
# 被动续期要等 mint 失败才跑，那时缓存 JWT 只剩 ≤20h。主动续期每 12h 试一次，
# 只在「登录态真的无法自动续期」（auth_dead）时提前报——厂商 5xx 不报（已有上游红线）。

def _health(tmp_path, monkeypatch, payload):
    p = tmp_path / "njstats_session_health.json"
    if payload is not None:
        p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ops, "SESSION_HEALTH", p)


def test_session_health_only_surfaces_auth_dead(tmp_path, monkeypatch):
    _health(tmp_path, monkeypatch, {"result": "auth_dead", "at": "2026-11-09 19:00:00",
                                    "detail": "HTTP 401"})
    assert ops.session_health()["result"] == "auth_dead"
    for payload in ({"result": "vendor", "at": "x", "detail": "HTTP 502"},
                    {"result": "ok", "at": "x", "detail": "已续期"}, None):
        _health(tmp_path, monkeypatch, payload)
        assert ops.session_health() is None, payload


def test_session_expiring_flag_says_seed_is_not_urgent_but_do_it():
    flags = ops.build_red_flags(
        {"missing_count": 0, "missing_hours": []}, {}, None, {"missing_fields": 0},
        1.0, "账目: …", True,
        session_dead={"result": "auth_dead", "at": "2026-11-09 19:00:00", "detail": "HTTP 401"})
    hit = next(f for f in flags if f["code"] == "njstats_session_expiring")
    assert "2026-11-09 19:00:00" in hit["detail"]
    assert "./fe-agent seed" in hit["action"]
    assert "20h" in hit["action"] and "厂商 5xx" in hit["action"]


def test_no_session_flag_when_healthy():
    codes = {f["code"] for f in ops.build_red_flags(
        {"missing_count": 0, "missing_hours": []}, {}, None, {"missing_fields": 0},
        1.0, "账目: …", True, session_dead=None)}
    assert "njstats_session_expiring" not in codes
