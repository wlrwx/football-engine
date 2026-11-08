"""NJStats 回测策略采集（modId=1028）的解析与落盘测试（2026-11-08）。

为什么值得测：
这批数据唯一的用途是「日后能知道站点当时报了多少」——站点没有历史接口，
每次请求返回的都是截至当下的全量重算。所以最怕的不是报错，而是**解错还解得
很顺**：字段错位、把走盘当成负场、把站点赔率口径的收益率当成体彩口径，
快照就会以「看起来正常」的形状污染以后所有的对照。

这里锁四件事：
1. 请求体形状（页签选择器 base=1 / mine=2，不是翻页）
2. 两条已互验的口径恒等式：胜率 = 胜/(总−走)、场均收益率 = 累计收益/(总−走)×100
3. 解不出的字段原样落盘（raw_fN / raw_<tag>），不编标签也不丢条目
4. 失败保旧：任何一步取不到数都不覆盖已有文件、不整份丢掉别的策略

边界：本模块 store-only。这些是站点赔率口径的回测，按市场隔离纪律只能做
展示与外部对照，不能当特征——所以这里也只测「解得对、存得住」。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, fname: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / fname)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# 先注册 api，collector 里那句 import njstats_api_submit 才会复用同一个对象，
# monkeypatch 才patch得到（patch 错对象 = 测试绿、线上照旧走网络）。
api = _load("njstats_api_submit", "njstats_api_submit.py")
col = _load("fetch_njstats_strategies", "fetch_njstats_strategies.py")

BJT = timezone(timedelta(hours=8))


def test_collector_uses_the_patched_gateway_module():
    assert col.api is api


# ---------------------------------------------------------------- 请求体形状

def test_encode_strategy_list_is_a_tab_selector_not_paging():
    """f1{1:tab} + f2{空}：1=基础策略、2=自建策略。写反了就会每天存到同一页。"""
    assert api.encode_strategy_list("base") == b"\n\x02\x08\x01\x12\x00"
    assert api.encode_strategy_list("mine") == b"\n\x02\x08\x02\x12\x00"
    with pytest.raises(KeyError):
        api.encode_strategy_list("nope")


def test_encode_strategy_detail_carries_sid_and_window():
    got = api.encode_strategy_detail("abc123")
    outer = api.decode_msg(got)
    inner = api.decode_msg(api._get(outer, 1))
    assert api._get(inner, 1).decode() == "abc123"
    assert api._get(inner, 2, "v") == api.WINDOW_ALL


# ---------------------------------------------------------------- 列表解析

def _entry(sid: str, name: str, cond: bytes, win_rate: str = "68.58",
           no: str = "1") -> bytes:
    return api._field(
        1,
        api._field(1, sid.encode()) + api._field(2, name.encode())
        + api._field(4, cond) + api._field(10, win_rate.encode())
        + api._field(11, no.encode()),
    )


def test_parse_strategy_list_extracts_definition_and_claims():
    raw = api._field(5, _entry("s1", "客队不败", b'[{"type":"odds"}]'))
    rows = api.parse_strategy_list(raw)
    assert rows == [{"sid": "s1", "name": "客队不败",
                     "conditions": [{"type": "odds"}],
                     "win_rate": "68.58", "strategy_no": "1"}]


def test_parse_strategy_list_keeps_entry_when_conditions_unreadable():
    """站点改了条件格式 → 条件记 None，但这一条不能跟着消失，
    否则快照看起来「策略变少了」，像站点下线了策略而不是我们解不动。"""
    rows = api.parse_strategy_list(api._field(5, _entry("s1", "X", b"{broken")))
    assert len(rows) == 1 and rows[0]["conditions"] is None


def test_parse_strategy_list_shape_edge_cases():
    assert api.parse_strategy_list(b"") == []
    assert api.parse_strategy_list(api._field(6, _entry("s1", "X", b"[]"))) == []
    # 没有 sid 的条目 = 不是策略行，跳过而不是抛
    assert api.parse_strategy_list(api._field(5, api._field(1, b""))) == []


# ---------------------------------------------------------------- 回测解析

# 2026-11-08 从网关取到的「客队不败」真实汇总（442/251/76 → 68.58% / 23.27%）。
REAL = {"total": 442, "wins": 251, "push": 76, "avg_odds": "1.80",
        "win_rate": "68.58", "roi_pct": "23.27", "profit": "85.17",
        "raw_f5": 52}


def _header(fields: list) -> bytes:
    hdr = b"".join(fields)
    return api._field(5, api._field(1, hdr))


def _backtest_raw(over: dict | None = None) -> bytes:
    d = {**REAL, **(over or {})}
    fields = [
        api._field_v(1, d["total"]), api._field_v(2, d["wins"]), api._field_v(3, d["push"]),
        api._field(6, str(d["avg_odds"]).encode()), api._field(7, str(d["win_rate"]).encode()),
        api._field(8, str(d["roi_pct"]).encode()), api._field(9, str(d["profit"]).encode()),
        api._field_v(5, d["raw_f5"]), api._field_v(12, 5), api._field_v(13, 50),
        api._field_v(15, 1),
        api._field(10, api._field(1, b"L123") + api._field(2, "西班牙".encode())
                   + api._field(3, "西甲".encode())
                   + api._field_v(4, 30) + api._field_v(5, 20) + api._field_v(6, 3)
                   + api._field(9, b"74.07")),
        api._field(11, api._field(1, b"W")
                   + api._field(2, api._field_v(1, 7) + api._field_v(2, 4)
                                + api._field(3, b"66.67") + api._field(4, b"1.85")
                                + api._field(5, b"1.20") + api._field(6, b"20.0")
                                + api._field_v(7, 202645) + api._field_v(8, 1))),
        api._field(11, api._field(1, b"M")
                   + api._field(2, api._field_v(1, 9) + api._field_v(2, 6)
                                + api._field(3, b"66.67") + api._field(4, b"1.85")
                                + api._field(5, b"1.50") + api._field(6, b"16.67")
                                + api._field_v(7, 202611) + api._field_v(8, 0))),
        api._field(11, api._field(1, b"Q")
                   + api._field(2, api._field_v(1, 3) + api._field_v(2, 1)
                                + api._field_v(7, 20261))),
        api._field(14, b"L123"), api._field(14, b"L456"),
    ]
    return _header(fields)


def test_parse_strategy_backtest_matches_real_values():
    bt = api.parse_strategy_backtest(_backtest_raw())
    assert bt["total"] == 442 and bt["wins"] == 251 and bt["push"] == 76
    assert bt["avg_odds"] == 1.8 and bt["profit"] == 85.17
    assert bt["hit_league_sids"] == ["L123", "L456"]
    # 未定口径照样落盘：不编标签，但也不能丢
    assert (bt["raw_f5"], bt["raw_f12"], bt["raw_f13"], bt["raw_f15"]) == (52, 5, 50, 1)


def test_parse_strategy_backtest_two_verified_identities():
    """这两条恒等式是 2026-11-08 对 5 个策略逐个核出来的，零误差。

    解错任何一个字段（典型：把 f3 走盘当负场、把 f9 表现当收益率）都会让等式
    在真实响应上成立、在fixture 上立刻断裂——所以这里按 UI 报的数字反推。
    """
    bt = api.parse_strategy_backtest(_backtest_raw())
    n = bt["total"] - bt["push"]
    assert round(bt["wins"] / n * 100, 2) == pytest.approx(bt["win_rate"])
    assert round(bt["profit"] / n * 100, 2) == pytest.approx(bt["roi_pct"])
    # 五个策略都成立；换一组真实值再验一次（三体客队受让 236/137/28 → 65.87 / 20.59）
    alt = api.parse_strategy_backtest(_backtest_raw(
        {"total": 236, "wins": 137, "push": 28, "win_rate": "65.87",
         "roi_pct": "20.59", "profit": "42.82", "avg_odds": "1.81"}))
    m = alt["total"] - alt["push"]
    assert round(alt["wins"] / m * 100, 2) == pytest.approx(alt["win_rate"])
    assert round(alt["profit"] / m * 100, 2) == pytest.approx(alt["roi_pct"])


def test_parse_strategy_backtest_leagues_and_series():
    bt = api.parse_strategy_backtest(_backtest_raw())
    assert bt["leagues"] == [{"league_sid": "L123", "country": "西班牙",
                             "league": "西甲", "total": 30, "wins": 20,
                             "push": 3, "win_rate": 74.07}]
    assert round(20 / (30 - 3) * 100, 2) == pytest.approx(bt["leagues"][0]["win_rate"])
    assert bt["series"]["weekly"][0]["period"] == 202645
    assert bt["series"]["weekly"][0]["push"] == 1
    assert bt["series"]["monthly"][0]["period"] == 202611
    assert bt["series"]["monthly"][0]["roi_pct"] == 16.67
    # 未知页签标签照样落盘，键名带上标签本身——不猜「Q 大概是季度」
    assert bt["series"]["raw_Q"][0]["total"] == 3


def test_parse_strategy_backtest_garbage_returns_none():
    """None = 没解出 header，和「解出来但没有联赛行」必须是两个不同的值。"""
    assert api.parse_strategy_backtest(b"") is None
    assert api.parse_strategy_backtest(api._field(6, b"")) is None
    assert api.parse_strategy_backtest(api._field(5, api._field(2, b""))) is None


# ---------------------------------------------------------------- 采集分层

def _row(sid="s1", name="客队不败"):
    return {"sid": sid, "name": name, "conditions": None,
            "win_rate": "68.58", "strategy_no": "1"}


def test_entry_splits_summary_from_detail(monkeypatch):
    bt = api.parse_strategy_backtest(_backtest_raw())
    monkeypatch.setattr(api, "strategy_backtest", lambda sid, jwt=None: bt)
    e, det = col._entry(_row(), "base", "jwt")
    assert e["sid"] == "s1" and e["tab"] == "base"
    assert e["backtest_error"] is None
    assert e["backtest"]["win_rate"] == 68.58
    assert not (set(e["backtest"]) & set(col.DETAIL_KEYS)), "明细不能混进每日汇总"
    assert set(det) == set(col.DETAIL_KEYS) and len(det["leagues"]) == 1


@pytest.mark.parametrize("side_effect,expected", [
    (RuntimeError("boom"), "RuntimeError"),
    (None, "no_detail"),
])
def test_entry_records_why_a_strategy_is_missing(monkeypatch, side_effect, expected):
    """坏掉的那个策略留下原因，其余策略照常入库——快照要能说出是谁坏了。"""
    def fake(sid, jwt=None):
        if isinstance(side_effect, Exception):
            raise side_effect
        return side_effect
    monkeypatch.setattr(api, "strategy_backtest", fake)
    e, det = col._entry(_row(), "base", "jwt")
    assert e["backtest"] is None and e["backtest_error"] == expected
    assert det == {}


def test_collect_tab_shapes(monkeypatch):
    bt = api.parse_strategy_backtest(_backtest_raw())
    calls = []

    def fake_list(tab, jwt=None):
        calls.append(tab)
        return {"base": [_row()], "mine": []}[tab]
    monkeypatch.setattr(api, "strategy_list", fake_list)
    monkeypatch.setattr(api, "strategy_backtest", lambda sid, jwt=None: bt)
    got, ok = col.collect("jwt")
    assert calls == list(col.TABS)
    assert got["tab_counts"] == {"base": 1, "mine": 0}
    # mine=0 是权限状态（非会员建不了自建策略），不是取数失败
    assert ok is True
    assert got["details"][0]["tab"] == "base"


def test_collect_distinguishes_unreachable_from_empty(monkeypatch):
    monkeypatch.setattr(api, "strategy_list", lambda tab, jwt=None: None if tab == "base" else [])
    monkeypatch.setattr(api, "strategy_backtest", lambda sid, jwt=None: None)
    got, ok = col.collect("jwt")
    assert got["tab_counts"] == {"base": None, "mine": 0}
    assert ok is False, "None（取不到）必须让本轮标记为不完整，空列表不用"


def test_collect_marks_incomplete_when_one_backtest_fails(monkeypatch):
    monkeypatch.setattr(api, "strategy_list", lambda tab, jwt=None: [_row("s1"), _row("s2", "三体主胜")]
                        if tab == "base" else [])
    monkeypatch.setattr(api, "strategy_backtest",
                        lambda sid, jwt=None: None if sid == "s2" else api.parse_strategy_backtest(_backtest_raw()))
    got, ok = col.collect("jwt")
    assert len(got["strategies"]) == 2
    assert [s["backtest_error"] for s in got["strategies"]] == [None, "no_detail"]
    assert len(got["details"]) == 1 and ok is False


# ---------------------------------------------------------------- 落盘

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    out = tmp_path / "data" / "state" / "njstats" / "strategies"
    monkeypatch.setattr(col, "OUT_DIR", out)
    monkeypatch.setattr(col, "DETAIL_DIR", out / "detail")
    monkeypatch.setattr(col, "_jwt_from_disk", lambda: "jwt")
    bt = api.parse_strategy_backtest(_backtest_raw())
    monkeypatch.setattr(api, "strategy_list",
                        lambda tab, jwt=None: [_row()] if tab == "base" else [])
    monkeypatch.setattr(api, "strategy_backtest", lambda sid, jwt=None: bt)
    return out


def today_bjt() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d")


def test_main_writes_daily_summary_and_weekly_detail(sandbox):
    assert col.main([]) == 0
    daily = json.loads((sandbox / f"{today_bjt()}.json").read_text(encoding="utf-8"))
    assert daily["tabs"] == {"base": 1, "mine": 0}
    assert daily["detail_written"] is True
    assert daily["strategies"][0]["backtest"]["win_rate"] == 68.58
    assert col.DETAIL_KEYS and "leagues" not in daily["strategies"][0]["backtest"]
    assert "field_notes" in daily and "口径" in daily["note"]
    detail = json.loads((sandbox / "detail" / f"{col.week_key(datetime.now(BJT))}.json")
                        .read_text(encoding="utf-8"))
    assert len(detail["strategies"][0]["leagues"]) == 1


@pytest.mark.parametrize("argv,expect_rewrite", [(["--force-detail"], True), ([], False)])
def test_detail_written_once_per_week(sandbox, argv, expect_rewrite):
    assert col.main([]) == 0
    path = sandbox / "detail" / f"{col.week_key(datetime.now(BJT))}.json"
    path.write_text('{"sentinel": 1}', encoding="utf-8")
    assert col.main(argv) == 0
    body = json.loads(path.read_text(encoding="utf-8"))
    assert (body.get("sentinel") is None) is expect_rewrite
    daily = json.loads((sandbox / f"{today_bjt()}.json").read_text(encoding="utf-8"))
    assert daily["detail_written"] is expect_rewrite


def test_missing_jwt_writes_nothing(sandbox, monkeypatch):
    """没 JWT 时既不抓也不写——半条数据都不许覆盖昨天的快照。"""
    monkeypatch.setattr(col, "_jwt_from_disk", lambda: "")
    called = []
    monkeypatch.setattr(api, "strategy_list", lambda *a, **k: called.append(1) or [])
    assert col.main([]) == 1
    assert not called and not list(sandbox.glob("*.json"))


def test_write_failure_keeps_previous_snapshot(sandbox, monkeypatch, capsys):
    assert col.main([]) == 0
    before = (sandbox / f"{today_bjt()}.json").read_text(encoding="utf-8")

    def boom(path, payload):
        raise OSError("盘满")
    monkeypatch.setattr(col, "_write", boom)
    assert col.main([]) == 1
    assert (sandbox / f"{today_bjt()}.json").read_text(encoding="utf-8") == before
    assert "写盘失败" in capsys.readouterr().out


def test_check_mode_does_not_write(sandbox):
    assert col.main(["--check"]) == 0
    assert not list(sandbox.glob("*.json"))


def test_week_key_uses_iso_year_not_calendar_year():
    assert col.week_key(datetime(2026, 11, 8)) == "2026-W45"
    # 2027-01-03 还属于 2026 年第 53 周；按日历年切会多出 2027-W01 这种孤儿周
    assert col.week_key(datetime(2027, 1, 3)) == "2026-W53"


def test_no_target_market_odds_leak_into_model_surface():
    """市场隔离：本模块只落盘，不能被 engine 侧 import 当特征。

    快照本身是站点赔率口径的回测——一旦被模型侧引用，就违反了「目标盘口
    赔率不得作特征」。这条比文档便宜，且会在有人真接线时立刻变红。
    """
    refs = [p.relative_to(col.ROOT) for p in (col.ROOT / "engine").rglob("*.py")
            if "njstats_strategies" in p.read_text(encoding="utf-8", errors="ignore")]
    assert refs == [], refs
