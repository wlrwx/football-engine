"""duty 提交链的回归测试（2026-10-22）。

钉的是这次审计查出来的实际缺陷：`njstats_duty.py` 里那段内联 commit 只认
`data/state/challenge_state.json` 一个文件，而 duty 每天另外写的几份快照
（site_odds / my_predictions / community，2026-11-08 起再加 strategies）
**从来没被提交过**。后果不是"少一个备份"，是站点赔率样本只活在笔记本工作区
——而站点赔率快照存在的唯一理由就是攒够样本做配对回放
（docs/CHALLENGE_ODDS_GAP_20261021.md）。
已知坑②「git reset --hard 清归档」已经发生过两次。
所以这里的重点不只是「名单里有四个目录」，而是**任何脚本往 njstats/ 下写快照，
就必须出现在名单里**（用源码扫描兜住，见 test_every_njstats_snapshot_dir）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def _load_duty():
    spec = importlib.util.spec_from_file_location(
        "njstats_duty", ROOT / "scripts" / "njstats_duty.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["njstats_duty"] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_sh(status_out="", push_rc=0, calls=None):
    def sh(*args):
        if calls is not None:
            calls.append(args)
        out = status_out if args[:3] == ("git", "status", "--short") else ""
        rc = push_rc if args[:2] == ("git", "push") else 0
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")
    return sh


def test_snapshot_paths_are_all_staged(monkeypatch):
    duty = _load_duty()
    calls: list[tuple] = []
    monkeypatch.setattr(duty, "sh", _fake_sh(
        " M data/state/njstats/site_odds/x.json", calls=calls))
    monkeypatch.setattr(duty, "log", lambda m: None)
    monkeypatch.setattr(duty.time, "sleep", lambda s: None)

    assert duty.commit_push(duty.SNAPSHOT_PATHS, "duty: 快照") is True
    add = next(c for c in calls if c[:2] == ("git", "add"))
    for p in duty.SNAPSHOT_PATHS:
        assert p in add, f"{p} 没进提交链——这正是 2026-10-22 修掉的缺陷"
    assert ("git", "push", "origin", "main") in calls


def test_every_njstats_snapshot_dir_is_committed():
    """名单必须覆盖**所有**往 data/state/njstats/<x>/ 写盘的脚本，不只 duty 自己。

    原先这里手写三个目录名，第四份快照（strategies/）进来时它照样绿——
    这正是 2026-10-22 那个缺陷的形状：写的人加了目录，提交链没人加。
    所以改成扫源码：以后任何脚本新增一份 njstats 快照，这条会立刻要求
    它进 SNAPSHOT_PATHS，否则就是「样本只活在笔记本上」。
    """
    duty = _load_duty()
    dirs = set()
    for p in sorted((ROOT / "scripts").glob("*.py")):
        src = p.read_text(encoding="utf-8", errors="ignore")
        dirs |= set(re.findall(r'ROOT / "data" / "state" / "njstats" / "([a-z_]+)"', src))
        dirs |= set(re.findall(r"data/state/njstats/([a-z_]+)/", src))
    listed = {d.rstrip("/").rsplit("/", 1)[-1] for d in duty.SNAPSHOT_PATHS}
    assert {"site_odds", "my_predictions", "community", "strategies"} <= listed
    assert dirs - listed == set(), f"这些快照目录没人提交：{sorted(dirs - listed)}"


def test_clean_tree_does_not_commit(monkeypatch):
    duty = _load_duty()
    calls: list[tuple] = []
    monkeypatch.setattr(duty, "sh", _fake_sh("", calls=calls))
    monkeypatch.setattr(duty, "log", lambda m: None)
    assert duty.commit_push(duty.STATE_PATHS, "duty: state") is True
    assert not [c for c in calls if c[:2] in (("git", "add"), ("git", "commit"))], \
        "零合格轮不该制造噪音提交（这条纪律原先写在 challenge_hourly 的注释里）"


def test_main_wires_the_snapshot_commit(monkeypatch, tmp_path):
    """链路测试：duty 的 main() 真的要用 SNAPSHOT_PATHS 调一次 commit_push。

    单独测 commit_push 只能证明这个函数会干活。本项目的头号病害是
    「写了却从未被调用」（PROJECT.md 2026-10-13 扫出 7 个），所以这条
    接线必须单独钉住——把 main() 里的 commit_push 换成 recorder 驱动一遍。
    """
    duty = _load_duty()
    calls: list[list[str]] = []
    snapshot_steps: list[str] = []
    monkeypatch.setattr(duty, "sh", _fake_sh(""))
    monkeypatch.setattr(duty, "log", lambda m: None)
    monkeypatch.setattr(duty, "mint", lambda: "fake-jwt")
    monkeypatch.setattr(duty, "push_secret", lambda t: None)
    monkeypatch.setattr(duty, "JWT_FILE", tmp_path / "njstats_jwt")
    for name in ("_maybe_server_records_snapshot", "_maybe_community_snapshot",
                 "_maybe_strategy_snapshot", "_maybe_djyy_snapshot",
                 "_maybe_dispatch_daily_ci", "_maybe_dispatch_ci"):
        monkeypatch.setattr(duty, name, lambda _n=name: snapshot_steps.append(_n))

    def rec_commit(paths, msg):
        calls.append(list(paths))
        return True
    monkeypatch.setattr(duty, "commit_push", rec_commit)
    monkeypatch.setattr(duty.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="challenge: 零合格（edge 门槛1.5%/池236）——正常", stderr="", returncode=0))
    for flag in ("DUTY_SKIP_SERVER_SNAPSHOT", "DUTY_SKIP_COMMUNITY",
                 "DUTY_SKIP_STRATEGIES", "DUTY_SKIP_CI_WATCHDOG"):
        monkeypatch.delenv(flag, raising=False)

    assert duty.main() == 0
    assert duty.SNAPSHOT_PATHS in calls, f"main() 没提交快照，实际调用：{calls}"
    assert duty.STATE_PATHS in calls, "state 的提交链不能因为重构而丢"
    # 「函数写了但没人调」是本项目的头号病害：每个采集步都得被 main 真的调到
    assert {"_maybe_server_records_snapshot", "_maybe_community_snapshot",
            "_maybe_strategy_snapshot"} <= set(snapshot_steps), snapshot_steps
    assert "_maybe_djyy_snapshot" in snapshot_steps


def test_strategy_snapshot_can_be_skipped_by_env(monkeypatch, tmp_path):
    """留一个跳过开关（与其它快照一致），但默认必须开——关掉就等于不再攒样本。"""
    duty = _load_duty()
    steps = []
    monkeypatch.setattr(duty, "sh", _fake_sh(""))
    monkeypatch.setattr(duty, "log", lambda m: None)
    monkeypatch.setattr(duty, "mint", lambda: "fake-jwt")
    monkeypatch.setattr(duty, "push_secret", lambda t: None)
    monkeypatch.setattr(duty, "JWT_FILE", tmp_path / "njstats_jwt")
    monkeypatch.setattr(duty.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="ok", stderr="", returncode=0))
    for name in ("_maybe_server_records_snapshot", "_maybe_community_snapshot",
                 "_maybe_djyy_snapshot", "_maybe_ops_digest", "_maybe_agent_dispatch",
                 "_maybe_dispatch_daily_ci", "_maybe_dispatch_ci"):
        monkeypatch.setattr(duty, name, lambda _n=name: steps.append(_n))
    monkeypatch.setattr(duty, "_maybe_strategy_snapshot",
                        lambda: steps.append("_maybe_strategy_snapshot"))
    monkeypatch.setenv("DUTY_SKIP_STRATEGIES", "1")
    for flag in ("DUTY_SKIP_SERVER_SNAPSHOT", "DUTY_SKIP_COMMUNITY", "DUTY_SKIP_CI_WATCHDOG"):
        monkeypatch.delenv(flag, raising=False)

    assert duty.main() == 0
    assert "_maybe_strategy_snapshot" not in steps, "开关生效时不该调用采集"


def test_push_failure_is_visible_and_retried(monkeypatch):
    duty = _load_duty()
    calls: list[tuple] = []
    logged: list[str] = []
    monkeypatch.setattr(duty, "sh", _fake_sh(" M x", calls=calls, push_rc=1))
    monkeypatch.setattr(duty, "log", logged.append)
    monkeypatch.setattr(duty.time, "sleep", lambda s: None)

    assert duty.commit_push(duty.SNAPSHOT_PATHS, "duty: 快照") is False
    assert len([c for c in calls if c[:2] == ("git", "push")]) == 3, "重试三次"
    assert any("push 失败" in m for m in logged), f"失败必须可见，实际日志：{logged}"


# ---------- DJYY 快照（2026-10-22） ----------
#
# 上游对 Worker/CI 出口整体 challenge，只有本机住宅网络能直连。把抓取放在
# duty 里是唯一不依赖第三方代理的路径；CI 消费仓库里的快照。

def _stale_djyy(tmp_path, hours=48):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    (d / "djyy_matches.json").write_text(json.dumps({
        "extracted_at": ts.isoformat(), "with_odds": 3, "matches": [],
    }), encoding="utf-8")


def test_djyy_snapshot_fetched_directly_when_stale(tmp_path, monkeypatch):
    duty = _load_duty()
    _stale_djyy(tmp_path)
    monkeypatch.setattr(duty, "ROOT", tmp_path)
    monkeypatch.setattr(duty, "DJYY_MARKER", tmp_path / "marker")
    monkeypatch.setattr(duty, "log", lambda m: None)
    runs, commits = [], []
    monkeypatch.setattr(duty.subprocess, "run", lambda *a, **k: (
        runs.append(k), SimpleNamespace(returncode=0, stdout="  ✓ 42 matches", stderr=""))[1])
    monkeypatch.setattr(duty, "commit_push", lambda p, m: commits.append((p, m)) or True)

    duty._maybe_djyy_snapshot()

    assert runs, "快照陈旧时必须发起抓取"
    assert runs[0]["env"]["DJYY_PROXY"] == "", "必须清空代理走本机直连"
    assert "DJYY_KEY" not in runs[0]["env"], "本机直连不需要也不该带 Worker key"
    assert commits == [([duty.DJYY_PATH], "duty: DJYY 快照（本机住宅网络抓取）")]


def test_djyy_snapshot_fresh_is_skipped(tmp_path, monkeypatch):
    duty = _load_duty()
    _stale_djyy(tmp_path, hours=1)
    monkeypatch.setattr(duty, "ROOT", tmp_path)
    monkeypatch.setattr(duty, "DJYY_MARKER", tmp_path / "marker")
    runs = []
    monkeypatch.setattr(duty.subprocess, "run", lambda *a, **k: runs.append(1))

    duty._maybe_djyy_snapshot()

    assert not runs, "10h 内的快照不该重复抓"


def test_djyy_failed_fetch_is_rate_limited(tmp_path, monkeypatch):
    duty = _load_duty()
    _stale_djyy(tmp_path)
    monkeypatch.setattr(duty, "ROOT", tmp_path)
    monkeypatch.setattr(duty, "DJYY_MARKER", tmp_path / "marker")
    monkeypatch.setattr(duty, "log", lambda m: None)
    runs, commits = [], []
    monkeypatch.setattr(duty.subprocess, "run", lambda *a, **k: (
        runs.append(1), SimpleNamespace(returncode=1, stdout="", stderr="HTTP 403"))[1])
    monkeypatch.setattr(duty, "commit_push", lambda p, m: commits.append(p) or True)

    duty._maybe_djyy_snapshot()
    duty._maybe_djyy_snapshot()

    assert len(runs) == 1, "失败后 2h 内不应反复请求上游"
    assert not commits, "没拿到新数据就不该提交"


# ---------- 回测策略快照 + 服务端真值空快照（2026-11-08） ----------


def _strategy_duty(tmp_path, monkeypatch, runs=None, rc=0, stdout="strategies: base=5 mine=0"):
    duty = _load_duty()
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(duty, "ROOT", tmp_path)
    log_lines = []
    monkeypatch.setattr(duty, "log", log_lines.append)

    def fake_run(cmd, **kw):
        if runs is not None:
            runs.append((cmd, kw))
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")
    monkeypatch.setattr(duty.subprocess, "run", fake_run)
    return duty, log_lines


def test_strategy_snapshot_runs_once_per_day(tmp_path, monkeypatch):
    runs = []
    duty, _ = _strategy_duty(tmp_path, monkeypatch, runs)
    duty._maybe_strategy_snapshot()
    duty._maybe_strategy_snapshot()
    assert len(runs) == 1, "marker 必须按日期门控，不然每 30 分钟抓一次全量重算"
    assert (tmp_path / "data" / "state" / ".njstats_strategy_done").read_text(
        encoding="utf-8").strip() == time.strftime("%Y-%m-%d")


def test_strategy_snapshot_calls_the_fetcher_with_a_bounded_timeout(tmp_path, monkeypatch):
    """钉住「调的是哪个脚本 + 超时」：改错脚本名只会留下一条没人看的日志。"""
    runs = []
    duty, _ = _strategy_duty(tmp_path, monkeypatch, runs)
    duty._maybe_strategy_snapshot()
    cmd, kw = runs[0]
    assert cmd[-1] == "scripts/fetch_njstats_strategies.py", cmd
    assert kw["timeout"] <= 180 and kw["cwd"] is duty.ROOT


def test_failed_strategy_snapshot_leaves_marker_for_retry(tmp_path, monkeypatch):
    """取数失败不写 marker（下一轮重试），但日志要留下原因——store-only 的东西
    失败一周没人发现，就等于这一周的数据永久没了。"""
    runs = []
    duty, lines = _strategy_duty(tmp_path, monkeypatch, runs, rc=1, stdout="")
    duty._maybe_strategy_snapshot()
    assert not (tmp_path / "data" / "state" / ".njstats_strategy_done").exists()
    assert lines == ["strategies: 无输出 rc=1"], lines
    duty._maybe_strategy_snapshot()
    assert len(runs) == 2


def _truth_duty(tmp_path, monkeypatch, recs, board=None, local=None):
    duty = _load_duty()
    monkeypatch.setattr(duty, "ROOT", tmp_path)
    monkeypatch.setattr(duty.api, "my_predictions", lambda *a, **k: recs)
    monkeypatch.setattr(duty.api, "board_with_odds",
                        lambda *a, **k: board if board is not None else [])
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "challenge_state.json").write_text(json.dumps(
        {"records": [{"match": m} for m in (local or [])]}), encoding="utf-8")
    lines: list[str] = []
    monkeypatch.setattr(duty, "log", lines.append)
    return duty, lines


def _truth_file(tmp_path):
    d = tmp_path / "data" / "state" / "njstats" / "my_predictions" / (
        time.strftime("%Y-%m-%d") + ".json")
    return d


def test_empty_server_truth_does_not_overwrite_a_fuller_snapshot(tmp_path, monkeypatch):
    """半失效 JWT：msgId=1 全板正常、msgId=11 静默返回 0 条。

    覆盖当天快照会把上午的真值清成空表，而对账行按服务端条数遍历——
    于是「仓库未记 0 条」，看起来一切正常（11-04→11-07 实测四天无红线）。
    """
    day = _truth_file(tmp_path)
    day.parent.mkdir(parents=True, exist_ok=True)
    day.write_text(json.dumps({"count": 60, "records": [{"a": 1}] * 60}), encoding="utf-8")
    duty, lines = _truth_duty(tmp_path, monkeypatch, recs=[], local=[f"m{i} vs n{i}" for i in range(60)])

    duty._maybe_server_records_snapshot()

    assert json.loads(day.read_text(encoding="utf-8"))["count"] == 60, "旧真值不能被空返回覆盖"
    assert any("服务端真值 0 条而本地有 60 条注单" in m for m in lines), lines


def test_empty_server_truth_alongside_odds_snapshot_still_runs(tmp_path, monkeypatch):
    """真值为空时，站点赔率快照照旧要写——早退一次就少一天赔率样本。"""
    duty, _ = _truth_duty(tmp_path, monkeypatch, recs=[], local=["A vs B"],
                          board=[{"sid": "s1", "home": "A", "away": "B",
                                  "kick_ts": 1, "odds": {"1": 1.9}}])
    duty._maybe_server_records_snapshot()
    odds = tmp_path / "data" / "state" / "njstats" / "site_odds" / (
        time.strftime("%Y-%m-%d") + ".json")
    assert json.loads(odds.read_text(encoding="utf-8"))["with_odds"] == 1


def test_genuine_empty_account_still_writes_a_snapshot(tmp_path, monkeypatch):
    """本地也没注单时，0 条是真话不是故障：照常落盘，并带上 local_records 供对照。"""
    duty, lines = _truth_duty(tmp_path, monkeypatch, recs=[], local=[])
    duty._maybe_server_records_snapshot()
    body = json.loads(_truth_file(tmp_path).read_text(encoding="utf-8"))
    assert body == {**body, "count": 0, "local_records": 0}
    assert any("server-snapshot: 服务端真值 0 条" == m for m in lines), lines


def test_server_truth_snapshot_carries_both_sides(tmp_path, monkeypatch):
    duty, lines = _truth_duty(tmp_path, monkeypatch, recs=[{"sid": "a"}, {"sid": "b"}],
                              local=["A vs B"])
    duty._maybe_server_records_snapshot()
    body = json.loads(_truth_file(tmp_path).read_text(encoding="utf-8"))
    assert body["count"] == 2 and body["local_records"] == 1
    assert "server-snapshot: 服务端真值 2 条" in lines


def test_unusable_jwt_leaves_snapshots_untouched(tmp_path, monkeypatch):
    """取不到（None）和取到空表是两件事：前者不写也不喊，交给 mint 侧报。"""
    duty, lines = _truth_duty(tmp_path, monkeypatch, recs=None, local=["A vs B"])
    duty._maybe_server_records_snapshot()
    assert not _truth_file(tmp_path).exists()
    assert lines == []
