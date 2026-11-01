#!/usr/bin/env python3
"""football-data.co.uk 免费历史数据入库（2026-10-11，2026-10-28 修复刷新语义）

## 授权与使用条件

football-data.co.uk **不是收费商用 API，也没有 API key**：站方自述
"My data is free"，提供 Excel/CSV 下载给个人做量化分析；付费的是它推荐的第三方
（TheStatsAPI / OddsRelay），与本脚本无关。站方 disclaimer 只做责任与博彩合规
免责，未授予再分发许可，因此本项目**只缓存自用、不对外分发**，且不把 CSV 原文
打包进任何发布产物。引用时注明来源 football-data.co.uk。

## 免费拿什么

- 欧洲主联赛（赛季制 /mmz4281/{2526,2627}/{DIV}.csv）：D1/D2/E0/E1/SP1/I1/F1/N1/P1/SC0
- 日历年联赛（/new/{CODE}.csv，2012 至今全历史）：JPN/CHN/BRA/FIN/NOR/SWE/USA 等
- 收盘价列：`PSCH/PSCD/PSCA`（Pinnacle）、`B365CH`、`MaxCH`、`AvgCH`

**注意（2026-10-28 实测）**：`new/` 文件里 Pinnacle 收盘价到 2025 年后基本为空
（BRA 2026 年 319 行，PSCH 非空 0），所以 CLV 基准会退到 B365 收盘——
这不是匹配 bug，是源数据本身如此，报告里必须按基准分开统计。

产出：data/state/fdc/{name}.csv 原样缓存 + parsed.jsonl 归一化
      + status.json（本次抓取结果，供 health-check 判定「缓存过期」）

## 2026-10-28 修的两件事

1. **缓存永不刷新**：旧实现 `if dest.exists() and size > 5000: continue`，
   文件一旦落盘就再也不会更新——数据从 2026-10-09 起停更 19 天没人发现。
   现在每次运行都重下；下载失败才回退用旧缓存，并把失败写进 status.json。
2. **代理写死**：旧实现硬编码 `127.0.0.1:7897`（本机 Clash），CI runner 上
   必然失败，而 workflow 里是 `|| true`，于是永远静默。现在改成
   `FDC_PROXY` 环境变量，未设置就直连（CI / 普通网络均可）。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "state" / "fdc"
STATUS = OUT / "status.json"

# 欧洲主联赛（赛季制）：竞彩主要盘口
SEASON_DIVS = ["D1", "D2", "E0", "E1", "SP1", "SP2", "I1", "I2", "F1", "F2",
               "N1", "P1", "SC0", "G1", "T1", "SW1"]
SEASONS = ["2526", "2627"]
# 日历年联赛（/new/ 全历史）
NEW_CODES = ["JPN", "CHN", "BRA", "FIN", "NOR", "SWE", "USA", "ARG", "RO1",
             "POL", "RUS", "MEX", "AUT"]


def _proxy() -> dict | None:
    """`FDC_PROXY` 显式配置才走代理；未设置 = 直连。

    2026-10-28：旧实现写死 `127.0.0.1:7897`（Mac 的 Clash）。CI runner 上没有这个
    端口，每个请求都抛 ConnectionError → `fetch` 吞成 None → workflow 的 `|| true`
    让整步恒绿。于是「云端每天在刷新」只是日志幻觉，真实数据只在本机跑时才更新。
    """
    raw = (os.environ.get("FDC_PROXY") or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        print(f"  ⚠ FDC_PROXY={raw!r} 缺少 scheme（http:// 或 https://），按未配置处理，直连")
        return None
    return {"http": raw, "https": raw}


def fetch(url: str, proxies: dict | None = None) -> tuple[bytes | None, str]:
    """返回 (content, err)。err 非空说明为什么没拿到。"""
    try:
        r = requests.get(url, proxies=proxies, timeout=30)
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    if b"<!DOCTYPE" in r.content[:200]:
        # 站方对未知 UA / 频繁访问会返回 HTML 页；当失败处理，别把 HTML 当 CSV 缓存
        return None, "html_instead_of_csv"
    return r.content, ""


def _urls() -> dict[str, str]:
    urls: dict[str, str] = {}
    # 2026-10-28：赛季制文件必须用**不带 www** 的域名。带 www 的入口在本机与
    # 部分网络下 TLS 握手会被中断（SSLEOFError: UNEXPECTED_EOF_WHILE_READING），
    # 12 个联赛文件全部抓不下来；不带 www 的入口 200 且直接返回 CSV。
    for season in SEASONS:
        for div in SEASON_DIVS:
            urls[f"{season}_{div}"] = f"https://football-data.co.uk/mmz4281/{season}/{div}.csv"
    for code in NEW_CODES:
        urls[f"new_{code}"] = f"https://football-data.co.uk/new/{code}.csv"
    return urls


def fetch_all(proxies: dict | None = None) -> dict[str, dict]:
    """下载全部 CSV（每次都刷）。返回 {name: {ok, bytes, error, used_cache}}。"""
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    for name, url in _urls().items():
        dest = OUT / f"{name}.csv"
        data, err = fetch(url, proxies)
        if data:
            dest.write_bytes(data)
            results[name] = {"ok": True, "bytes": len(data), "used_cache": False}
            print(f"  ✓ {name}: {len(data)}B")
        elif dest.exists() and dest.stat().st_size > 5000:
            # 下载失败但不删旧文件：留着还能当历史基准，只是不能当"今天的数据"
            results[name] = {"ok": False, "error": err or "download_failed",
                             "used_cache": True, "cache_bytes": dest.stat().st_size}
            print(f"  ⚠ {name} 刷新失败（{err or 'download_failed'}），沿用旧缓存")
        else:
            results[name] = {"ok": False, "error": err or "no_data", "used_cache": False}
            print(f"  ✗ {name} 不可用（{err or 'no_data'}）")
    return results


def _parse_date(s: str) -> str | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime((s or "").strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_all() -> tuple[int, dict[str, str]]:
    """归一化全部缓存 CSV → parsed.jsonl。

    返回 (行数, {src: 该文件最新日期})——最新日期是判断「数据新不新鲜」的硬指标，
    比进程退出码可靠（下载成功但站方没更新，同样等于没新数据）。
    """
    rows = 0
    latest: dict[str, str] = {}
    with open(OUT / "parsed.jsonl", "w", encoding="utf-8") as out:
        for f in sorted(OUT.glob("*.csv")):
            try:
                text = f.read_text(encoding="utf-8-sig", errors="ignore")
                reader = csv.DictReader(text.splitlines())
                for r in reader:
                    date = _parse_date(r.get("Date", ""))
                    if not date:
                        continue

                    def fl(*keys):
                        for k in keys:
                            v = (r.get(k) or "").strip()
                            if v:
                                try:
                                    return float(v)
                                except ValueError:
                                    pass
                        return None

                    rec = {
                        "src": f.stem,
                        "date": date,
                        "league": (r.get("League") or r.get("Div") or "").strip(),
                        "season": (r.get("Season") or "").strip(),
                        "home_en": (r.get("HomeTeam") or r.get("Home") or "").strip(),
                        "away_en": (r.get("AwayTeam") or r.get("Away") or "").strip(),
                        "hg": r.get("HG") if r.get("HG") not in (None, "") else r.get("FTHG"),
                        "ag": r.get("AG") if r.get("AG") not in (None, "") else r.get("FTAG"),
                        "psc_h": fl("PSCH"), "psc_d": fl("PSCD"), "psc_a": fl("PSCA"),
                        "ps_h": fl("PSH"), "ps_d": fl("PSD"), "ps_a": fl("PSA"),
                        "b365c_h": fl("B365CH"), "b365c_d": fl("B365CD"), "b365c_a": fl("B365CA"),
                        "maxc_h": fl("MaxCH"), "maxc_d": fl("MaxCD"), "maxc_a": fl("MaxCA"),
                        "avgc_h": fl("AvgCH"), "avgc_d": fl("AvgCD"), "avgc_a": fl("AvgCA"),
                        "b365_h": fl("B365H"), "b365_d": fl("B365D"), "b365_a": fl("B365A"),
                    }
                    if rec["home_en"] and rec["away_en"]:
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        rows += 1
                        if date > latest.get(f.stem, ""):
                            latest[f.stem] = date
            except Exception as e:  # noqa: BLE001 — 单个文件坏了不该拖垮整批
                print(f"  ⚠ {f.name} 解析失败: {e}")
    return rows, latest


def write_status(results: dict[str, dict], rows: int,
                 latest: dict[str, str], proxy_configured: bool) -> dict:
    """落盘本次抓取状态，供 health-check 判定「缓存是否还在更新」。

    只靠退出码判定是不够的：下载成功但站方没更新（或 43 个文件里 40 个成功、
    关键联赛的那个失败），退出码都是 0。真正该报警的是**数据本身变旧**。
    """
    now = datetime.now(timezone.utc)
    files_ok = sum(1 for v in results.values() if v.get("ok"))
    latest_date = max(latest.values()) if latest else None
    payload = {
        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "at_epoch": int(now.timestamp()),
        "proxy_configured": proxy_configured,
        "files_total": len(results),
        "files_ok": files_ok,
        "files_failed": len(results) - files_ok,
        "rows": rows,
        "latest_date": latest_date,
        "latest_by_src": dict(sorted(latest.items())),
        "failed": {k: v.get("error") for k, v in results.items() if not v.get("ok")},
        "ok": files_ok > 0,
    }
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="football-data.co.uk 免费历史数据入库")
    parser.add_argument("--parse-only", action="store_true", help="跳过下载只重新解析")
    args = parser.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    proxies = _proxy()
    print(f"代理: {'已配置 ' + str(proxies['https']) if proxies else '未配置（直连）'}")

    if args.parse_only:
        results: dict[str, dict] = {}
    else:
        print("下载 CSV ...")
        results = fetch_all(proxies)
        ok = sum(1 for v in results.values() if v.get("ok"))
        print(f"下载完成: {ok}/{len(results)} 成功")

    rows, latest = parse_all()
    status = write_status(results, rows, latest, bool(proxies))
    print(f"归一化 {rows} 行 → {OUT / 'parsed.jsonl'}")
    print(f"最新数据日期: {status['latest_date']}（{status['files_ok']}/{status['files_total']} 文件刷新成功）")

    # 全挂时给 CI 一条 annotation：workflow 里是 `|| true`，只靠退出码看不见
    if results and status["files_ok"] == 0:
        print("::error::football-data.co.uk 全部文件刷新失败（检查出口网络/FDC_PROXY）——"
              "CLV 面板将停留在旧数据上", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
