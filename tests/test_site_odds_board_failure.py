"""站点赔率快照断供的取证与归因（2026-11-27，工单 issue #29）。

真实形状（不是编的）：2026-11-26 21:35 起厂商 msgId=1 全板在 **HTTP 200** 里返回
应用级错误，实测原文只有 27 字节：

    b'\\x08\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\x01\\x12\\x0e系统错误!!'
    = f1=-1（哨兵） + f2="系统错误!!"，没有 f5 场次容器

`parse_board_with_odds()` 对它的返回值和「今天就是没比赛」完全一样：`[]`。
于是 `njstats_duty.py` 的 `if not board: return` 一个字都不写，duty.log 里
odds-snapshot 行凭空消失三小时无人知晓；北京日翻篇后 00:05 那轮摘要只剩一句
「今天的站点赔率快照不在仓库里 → 查 duty 是否跑过、是否推成功」，
把一条厂商侧故障派成了查 git 链路的 agent 工单。

这里钉四层，全部真跑函数（不做源码文本断言）：
  ① 取数层能把「200 里的错误」和「空板」分开说；
  ② duty 的失败轮必须留字，且不许覆盖当天旧快照；
  ③ 摘要按 duty 写的**原话**认出厂商侧故障（跨模块锁步）；
  ④ 派单器对这个 code 不派 agent，而真正的提交链故障照旧派。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BJT = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 27, 0, 40, tzinfo=BJT)

# 2026-11-27 00:1x 从生产机抓的真实响应原文（无任何凭据：f1/f2 都是厂商字段）
ERR_ENVELOPE = (b"\x08\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01"
                b"\x12\x0e\xe7\xb3\xbb\xe7\xbb\x9f\xe9\x94\x99\xe8\xaf\xaf!!")


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load("njstats_api_submit_board", "scripts/njstats_api_submit.py")
ops = _load("ops_digest_board", "scripts/ops_digest.py")


# ---------- ① 取数层 ----------

def test_error_envelope_parses_as_empty_board():
    """先把事实钉住：这个响应的板子是空的——旧代码正是因此分不出两种故障。"""
    assert ERR_ENVELOPE.endswith("系统错误!!".encode())
    assert len(ERR_ENVELOPE) == 27
    assert api.parse_board_with_odds(ERR_ENVELOPE) == []


def test_board_response_error_reads_in_band_error():
    assert api.board_response_error(ERR_ENVELOPE) == "系统错误!!"


def test_board_response_error_says_nothing_about_a_good_board():
    """成功响应的 f2 是 "suc"，不能被当成错误（否则每天误报厂商故障）。"""
    assert api.board_response_error(b"\x12\x03suc\x2a\x00") == ""
    assert api.board_response_error(b"") == ""
    assert api.board_response_error(b"\xff\xff\xff\xff") == ""     # 解析不动就闭嘴


def test_board_fetch_returns_the_reason_not_just_an_empty_list(monkeypatch):
    """(场次, 原因) 双返回值：采集侧要能回答「为什么没有」。"""
    monkeypatch.setattr(api, "_post_jwt", lambda *a, **k: (200, ERR_ENVELOPE))
    board, why = api.board_fetch("jwt-not-used")
    assert board == [] and why == "系统错误!!"

    monkeypatch.setattr(api, "_post_jwt", lambda *a, **k: (502, b""))
    board, why = api.board_fetch("jwt-not-used")
    assert board is None and why == "HTTP 502"

    # 200 + 真没场次（有 f5 容器但里面为空）：不能说成错误
    monkeypatch.setattr(api, "_post_jwt", lambda *a, **k: (200, b"\x12\x03suc\x2a\x00"))
    board, why = api.board_fetch("jwt-not-used")
    assert board == [] and why


def test_board_with_odds_contract_unchanged(monkeypatch):
    """下单链（challenge_hourly）还按老契约用：None=没拿到、[]=空板。"""
    monkeypatch.setattr(api, "_post_jwt", lambda *a, **k: (200, ERR_ENVELOPE))
    assert api.board_with_odds("jwt") == []
    monkeypatch.setattr(api, "_post_jwt", lambda *a, **k: (500, b""))
    assert api.board_with_odds("jwt") is None


# ---------- ② duty 采集步 ----------

@pytest.fixture
def duty_env(tmp_path, monkeypatch):
    """把 duty 的全板/真值两条取数都换成桩，ROOT 换到 tmp_path，日志收进列表。"""
    duty = _load("njstats_duty_board", "scripts/njstats_duty.py")
    monkeypatch.setattr(duty, "ROOT", tmp_path)
    monkeypatch.setattr(duty.api, "my_predictions", lambda *a, **k: [{"sid": "a"}])
    lines: list[str] = []
    monkeypatch.setattr(duty, "log", lines.append)
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "challenge_state.json").write_text(
        json.dumps({"records": [{"match": "A vs B"}]}), encoding="utf-8")
    odds_dir = state / "njstats" / "site_odds"
    odds_dir.mkdir(parents=True, exist_ok=True)
    today = odds_dir / f"{time.strftime('%Y-%m-%d')}.json"
    today.write_text(json.dumps({"total": 387, "matches": [{"sid": "x"}]}),
                     encoding="utf-8")
    return duty, lines, today


def test_duty_logs_the_board_failure_instead_of_swallowing_it(duty_env, monkeypatch):
    """空板不许再静默：必须留字，且**不能覆盖**当天已经攒下的快照。"""
    duty, lines, today = duty_env
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: ([], "系统错误!!"))

    duty._maybe_server_records_snapshot()

    assert any("odds-snapshot:" in m for m in lines), lines
    assert json.loads(today.read_text(encoding="utf-8"))["total"] == 387, "旧快照被清空了"
    # 真值那份与它同函数，一处失败不能连坐另一处
    assert any("server-snapshot:" in m for m in lines), lines


def test_duty_distinguishes_no_response_from_empty_board(duty_env, monkeypatch):
    duty, lines, _today = duty_env
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: (None, "HTTP 502"))
    duty._maybe_server_records_snapshot()
    fail = next(m for m in lines if "odds-snapshot:" in m)

    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: ([], "系统错误!!"))
    duty._maybe_server_records_snapshot()
    empty = next(m for m in lines if "odds-snapshot:" in m and m != fail)

    assert "全板取数失败" in fail and "HTTP 502" in fail, fail
    assert "全板 0 场" in empty and "系统错误!!" in empty, empty


def test_duty_still_writes_the_snapshot_when_the_board_is_back(duty_env, monkeypatch):
    """修的是「看不见」，不是采集本身：板子回来就照常落盘。"""
    duty, lines, today = duty_env
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: (
        [{"sid": "s1", "home": "甲", "away": "乙", "kick_ts": 1, "odds": [1.9, 3.4, 4.0]},
         {"sid": "s2", "home": "丙", "away": "丁", "kick_ts": 2, "odds": None}], ""))

    duty._maybe_server_records_snapshot()

    body = json.loads(today.read_text(encoding="utf-8"))
    assert body["total"] == 2 and body["with_odds"] == 1, body
    assert any("odds-snapshot: 站点赔率 1/2 场" == m for m in lines), lines


# ---------- ③ 摘要按 duty 的原话归因（跨模块锁步） ----------

def _rows_from_duty(lines: list[str]) -> list[tuple[str, str]]:
    """duty 写日志不带时间戳前缀，这里按生产格式补上再走真解析。"""
    ts = NOW.strftime("%m-%d %H:%M:%S")
    return ops.parse_log("\n".join(f"{ts} {m}" for m in lines))


def _flags(rows, today_site_odds: bool):
    return ops.build_red_flags(
        {"missing_count": 0, "missing_hours": []}, {}, None,
        {"records": 0, "missing_fields": 0}, 1.0, "仓库未记 0 条", today_site_odds,
        board_down=ops.board_outage(rows))


def test_duty_failure_line_is_recognised_by_the_digest(duty_env, monkeypatch):
    duty, lines, _today = duty_env
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: ([], "系统错误!!"))
    duty._maybe_server_records_snapshot()
    odds_line = [m for m in lines if m.startswith("odds-snapshot:")]
    assert ops.board_outage(_rows_from_duty(odds_line)) == {"why": "系统错误!!"}


def test_digest_says_upstream_instead_of_blaming_the_push_chain(duty_env, monkeypatch):
    duty, lines, _today = duty_env
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: (None, "HTTP 502"))
    duty._maybe_server_records_snapshot()
    rows = _rows_from_duty([m for m in lines if m.startswith("odds-snapshot:")])

    codes = {f["code"] for f in _flags(rows, today_site_odds=False)}
    assert codes == {"odds_board_upstream"}, codes
    flag = next(f for f in _flags(rows, False) if f["code"] == "odds_board_upstream")
    assert "HTTP 502" in flag["detail"] and "msgId=1" in flag["action"], flag


def test_outage_is_caught_even_before_the_day_rolls_over(duty_env, monkeypatch):
    """11-26 21:35→23:35 那种「当天文件还在、但从此不再更新」的形状以前无人知晓。"""
    duty, lines, _today = duty_env
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: ([], "系统错误!!"))
    duty._maybe_server_records_snapshot()
    rows = _rows_from_duty([m for m in lines if m.startswith("odds-snapshot:")])

    codes = {f["code"] for f in _flags(rows, today_site_odds=True)}
    assert codes == {"odds_board_upstream"}, codes


def test_healthy_rounds_produce_no_board_flag(duty_env, monkeypatch):
    duty, lines, _today = duty_env
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: (
        [{"sid": "s1", "home": "甲", "away": "乙", "kick_ts": 1, "odds": [1.9, 3.4, 4.0]}], ""))
    duty._maybe_server_records_snapshot()
    rows = _rows_from_duty([m for m in lines if m.startswith("odds-snapshot:")])

    assert ops.board_outage(rows) is None
    assert _flags(rows, today_site_odds=True) == []
    # 板子正常而当天文件不在 = 真是提交链的事，照旧可派单
    assert {f["code"] for f in _flags(rows, today_site_odds=False)} == {"today_site_odds_missing"}


def test_recovery_mid_window_clears_the_outage(duty_env, monkeypatch):
    """失败之后又成功一轮 → 不再喊厂商，避免一次抖动常驻红线。"""
    duty, lines, _today = duty_env
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: ([], "系统错误!!"))
    duty._maybe_server_records_snapshot()
    bad = [m for m in lines if m.startswith("odds-snapshot:")]
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: (
        [{"sid": "s1", "home": "甲", "away": "乙", "kick_ts": 1, "odds": [1.9, 3.4, 4.0]}], ""))
    duty._maybe_server_records_snapshot()
    good = [m for m in lines if m.startswith("odds-snapshot:")][len(bad):]

    assert ops.board_outage(_rows_from_duty(bad)) is not None
    assert ops.board_outage(_rows_from_duty(bad + good)) is None


def test_odds_failure_line_is_not_counted_as_a_successful_round():
    """SUCCESS_RE 的收紧：失败行不能把本轮算成「成功轮次」。

    不收紧的话，error_upstream 每轮都被自己轮尾的 odds 失败行「洗白」，
    派单器 ROOT_CAUSES 的压制就永远不生效（#29 正是这条链路）。
    """
    rows = ops.parse_log("\n".join([
        NOW.strftime("%m-%d %H:%M:%S ") + "mint HTTP 502（站点 API 服务端故障，与登录态无关）",
        (NOW + timedelta(seconds=1)).strftime("%m-%d %H:%M:%S ")
        + "odds-snapshot: 全板 0 场，不写快照，沿用旧文件；原因=系统错误!!",
    ]))
    assert ops.error_summary(rows)["upstream"]["resolved"] is False

    rows_ok = ops.parse_log("\n".join([
        NOW.strftime("%m-%d %H:%M:%S ") + "mint HTTP 502（站点 API 服务端故障，与登录态无关）",
        (NOW + timedelta(seconds=1)).strftime("%m-%d %H:%M:%S ")
        + "odds-snapshot: 站点赔率 387/502 场",
    ]))
    assert ops.error_summary(rows_ok)["upstream"]["resolved"] is True


# ---------- ④ 派单路由 ----------

def test_board_outage_does_not_open_an_agent_ticket(duty_env, monkeypatch):
    disp = _load("agent_dispatch_board", "scripts/agent_dispatch.py")
    duty, lines, _today = duty_env
    monkeypatch.setattr(duty.api, "board_fetch", lambda *a, **k: (None, "HTTP 502"))
    duty._maybe_server_records_snapshot()
    flags = _flags(_rows_from_duty([m for m in lines if m.startswith("odds-snapshot:")]),
                   today_site_odds=False)

    assert "odds_board_upstream" in disp.HUMAN_ONLY
    assert disp.candidates_from_flags(flags) == [], flags
    # 而真·提交链故障仍然派单：不能把这一类一起闭嘴
    assert disp.candidates_from_flags(
        [{"code": "today_site_odds_missing", "detail": "d", "action": "a"}]) != []
