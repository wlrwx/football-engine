"""自动派单器（2026-11-04）：只派可安全自动修的类别，去重限流，红线只报给人。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("agent_dispatch", ROOT / "scripts" / "agent_dispatch.py")
disp = importlib.util.module_from_spec(SPEC)
sys.modules["agent_dispatch"] = disp
SPEC.loader.exec_module(disp)

TODAY = "2026-11-04"


def test_only_safe_codes_are_dispatchable():
    flags = [{"code": c, "detail": "d", "action": "a"} for c in
             ("prediction_coverage_gap", "snapshot_not_committed", "today_site_odds_missing",
              "account_gap", "record_fields_missing", "zero_qualify_unexplained",
              "error_mint", "error_credentials", "duty_missing_hours")]
    got = {c["key"] for c in disp.candidates_from_flags(flags)}
    assert got == {"flag:prediction_coverage_gap", "flag:snapshot_not_committed",
                   "flag:today_site_odds_missing"}, got


def test_every_digest_red_flag_code_is_classified():
    # 派单器必须对 ops_digest 会产出的每个 code 都有明确归属，不能漏成「默默不派」。
    # error_* 直接从 ops_digest.ERROR_RULES 推导：以后加症状忘了分类，这条会红。
    spec = importlib.util.spec_from_file_location("ops_digest_for_dispatch",
                                                  ROOT / "scripts" / "ops_digest.py")
    od = importlib.util.module_from_spec(spec)
    sys.modules["ops_digest_for_dispatch"] = od
    spec.loader.exec_module(od)
    known = {f"error_{code}" for code, _pat, _act in od.ERROR_RULES} | {
        "prediction_coverage_gap", "zero_qualify_unexplained", "snapshot_not_committed",
        "today_site_odds_missing", "account_gap", "record_fields_missing", "duty_missing_hours",
        "truth_snapshot_empty"}
    assert known <= (set(disp.DISPATCHABLE) | set(disp.HUMAN_ONLY)), \
        f"未分类: {known - (set(disp.DISPATCHABLE) | set(disp.HUMAN_ONLY))}"
    assert not (set(disp.DISPATCHABLE) & set(disp.HUMAN_ONLY)), "同一 code 不能既派又不派"
    assert "error_branch" in disp.HUMAN_ONLY, "分支被切走是 agent 越界造成的，不能派给它自己修"


def test_ci_only_watched_failures_and_latest_per_workflow():
    runs = [
        {"name": "daily-forecast.yml", "path": ".github/workflows/daily-forecast.yml",
         "conclusion": "failure", "run_number": 1630, "created_at": "2026-11-03T00:35:27Z",
         "html_url": "u1630", "event": "schedule", "head_sha": "a" * 40},
        {"name": "daily-forecast.yml", "path": ".github/workflows/daily-forecast.yml",
         "conclusion": "failure", "run_number": 1629, "created_at": "2026-11-02T20:20:44Z",
         "html_url": "u1629", "event": "schedule", "head_sha": "b" * 40},
        {"name": "别的 workflow", "path": ".github/workflows/other.yml",
         "conclusion": "failure", "run_number": 9, "created_at": "2026-11-03T00:00:00Z"},
    ]
    got = disp.candidates_from_ci(runs)
    assert len(got) == 1, got
    assert got[0]["key"] == "ci:daily-forecast.yml:1630"
    assert "u1630" in got[0]["evidence"]


def test_already_fixed_old_failure_is_not_dispatched():
    # 最近一次是绿的就不派，否则会让 agent 去修一个已经修好的问题
    runs = [
        {"name": "health-check.yml", "path": ".github/workflows/health-check.yml",
         "conclusion": "failure", "run_number": 1244, "created_at": "2026-11-03T10:15:51Z"},
        {"name": "health-check.yml", "path": ".github/workflows/health-check.yml",
         "conclusion": "success", "run_number": 1245, "created_at": "2026-11-04T02:00:00Z"},
    ]
    assert disp.candidates_from_ci(runs) == []


def test_in_progress_run_is_not_a_failure():
    runs = [{"name": "test.yml", "path": ".github/workflows/test.yml",
             "conclusion": None, "run_number": 7, "created_at": "2026-11-04T02:00:00Z"}]
    assert disp.candidates_from_ci(runs) == []


def test_dedup_same_key_same_day_and_daily_limit():
    st = {}
    assert disp.due(st, "k1", TODAY)
    st = disp.record(st, "k1", TODAY, "url1")
    assert not disp.due(st, "k1", TODAY), "同一 key 当天重复派单"
    assert disp.due(st, "k2", TODAY)
    st = disp.record(st, "k2", TODAY, "url2")
    st = disp.record(st, "k3", TODAY, "url3")
    assert not disp.due(st, "k4", TODAY, limit=3), "超过每日上限仍在派"
    assert disp.due(st, "k1", "2026-11-05"), "跨天应允许再派"
    assert disp.due(st, "k4", "2026-11-05", limit=3), "跨天计数应重置"


def test_issue_body_carries_scope_and_red_lines():
    title, body = disp.render_issue({"key": "ci:x:1", "kind": "ci_failure",
                                     "title": "上游 CI 失败：x #1", "evidence": "http://evidence"})
    assert title.startswith("[自动派单]")
    for must in ("@dykily", "http://evidence", "wlrwx/football-engine", "不要合并任何 PR",
                 "不得给 main 加分支保护", "review_ledger.jsonl", "行为测试", "停下来只报告"):
        assert must in body, f"派单正文缺少 {must}"


def test_dry_run_creates_nothing_and_writes_no_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(disp, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(disp, "DIGEST_FILE", tmp_path / "digest.json")
    monkeypatch.setattr(disp, "TOKEN_FILE", tmp_path / "no-token")
    (tmp_path / "digest.json").write_text(json.dumps({"red_flags": [
        {"code": "prediction_coverage_gap", "detail": "匹配0", "action": "查 CI 回仓"},
        {"code": "account_gap", "detail": "漏记 5 条", "action": "人工核对"}]}), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("dry-run 竟然去建 issue / 查网络")
    monkeypatch.setattr(disp, "create_issue", boom)
    assert disp.main(["--dry-run", "--no-ci"]) == 0
    out = capsys.readouterr().out
    assert "将建 issue" in out and "只报给人的红线" in out and "account_gap" in out
    assert not (tmp_path / "state.json").exists(), "dry-run 不应写状态"


def test_missing_token_does_not_crash(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(disp, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(disp, "DIGEST_FILE", tmp_path / "digest.json")
    monkeypatch.setattr(disp, "TOKEN_FILE", tmp_path / "nope")
    assert disp.main([]) == 0
    assert "无 GitHub token" in capsys.readouterr().out


def test_scout_rotates_and_is_stable_within_a_day():
    a = disp.scout_candidate("2026-11-04")
    assert a == disp.scout_candidate("2026-11-04"), "同一天每 30 分钟跑一次却换题了"
    assert a["kind"] == disp.SCOUT_KIND and a["key"].startswith("scout:2026-11-04:")
    days = [(disp.scout_candidate(f"2026-11-{d:02d}")["focus"]) for d in range(4, 10)]
    assert len(set(days)) == len(disp.SCOUT_FOCUS), f"6 天没轮完全部主题: {days}"


def test_scout_has_independent_budget():
    st = {}
    for k in ("k1", "k2", "k3"):
        st = disp.record(st, k, TODAY, "u")
    assert not disp.due(st, "k4", TODAY, limit=3), "故障额度已满"
    assert disp.scout_due(st, TODAY), "探索工单不该被故障额度挤掉"
    st = disp.record_scout(st, TODAY, "scout-url")
    assert not disp.scout_due(st, TODAY), "同一天派了第二条探索工单"
    assert disp.scout_due(st, "2026-11-05")


def test_scout_issue_is_investigation_not_a_gate_change():
    c = disp.scout_candidate(TODAY)
    title, body = disp.render_issue(c)
    assert title.startswith("[每日探索]")
    for must in ("只读量化", "docs/proposals/", "不要改 `docs/EXPERIMENTS.md`",
                 "不得改模型特征", "本轮无可行动发现", "不要合并任何 PR",
                 "禁止在 `/Users/dykily/football-engine`"):
        assert must in body, f"探索工单缺少边界: {must}"


def test_no_scout_flag_suppresses_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(disp, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(disp, "DIGEST_FILE", tmp_path / "digest.json")
    monkeypatch.setattr(disp, "TOKEN_FILE", tmp_path / "nope")
    assert disp.main(["--dry-run", "--no-ci", "--no-scout"]) == 0
    assert "探索" not in capsys.readouterr().out


def _pr(**over):
    d = {"number": 80, "title": "泄漏审计", "html_url": "https://x/pr/80",
         "head_sha": "a" * 40, "failed_checks": ["guard", "test (ubuntu-latest)"]}
    d.update(over)
    return d


def test_pr_feedback_only_red_prs_and_keyed_by_sha():
    got = disp.candidates_from_prs([_pr(), _pr(number=81, failed_checks=[])])
    assert len(got) == 1, got
    assert got[0]["key"] == "prfix:80:aaaaaaaa" and got[0]["kind"] == disp.PR_FIX_KIND
    assert "guard" in got[0]["evidence"]
    # 推了新提交就是新 key，不会被旧失败的去重挡住
    assert disp.candidates_from_prs([_pr(head_sha="c" * 40)])[0]["key"] != got[0]["key"]


def test_pr_fix_issue_requires_local_verification():
    title, body = disp.render_issue(disp.candidates_from_prs([_pr()])[0])
    assert title.startswith("[PR 修复]")
    for must in ("原来那个 fork 分支", "ruff check --select F", "不要绕过闸门",
                 "F841", "不要合并任何 PR", "按结论改正"):
        assert must in body, f"PR 修复单缺少 {must}"


def test_no_pr_feedback_flag_skips_query(tmp_path, monkeypatch):
    monkeypatch.setattr(disp, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(disp, "DIGEST_FILE", tmp_path / "digest.json")
    (tmp_path / "tok").write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(disp, "TOKEN_FILE", tmp_path / "tok")
    monkeypatch.setattr(disp, "fetch_ci_runs", lambda t: [])

    def boom(t):
        raise AssertionError("--no-pr-feedback 竟然还去查 PR")
    monkeypatch.setattr(disp, "fetch_agent_prs", boom)
    assert disp.main(["--dry-run", "--no-pr-feedback"]) == 0
