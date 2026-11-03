#!/usr/bin/env python3
"""赛前情报采集（第三方新闻，store-only）——2026-10-23。

源：直播吧足球资讯列表（news.zhibo8.com/zuqiu/，HTML，当日更新，无需登录）。
用法：python scripts/fetch_news.py [--date 2026-10-23]

只做「与今日预测队名关键词匹配」的标题抓取，落到
data/daily/<date>/news.json，供 build_site 展示。**不进模型、不进账本**：
新闻未经核实，且 FusionInput 没有字段；要进模型需另立实验（输入层热插协议）。

失败语义：抓取/解析失败 → exit 1（CI 步骤会转成 ::warning::，不阻塞预测）；
抓取成功但没有匹配 → 写空 items 并 exit 0。
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from engine.team_aliases import TEAM_ALIASES, normalize_team  # noqa: E402

SOURCE_URL = "https://news.zhibo8.com/zuqiu/"
SOURCE_NAME = "直播吧足球频道"
BJT = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")

_A_RE = re.compile(
    r'<a[^>]+href="(//news\.zhibo8\.com/zuqiu/(\d{4}-\d{2}-\d{2})/[^"]+)"[^>]*>(.*?)</a>',
    re.S,
)
_INJURY_RE = re.compile(
    r"伤|缺阵|停赛|复出|报销|伤愈|大名单|首发|阵容|替补|轮换|禁赛|红牌|手术|拉伤|肌肉|脚踝|膝"
)
_TRANSFER_RE = re.compile(
    r"转会|报价|谈判|签约|目标|有意|传闻|绯闻|续约|解约|租借|挖角|冬窗|夏窗|引援|关注"
)
KIND_ORDER = {"injury": 0, "other": 1, "transfer": 2}


def classify(title: str) -> str:
    """伤病/首发优先于转会传闻；剩下的是前瞻/战报等。"""
    if _INJURY_RE.search(title):
        return "injury"
    if _TRANSFER_RE.search(title):
        return "transfer"
    return "other"


def fetch_html(url: str = SOURCE_URL, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://news.zhibo8.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def parse_list(page: str) -> list[dict]:
    """解析列表页：标题去标签/反转义、URL 补 https、按 URL 去重。"""
    items, seen = [], set()
    for m in _A_RE.finditer(page):
        url = "https:" + m.group(1)
        if url in seen:
            continue
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        if len(title) < 6:
            continue
        seen.add(url)
        items.append({"date": m.group(2), "title": title, "url": url})
    return items


def team_variants(predictions: list[dict]) -> dict[str, set[str]]:
    """预测队名 → 新闻里可能出现的写法。

    来源：原文、normalize_team 规范名、TEAM_ALIASES 反查（曼联/尤文/马竞等简称）、
    挑战池译名反查（萨迪纳摩↔萨格勒布迪纳摩）。只做关键词匹配，不做实体消歧。
    """
    try:
        from challenge_hourly import _POOL_ALIAS
        rev_pool = {v: k for k, v in _POOL_ALIAS.items()}
    except Exception:
        rev_pool = {}
    out: dict[str, set[str]] = {}
    for p in predictions or []:
        for team in (p.get("home_team"), p.get("away_team")):
            if not team or team in out:
                continue
            canon = normalize_team(team)
            names = {team, canon}
            names |= {k for k, v in TEAM_ALIASES.items() if v == canon}
            for key in (team, canon):
                if key in rev_pool:
                    names.add(rev_pool[key])
            out[team] = {n for n in names if len(n) >= 2}
    return out


# 2026-11-04：存储上限 40 → 120。实测 14 天里有 6 天条数正好卡在 40，说明按场次的
# 新闻覆盖率（45.2%）是被采集截断低估的，不是源不足。展示层只取前 12 条，提高存储
# 上限不影响页面。limit=None 表示不截断，供 main 记录截断前的匹配总数。
NEWS_LIMIT = 120


def match_items(items: list[dict], predictions: list[dict], target: str,
                days: int = 1, limit: int | None = NEWS_LIMIT) -> list[dict]:
    t0 = date.fromisoformat(target)
    window = {(t0 + timedelta(days=i)).isoformat() for i in range(-days, days + 1)}
    variants = team_variants(predictions)
    by_team: dict[str, list[str]] = {}
    for p in predictions or []:
        for team in (p.get("home_team"), p.get("away_team")):
            if team:
                by_team.setdefault(team, []).append(p.get("match_id", ""))
    matched = []
    for it in items:
        if it["date"] not in window:
            continue
        teams = [t for t, names in variants.items() if any(n in it["title"] for n in names)]
        if not teams:
            continue
        mids = sorted({m for t in teams for m in by_team.get(t, []) if m})
        matched.append({**it, "teams": teams, "match_ids": mids,
                        "kind": classify(it["title"])})
    # 最新在前，再按类别稳定排序：伤病/首发 → 其他 → 转会传闻
    matched.sort(key=lambda x: (x["date"], x["title"]), reverse=True)
    matched.sort(key=lambda x: KIND_ORDER.get(x["kind"], 1))
    return matched if limit is None else matched[:limit]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(BJT).date().isoformat())
    args = ap.parse_args(argv)

    pred_file = ROOT / "data" / "daily" / args.date / "predictions.json"
    try:
        raw = json.loads(pred_file.read_text(encoding="utf-8"))
        preds = raw if isinstance(raw, list) else raw.get("predictions", [])
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ⚠ 新闻采集跳过：读不到 {pred_file}: {e}")
        return 1

    try:
        page = fetch_html()
    except Exception as e:
        print(f"  ⚠ 新闻源抓取失败（{SOURCE_NAME}）: {type(e).__name__}: {e}")
        return 1

    items = parse_list(page)
    all_matched = match_items(items, preds, args.date, limit=None)
    matched = all_matched[:NEWS_LIMIT]
    counts = {k: sum(1 for x in matched if x["kind"] == k) for k in KIND_ORDER}
    out = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S"),
        "source": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "total_scanned": len(items),
        # 记下截断前的总数与上限：否则覆盖率会被上限蒙住，看不出是源不足还是我们截断了
        "matched_total": len(all_matched),
        "limit": NEWS_LIMIT,
        "counts": counts,
        "items": matched,
    }
    out_file = ROOT / "data" / "daily" / args.date / "news.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    cut = f"（匹配 {len(all_matched)} 条，截断至 {NEWS_LIMIT}）" if len(all_matched) > NEWS_LIMIT else ""
    print(f"  ✓ 赛前情报: {len(matched)} 条匹配 / 扫描 {len(items)} 条{cut} → {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
