"""断网韧性（2026-11-01）：计时/恢复提示 + challenge_state 冲突并集合并。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("duty_outage", ROOT / "scripts" / "njstats_duty.py")
duty = importlib.util.module_from_spec(SPEC)
sys.modules["duty_outage"] = duty
SPEC.loader.exec_module(duty)


def _state(week="2026-W44", used=2, records=None):
    return json.dumps({"week": week, "used": used, "quota": 28, "records": records or []},
                      ensure_ascii=False)


def test_net_fail_note_tracks_duration_and_clear_reports_it(tmp_path):
    p = tmp_path / "net_fail_state.json"
    t0 = 1_800_000_000.0
    assert "已 0.0h" in duty._net_fail_note(t0, path=p)
    later = t0 + 3 * 3600
    note = duty._net_fail_note(later, path=p)
    assert "已 3.0h" in note, note
    back = duty._net_fail_clear(later + 600, path=p)
    assert "3.2h 后恢复" in back
    assert not p.exists()
    assert duty._net_fail_clear(t0, path=p) == ""   # 没在计时就不说话


def test_merge_keeps_both_sides_records():
    remote = _state(used=3, records=[{"match": "CI注 vs 对手", "pick": "主胜@1.80"}])
    local = _state(used=2, records=[{"match": "本机注 vs 对手", "pick": "客胜@2.10",
                                     "gate": "ev_site", "ev_site": 0.03, "site_odds": 2.4}])
    merged = json.loads(duty.merge_challenge_state(remote, local))
    assert {r["match"] for r in merged["records"]} == {"CI注 vs 对手", "本机注 vs 对手"}
    assert merged["used"] == 3, "used 取大，不被任一侧抹掉"


def test_merge_prefers_richer_duplicate():
    thin = _state(records=[{"match": "A vs B", "pick": "主胜@1.80"}])
    rich = _state(records=[{"match": "A vs B", "pick": "主胜@1.80",
                            "gate": "ev_site", "ev_site": 0.04, "site_odds": 2.0}])
    merged = json.loads(duty.merge_challenge_state(thin, rich))
    assert len(merged["records"]) == 1
    assert merged["records"][0].get("ev_site") == 0.04, "同一注保留字段更全的那条"


def test_merge_returns_none_on_bad_json():
    assert duty.merge_challenge_state("{not json", _state()) is None
