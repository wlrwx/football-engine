"""服务端真值分页拉取（2026-11-22，#104 审计实锤的截断修复）。

旧实现 page=1/size=60 写死：本地账过 60 注后真值永远只有前 60 条，
跨机去重会拿残缺分母重复下单。行为测试钉三件事：翻到短页为止、
逐页调用次数正确、**任何一页坏都整体返 None**（半截真值比没有更危险）。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("api_page", ROOT / "scripts" / "njstats_api_submit.py")
api = importlib.util.module_from_spec(SPEC)
sys.modules["njstats_api_submit_page"] = api
SPEC.loader.exec_module(api)


def _fake_pages(monkeypatch, counts, fail_at=None):
    calls = []

    def fake_post(jwt, mod, msg, body):
        calls.append(body)
        page = len(calls)
        if fail_at == page:
            return 502, b""
        return 200, b"stub%d" % page

    def fake_parse(raw):
        n = counts[int(raw[4:]) - 1]
        return [{"sid": f"s{int(raw[4:])}-{i}"} for i in range(n)]

    monkeypatch.setattr(api, "_post_jwt", fake_post)
    monkeypatch.setattr(api, "parse_my_predictions", fake_parse)
    return calls


def test_stops_at_short_page_after_two_calls(monkeypatch):
    calls = _fake_pages(monkeypatch, [60, 5])
    rows = api.my_predictions(jwt="x")
    assert len(rows) == 65 and len(calls) == 2


def test_first_page_failure_returns_none(monkeypatch):
    calls = _fake_pages(monkeypatch, [60], fail_at=1)
    assert api.my_predictions(jwt="x") is None and len(calls) == 1


def test_middle_page_failure_never_returns_partial(monkeypatch):
    # 关键不变式：page1 满 60 条、page2 坏 → 整体 None，绝不交 60 条「半截真值」
    calls = _fake_pages(monkeypatch, [60, 60], fail_at=2)
    assert api.my_predictions(jwt="x") is None and len(calls) == 2


def test_exact_multiple_of_size_fetches_till_empty(monkeypatch):
    calls = _fake_pages(monkeypatch, [60, 60, 0])
    rows = api.my_predictions(jwt="x")
    assert len(rows) == 120 and len(calls) == 3
