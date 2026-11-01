"""football-data.co.uk 抓取的刷新语义与可见性（2026-10-28）

修的两个真问题：
1. **缓存永不刷新**：旧实现 `if dest.exists() and size > 5000: continue`，
   文件一旦落盘就再也不会更新 —— 实际数据从 2026-10-09 起停更 19 天，
   而新入账的 272 注全部匹配失败，却只被记成「覆盖不到/无英文/匹配失败」。
2. **代理写死**：旧实现硬编码 `127.0.0.1:7897`，CI runner 上必然失败，
   再被 workflow 的 `|| true` 吞掉，于是「云端每天在刷新」只是日志幻觉。

这里锁三件事：代理只在显式配置时启用；每次都重新下载；失败不删旧缓存但要落状态。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import fetch_football_data as ffd


class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status


# ---------- 代理解析 ----------

def test_proxy_unset_means_direct(monkeypatch):
    monkeypatch.delenv("FDC_PROXY", raising=False)
    assert ffd._proxy() is None, "未配置必须直连，否则 CI 永远连不上"


def test_proxy_from_env(monkeypatch):
    monkeypatch.setenv("FDC_PROXY", "http://127.0.0.1:7897")
    assert ffd._proxy() == {"http": "http://127.0.0.1:7897",
                            "https": "http://127.0.0.1:7897"}


def test_proxy_without_scheme_falls_back_to_direct(monkeypatch, capsys):
    monkeypatch.setenv("FDC_PROXY", "127.0.0.1:7897")
    assert ffd._proxy() is None
    assert "缺少 scheme" in capsys.readouterr().out


# ---------- 单次请求 ----------

def test_fetch_rejects_html_page(monkeypatch):
    monkeypatch.setattr(ffd.requests, "get",
                        lambda *a, **k: _Resp(b"<!DOCTYPE html><html>blocked"))
    data, err = ffd.fetch("https://x/y.csv")
    assert data is None and err == "html_instead_of_csv", "HTML 不能当 CSV 缓存"


def test_fetch_reports_http_status(monkeypatch):
    monkeypatch.setattr(ffd.requests, "get", lambda *a, **k: _Resp(b"", status=503))
    data, err = ffd.fetch("https://x/y.csv")
    assert data is None and err == "HTTP 503"


def test_fetch_reports_network_error(monkeypatch):
    def boom(*a, **k):
        raise ffd.requests.ConnectionError("no route")

    monkeypatch.setattr(ffd.requests, "get", boom)
    data, err = ffd.fetch("https://x/y.csv")
    assert data is None and "ConnectionError" in err


# ---------- 刷新语义（核心回归） ----------

@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ffd, "OUT", tmp_path)
    monkeypatch.setattr(ffd, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(ffd, "_urls", lambda: {"new_BRA": "https://x/bra.csv"})
    return tmp_path


def test_existing_cache_is_refreshed(out_dir, monkeypatch):
    """旧实现会因为文件已存在而永远不更新——这正是 19 天停更的根因。"""
    (out_dir / "new_BRA.csv").write_bytes(b"OLD" * 3000)
    monkeypatch.setattr(ffd, "fetch", lambda url, proxies=None: (b"NEW" * 3000, ""))

    res = ffd.fetch_all(None)

    assert (out_dir / "new_BRA.csv").read_bytes() == b"NEW" * 3000
    assert res["new_BRA"]["ok"] is True


def test_failed_refresh_keeps_old_cache(out_dir, monkeypatch):
    (out_dir / "new_BRA.csv").write_bytes(b"OLD" * 3000)
    monkeypatch.setattr(ffd, "fetch", lambda url, proxies=None: (None, "HTTP 403"))

    res = ffd.fetch_all(None)

    assert (out_dir / "new_BRA.csv").read_bytes() == b"OLD" * 3000
    assert res["new_BRA"]["ok"] is False
    assert res["new_BRA"]["used_cache"] is True
    assert res["new_BRA"]["error"] == "HTTP 403"


def test_failed_refresh_without_cache_is_error(out_dir, monkeypatch):
    monkeypatch.setattr(ffd, "fetch", lambda url, proxies=None: (None, "HTTP 403"))

    res = ffd.fetch_all(None)

    assert res["new_BRA"]["ok"] is False and res["new_BRA"]["used_cache"] is False


# ---------- 解析与状态 ----------

_HEADER = ("Country,League,Season,Date,Time,Home,Away,HG,AG,Res,"
           "PSCH,PSCD,PSCA,B365CH,B365CD,B365CA,MaxCH,MaxCD,MaxCA")


def _row(date, home, away, hg, ag, res, psc, b365, mx) -> str:
    """按列顺序显式拼一行，避免手写逗号导致列错位（本测试第一版就栽在这）。"""
    cells = ([c if isinstance(c, str) else str(c)
              for c in ["Brazil", "Serie A", "2026", date, "20:00", home, away, hg, ag, res]]
             + [("" if v is None else str(v)) for v in (*psc, *b365, *mx)])
    assert len(cells) == 19
    return ",".join(cells)


_CSV = "\n".join([
    _HEADER,
    # 2026 年真实情况：Pinnacle 收盘缺失，只有 B365 / Max 可用
    _row("23/07/2026", "Botafogo RJ", "Vitoria", 0, 0, "D",
         (None, None, None), (1.71, 3.6, 4.75), (1.75, 3.9, 4.75)),
    # 老数据：Pinnacle 收盘齐全
    _row("24/07/2026", "Flamengo RJ", "Santos", 2, 1, "H",
         (1.55, 4.1, 6.2), (1.53, 4.0, 6.0), (1.55, 4.2, 6.5)),
]) + "\n"


def test_parse_all_normalizes_and_tracks_latest(out_dir):
    out_dir.joinpath("new_BRA.csv").write_text(_CSV, encoding="utf-8")
    rows, latest = ffd.parse_all()
    assert rows == 2
    assert latest["new_BRA"] == "2026-07-24"

    recs = [json.loads(l) for l in
            out_dir.joinpath("parsed.jsonl").read_text(encoding="utf-8").splitlines()]
    assert recs[0]["psc_h"] is None and recs[0]["b365c_h"] == 1.71
    assert recs[1]["psc_h"] == 1.55 and recs[1]["hg"] == "2"


def test_write_status_reports_failures(out_dir):
    results = {"new_BRA": {"ok": True, "bytes": 10},
               "new_JPN": {"ok": False, "error": "HTTP 403", "used_cache": True}}
    st = ffd.write_status(results, rows=2, latest={"new_BRA": "2026-07-24",
                                                   "new_JPN": "2026-10-09"},
                          proxy_configured=False)
    assert st["files_ok"] == 1 and st["files_failed"] == 1
    assert st["latest_date"] == "2026-10-09", "latest_date 取全部文件的最大日期"
    assert st["failed"] == {"new_JPN": "HTTP 403"}
    assert st["proxy_configured"] is False
    assert isinstance(st["at_epoch"], int)
    assert json.loads(out_dir.joinpath("status.json").read_text(encoding="utf-8"))["rows"] == 2
