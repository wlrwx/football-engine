#!/usr/bin/env python3
"""NJStats 回测策略采集（store-only，2026-11-08 协议逆向）

站点 2026-11 上线的「自建策略回测」把官方 5 个基础策略与用户自建策略的
回测结果放在 modId=1028 网关里。本脚本只读、只落盘，不参与任何下单判断。

为什么值得每天存一份：
1. 策略定义（conditions JSON）是站点给出的**唯一**成体系的筛选口径，
   我们的 E8/E10 池需要外部对照物才能判断"我们的门槛是不是太松/太紧"。
2. 回测汇总与联赛分布**没有历史接口**——每次请求返回的都是"截至当下"的
   全量重算。不自己攒，日后就没法知道站点当时报了多少。
3. 自建策略页签（f1{1:2}）在本账号返回空：非会员建不了策略。这条也要落盘，
   否则升级会员后没法区分"从来没建过"和"建了又删了"。

口径（2026-11-08 用 5 个策略的真实响应互验，非推测）：
    胜率        = 胜场 / (总场 − 走盘)
    场均收益率  = 累计收益 / (总场 − 走盘) × 100
    客队不败：251/(442−76)=68.58 ✓   85.17/366×100=23.27 ✓
   五个策略两条恒等式全部零误差。解不出 UI 对应值的字段一律存成 raw_fN，
   不编标签。

边界（本脚本不会做的事）：
  · 不绕过会员墙。自建策略页签返回空就记空，不去撞任何写入端点。
  · 不进模型。这些是**站点赔率口径**的回测，按市场隔离纪律只能做展示/对照，
    绝不能当特征（见 docs/EXPERIMENTS.md 与 tests/test_challenge_firewall.py）。
  · 不改任何门槛。

用法:
    .venv/bin/python3 scripts/fetch_njstats_strategies.py            # 抓并落盘
    .venv/bin/python3 scripts/fetch_njstats_strategies.py --check    # 只探测不写
    .venv/bin/python3 scripts/fetch_njstats_strategies.py --force-detail  # 强写本周明细

输出:
    data/state/njstats/strategies/<YYYY-MM-DD>.json       每日：定义 + 汇总（~4KB）
    data/state/njstats/strategies/detail/<YYYY-Www>.json  每周：联赛分布 + 周/月序列
    （明细单份约 160KB，所以只在每周第一天写一次。）
失败保旧：任何一步取不到数都不覆盖已有文件，退出码非 0。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import njstats_api_submit as api  # noqa: E402

BJT = timezone(timedelta(hours=8))
OUT_DIR = ROOT / "data" / "state" / "njstats" / "strategies"
DETAIL_DIR = OUT_DIR / "detail"
TABS = ("base", "mine")

FIELD_NOTES = {
    "total": "总场次（站点口径，含走盘）",
    "wins": "赢盘场次",
    "push": "走盘/退款场次",
    "avg_odds": "平均指数（UI「平均指数」列，即平均赔率）",
    "win_rate": "胜率% = wins/(total-push)，已对 UI 校验",
    "profit": "表现（UI「表现」列，累计收益，单位=每场 1 注金）",
    "roi_pct": "场均收益率% = profit/(total-push)×100，已对 UI 校验",
    "raw_f5": "未定口径：恒有值，但既不是负场（total-wins-push 不等）"
              "也不是联赛数，疑为半赢/半输场次",
    "raw_f12": "未定口径：五个策略同为 5，疑似请求回显",
    "raw_f13": "未定口径：恒为 50，疑似页大小回显",
    "raw_f15": "未定口径：0/1/3 小整数，疑似标记位",
    "hit_league_sids": "该策略命中的联赛 sid 列表（与 leagues[].league_sid 同命名空间）",
    "leagues": "按联赛拆的回测行；胜率% = wins/(total-push)（逐行校验过）",
    "series.weekly": "周序列，period=YYYYWW",
    "series.monthly": "月序列，period=YYYYMM",
}


def _jwt_from_disk() -> str:
    try:
        return api.JWT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def week_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


DETAIL_KEYS = ("leagues", "series", "hit_league_sids")


def _entry(row: dict, tab: str, jwt: str) -> tuple[dict, dict]:
    """列表条目 → (每日汇总, 每周明细)。一个策略取不到回测就把原因写进
    条目里而不是整份丢掉——快照必须能说出「哪个策略坏了」。"""
    e = {"sid": row["sid"], "name": row["name"], "tab": tab,
         "conditions": row["conditions"], "win_rate_claimed": row["win_rate"],
         "strategy_no": row["strategy_no"], "backtest": None, "backtest_error": None}
    try:
        bt = api.strategy_backtest(row["sid"], jwt)
    except Exception as ex:  # noqa: BLE001 - 网关异常种类不受我们控制
        e["backtest_error"] = type(ex).__name__
        return e, {}
    if bt is None:
        e["backtest_error"] = "no_detail"
        return e, {}
    e["backtest"] = {k: v for k, v in bt.items() if k not in DETAIL_KEYS}
    return e, {k: bt.get(k) for k in DETAIL_KEYS}


def collect(jwt: str) -> tuple[dict, bool]:
    """拉两个页签的策略与回测 → (快照 dict, 是否全部成功)。"""
    strategies: list[dict] = []
    details: list[dict] = []
    tab_counts: dict[str, int | None] = {}
    ok = True
    for tab in TABS:
        rows = api.strategy_list(tab, jwt)
        if rows is None:
            tab_counts[tab] = None
            ok = False
            continue
        tab_counts[tab] = len(rows)
        for r in rows:
            e, det = _entry(r, tab, jwt)
            if e["backtest_error"]:
                ok = False
            strategies.append(e)
            if det:
                details.append({"sid": e["sid"], "name": e["name"], "tab": tab, **det})
    return {"tab_counts": tab_counts, "strategies": strategies,
            "details": details}, ok


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只探测，不写盘")
    ap.add_argument("--force-detail", action="store_true", help="本周明细已存在也重写")
    args = ap.parse_args(argv)

    jwt = _jwt_from_disk()
    if not jwt:
        print("strategies: 缺 JWT，跳过（不落盘、不覆盖旧快照）")
        return 1

    now = datetime.now(BJT)
    day = now.strftime("%Y-%m-%d")
    wk = week_key(now)
    detail_path = DETAIL_DIR / f"{wk}.json"
    need_detail = args.force_detail or not detail_path.exists()

    got, ok = collect(jwt)
    tabs = got["tab_counts"]
    if args.check:
        print(f"strategies: 探测 base={tabs.get('base')} mine={tabs.get('mine')} "
              f"明细待写={need_detail} 完整={'是' if ok else '否'}")
        return 0 if ok else 1

    payload = {
        "snapshot_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "modId=1028 msgId=0/2（站点自建策略回测）",
        "note": ("store-only：站点口径的回测与策略定义，只作展示与外部对照，"
                 "不是模型特征、不参与下单门槛。站点每次返回的都是截至当下的全量重算。"),
        "tabs": tabs,
        "mine_empty_means": "非会员无法创建自建策略；0 条是权限状态，不是取数失败",
        "field_notes": FIELD_NOTES,
        "detail_week": wk,
        "detail_written": need_detail,
        "strategies": got["strategies"],
    }
    try:
        _write(OUT_DIR / f"{day}.json", payload)
        if need_detail:
            _write(detail_path, {
                "snapshot_at": payload["snapshot_at"],
                "week": wk,
                "note": "按 ISO 周存一份：联赛分布与周/月序列体积大（~160KB），"
                        "日间几乎不变，所以每日只存汇总、明细每周一次。",
                "strategies": got["details"],
            })
    except OSError as e:
        print(f"strategies: 写盘失败 {e}")
        return 1

    bad = [s["name"] for s in got["strategies"] if s["backtest_error"]]
    print(f"strategies: base={tabs.get('base')} mine={tabs.get('mine')} "
          f"明细={'写' if need_detail else '跳过'}"
          + (f" 回测缺失={len(bad)}({'/'.join(bad)})" if bad else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
