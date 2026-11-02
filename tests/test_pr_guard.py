"""Gate for external PRs (added 2026-11-02, before the dsh-superagent 小号 gets write).

dsh-superagent's own docs say merge behaviour is only an agent *instruction*, not
enforced policy, and its setup requires the agent account to have push access —
so Write is unavoidable and the path rules have to live in CI.
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("pr_guard", ROOT / "scripts" / "pr_guard.py")
guard = importlib.util.module_from_spec(SPEC)
sys.modules["pr_guard"] = guard
SPEC.loader.exec_module(guard)


def test_ordinary_change_passes():
    assert guard.decide(["engine/main.py", "tests/test_main.py"], [], "随便写写") == []


def test_agent_author_never_touches_referee_layer():
    # 带 human-approved 也不行：标签是 Write 账号自己加得动的东西。
    for labels in ([], [guard.HUMAN_LABEL]):
        bad = guard.decide(["data/state/challenge_state.json"], labels, "x", author="dykily")
        assert bad, f"标签={labels} 时自动化身份竟然被放行"
        assert "必须由人提 PR" in bad[0]


def test_human_needs_label_for_referee_layer():
    files = ["data/state/review_ledger.jsonl"]
    assert guard.decide(files, [], "x", author="wlrwx")
    assert guard.decide(files, [guard.HUMAN_LABEL], "x", author="wlrwx") == []


def test_directory_prefix_counts():
    bad = guard.decide(["engine/backtest/scoring.py"], [], "拔除影响评估：无", author="wlrwx")
    assert bad and "engine/backtest/scoring.py" in bad[0]


def test_challenge_subsystem_needs_impact_section():
    files = ["scripts/challenge_hourly.py"]
    assert guard.decide(files, [], "修了个小 bug", author="wlrwx")
    assert guard.decide(files, [], "## 拔除影响评估\n只动策略层", author="wlrwx") == []


def test_the_gate_guards_itself():
    # pr-guard.yml / njstats_duty.py 都在 CHALLENGE 名单里：闸门改自己要留痕。
    assert guard.decide([".github/workflows/pr-guard.yml"], [guard.HUMAN_LABEL], "无", author="wlrwx")
    assert guard.decide(["scripts/njstats_duty.py"], [guard.HUMAN_LABEL], guard.SECTION, author="wlrwx") == []


def test_cli_exit_code_reflects_verdict(tmp_path):
    ok = tmp_path / "ok.txt"
    ok.write_text("engine/main.py\n", encoding="utf-8")
    assert guard.main(["--files-from", str(ok), "--labels-json", "[]", "--author", "wlrwx"]) == 0

    bad = tmp_path / "bad.txt"
    bad.write_text("\n".join(["data/state/challenge_policy.json", "scripts/njstats_duty.py"]) + "\n",
                   encoding="utf-8")
    rc = guard.main(["--files-from", str(bad),
                     "--labels-json", json.dumps([guard.HUMAN_LABEL]),
                     "--body-file", str(bad), "--author", "dykily"])
    assert rc == 1, "自动化改策略文件却没有被 CI 拦住"


def test_author_match_is_case_insensitive():
    assert guard.decide(["docs/EXPERIMENTS.md"], [], guard.SECTION, author="Dykily")
