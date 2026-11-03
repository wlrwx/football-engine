"""赛前情报采集（2026-10-23）：只做标题抓取与队名匹配，store-only。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import fetch_news as fn  # noqa: E402

PAGE = """
<a href="//news.zhibo8.com/zuqiu/2026-10-22/aaa.htm" target="_blank">曼联考虑换帅，伯恩茅斯之战成关键</a>
<a href="//news.zhibo8.com/zuqiu/2026-10-21/bbb.htm"><img src="x">尤文vs雷恩首发：穆阿尼先发</a>
<a href="//news.zhibo8.com/zuqiu/2026-10-19/ccc.htm">三天前的旧闻 曼联</a>
<a href="//news.zhibo8.com/zuqiu/2026-10-22/aaa.htm">重复链接</a>
<a href="/other/path">不是文章</a>
"""


def _preds():
    return [
        {"match_id": "2026-10-22_周四006", "home_team": "伯恩茅斯", "away_team": "AC米兰"},
        {"match_id": "2026-10-22_周四004", "home_team": "尤文图斯", "away_team": "雷恩"},
    ]


def test_parse_dedup_and_strip():
    items = fn.parse_list(PAGE)
    assert len(items) == 3                 # 重复 URL 去掉、非文章链接忽略
    assert items[0]["url"].startswith("https://news.zhibo8.com/zuqiu/")
    assert "尤文vs雷恩" in items[1]["title"]  # 去掉 img 标签


def test_match_uses_aliases_and_date_window():
    items = fn.parse_list(PAGE)
    got = fn.match_items(items, _preds(), "2026-10-22", days=1)
    titles = [x["title"] for x in got]
    assert any("曼联" in t for t in titles), "曼联应通过 TEAM_ALIASES 匹配到曼彻斯特联"
    assert any("尤文" in t for t in titles)
    assert not any("旧闻" in t for t in titles), "10-19 超出 ±1 天窗口"
    first = next(x for x in got if "曼联" in x["title"])
    assert first["match_ids"] == ["2026-10-22_周四006"]
    assert "伯恩茅斯" in first["teams"]


def _many(n, day="2026-10-22"):
    return [{"date": day, "title": f"尤文图斯伤情通报{i}",
             "url": f"https://news.zhibo8.com/zuqiu/{day}/x{i}.htm"} for i in range(n)]


def test_limit_none_returns_all_and_default_is_120():
    # 实测 14 天里 6 天条数正好卡在旧上限 40 → 按场次覆盖率被截断低估。
    assert fn.NEWS_LIMIT == 120, "存储上限被改回去了？覆盖率会再次被低估"
    items = _many(150)
    assert len(fn.match_items(items, _preds(), "2026-10-22", limit=None)) == 150
    assert len(fn.match_items(items, _preds(), "2026-10-22")) == 120
    assert len(fn.match_items(items, _preds(), "2026-10-22", limit=7)) == 7


def test_main_records_pre_truncation_total(tmp_path, monkeypatch):
    day = "2026-10-22"
    d = tmp_path / "data" / "daily" / day
    d.mkdir(parents=True)
    (d / "predictions.json").write_text(json.dumps(_preds(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(fn, "ROOT", tmp_path)
    monkeypatch.setattr(fn, "fetch_html", lambda: "")
    monkeypatch.setattr(fn, "parse_list", lambda page: _many(150, day))
    assert fn.main(["--date", day]) == 0
    out = json.loads((d / "news.json").read_text(encoding="utf-8"))
    assert out["matched_total"] == 150 and out["limit"] == 120, "必须记下截断前总数，否则覆盖率被上限蒙住"
    assert len(out["items"]) == 120


def test_classify_and_kind_sort():
    assert fn.classify("尤文伤情：主力缺阵，首发存疑") == "injury"
    assert fn.classify("尤文有意签下前锋，谈判中") == "transfer"
    assert fn.classify("尤文vs雷恩前瞻") == "other"
    preds = [{"match_id": "m1", "home_team": "尤文图斯", "away_team": "雷恩"}]
    items = [
        {"date": "2026-10-22", "title": "尤文有意签下前锋",
         "url": "https://news.zhibo8.com/zuqiu/2026-10-22/a.htm"},
        {"date": "2026-10-22", "title": "尤文伤情：主力缺阵",
         "url": "https://news.zhibo8.com/zuqiu/2026-10-22/b.htm"},
    ]
    got = fn.match_items(items, preds, "2026-10-22")
    assert [x["kind"] for x in got] == ["injury", "transfer"], "伤病/首发必须排前"


def test_main_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fn, "ROOT", tmp_path)
    monkeypatch.setattr(fn, "fetch_html", lambda *a, **k: PAGE)
    d = tmp_path / "data" / "daily" / "2026-10-22"
    d.mkdir(parents=True)
    (d / "predictions.json").write_text(json.dumps(_preds()), encoding="utf-8")

    assert fn.main(["--date", "2026-10-22"]) == 0
    out = json.loads((d / "news.json").read_text(encoding="utf-8"))
    assert out["source_url"].endswith("/zuqiu/")
    assert out["total_scanned"] == 3
    assert out["items"] and out["items"][0]["teams"]
    assert set(out["counts"]) == {"injury", "other", "transfer"}


def test_fetch_failure_does_not_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fn, "ROOT", tmp_path)
    d = tmp_path / "data" / "daily" / "2026-10-22"
    d.mkdir(parents=True)
    (d / "predictions.json").write_text(json.dumps(_preds()), encoding="utf-8")
    (d / "news.json").write_text('{"items":["old"]}', encoding="utf-8")

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(fn, "fetch_html", boom)

    assert fn.main(["--date", "2026-10-22"]) == 1
    assert json.loads((d / "news.json").read_text(encoding="utf-8"))["items"] == ["old"]
    assert "抓取失败" in capsys.readouterr().out
