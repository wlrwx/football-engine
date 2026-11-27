#!/usr/bin/env python3
"""NJStats 侧挂信号 —— 模型层唯一允许读 NJStats 旁路数据的入口。

## 侧挂 ≠ 热插拔

侧挂：常驻在旁路上的数据/模块。有就读，缺了就返回 None，引擎不需要改一行
代码就能继续跑。热插拔：可以随时替换数据源或实现而不改变输出——本项目
**不做**这个承诺，所以入口收敛成这一个文件，换源必须改这里。

## 允许进什么，禁止进什么

允许（赛前公开输入，且能过市场隔离）：
- NJStats 开盘赔率（去水后作 reference 市场特征）
- 主队主场 / 客队客场的分离统计（ppg、场均进失球）

禁止（裁判产物 = 对我们的判决，进了就是自己批卷，清单见 docs/CHALLENGE_SUBSYSTEM.md）：
- `challenge_` 前缀的状态 / 复盘 / 策略三件套
- 本站结算结果、`dom_tendency`（站点方向真值）

站点赔率（`site_odds/`）虽然可以读，但它是**目标市场**——E8 之后我们正是
拿它当结算价下注。按 `engine/prediction/market_isolation.py` 的口径，
目标市场派生值不得进特征，只能留在策略层（闸门/EV）。这里把它读出来只是
为了给 `fe-agent status` 报覆盖率，`to_features()` 不会吐它。

## 已知数据缺口（2026-10-22 实测）

社区票 `distribution[].code` 是**市场内选项序号**，快照里没有市场标识，
13 个 code 横跨 1x2/大小球/双方进球等多个市场，无法可靠映射成 1x2 概率。
唯一带 1x2 方向的是 `dom_tendency`，而它是方向真值。所以社区票当前只能
统计"有多少人、热不热闹"，不能进模型。要解开这个缺口，得在采集侧把市场
名写进快照（`scripts/njstats_capture.py`），不是模型侧能补的。

## 用法

    python -m engine.sources.njstats_side --report          # 覆盖率报告
    python -m engine.sources.njstats_side --date 2026-10-21 # 单日明细
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.beijing_time import beijing_now  # noqa: E402
from engine.team_aliases import normalize_team  # noqa: E402

NJ = ROOT / "data" / "state" / "njstats"
FEATURES = ROOT / "data" / "state" / "njstats_features"

# 社区票要多少人以上才算"有信号"（低于此值当作噪声，不统计）
MIN_CROWD_PEOPLE = 5
# 快照最多领先开赛多少小时仍可用（更早的快照当成过期，不追）
MAX_LEAD_H = 48.0


@dataclass(frozen=True)
class SideSignal:
    home: str
    away: str
    kickoff: str
    snapshot_at: str
    # reference 市场：开盘去水概率（允许进特征）
    open_fair: tuple[float, float, float] | None = None
    # 主队主场 / 客队客场的分离统计（允许进特征）
    home_ppg_home: float | None = None
    away_ppg_away: float | None = None
    # 目标市场：站点价（只给策略层/报表，不进特征）
    site_fair: tuple[float, float, float] | None = None
    # 社区热度（无法映射 1x2，只报人数）
    crowd_people: int = 0
    crowd_has_1x2: bool = False

    def features_as_of(self, as_of: str) -> dict[str, float] | None:
        """训练侧用：快照必须早于当时的预测时刻，否则算偷看未来。"""
        a = _parse_ts(as_of)
        if a is not None:
            s = _parse_ts(self.snapshot_at)
            if s is None or s >= a:
                return None
        return self.to_features()

    def to_features(self) -> dict[str, float]:
        """能进模型的那部分，列名以 side_ 开头，与主特征不撞。"""
        out: dict[str, float] = {}
        if self.open_fair:
            out["side_open_home"] = self.open_fair[0]
            out["side_open_draw"] = self.open_fair[1]
            out["side_open_away"] = self.open_fair[2]
        if self.home_ppg_home is not None and self.away_ppg_away is not None:
            out["side_ppg_home"] = self.home_ppg_home
            out["side_ppg_away"] = self.away_ppg_away
            out["side_ppg_diff"] = self.home_ppg_home - self.away_ppg_away
        return out


def _parse_ts(s: str) -> datetime | None:
    text = str(s or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(iso_text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _row_kickoff(row: dict) -> datetime | None:
    """站点价快照用 `kick_ts`（epoch 秒），社区/特征用 `kickoff` 字符串。"""
    ts = _parse_ts(row.get("kickoff", ""))
    if ts is not None:
        return ts
    try:
        return datetime.fromtimestamp(int(row.get("kick_ts")), tz=timezone.utc).replace(
            tzinfo=None) + timedelta(hours=8)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _devig(odds: list | tuple | None) -> tuple[float, float, float] | None:
    try:
        h, d, a = (float(x) for x in odds)  # type: ignore[misc]
    except (TypeError, ValueError):
        return None
    if min(h, d, a) <= 1.0:
        return None
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    if inv <= 0:
        return None
    return ((1.0 / h) / inv, (1.0 / d) / inv, (1.0 / a) / inv)


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _rows(kind: str, date: str = "") -> list[dict]:
    """按快照时间升序返回某个侧挂数据集的行，附 `_snapshot`。"""
    if kind == "features":
        files = sorted(FEATURES.glob(f"{date or '*'}.json"))
    else:
        files = sorted((NJ / kind).glob(f"{date or '*'}.json"))
    out: list[dict] = []
    for f in files:
        doc = _read_json(f)
        if kind == "features":
            for r in doc.get("rows") or []:
                out.append({**r, "_snapshot": r.get("snapshot_at", "")})
        elif kind == "site_odds":
            snap = doc.get("snapshot_at", "")
            for r in doc.get("matches") or []:
                out.append({**r, "_snapshot": snap})
        elif kind == "community":
            snap = doc.get("snapshot_at", "")
            for r in doc.get("matches") or []:
                out.append({**r, "_snapshot": snap})
    out.sort(key=lambda r: _parse_ts(r.get("_snapshot", "")) or datetime.min)
    return out


def key(home: str, away: str) -> tuple[str, str]:
    return normalize_team(str(home or "")), normalize_team(str(away or ""))


def _eligible(row: dict, kickoff: datetime | None) -> bool:
    """时点护栏：快照必须早于开赛，且不能早太多（过期快照不追）。"""
    snap = _parse_ts(row.get("_snapshot", ""))
    if snap is None or kickoff is None:
        return False
    if snap >= kickoff:
        return False
    return (kickoff - snap).total_seconds() / 3600.0 <= MAX_LEAD_H


def build_index(date: str = "") -> dict[tuple[str, str], SideSignal]:
    """把三个数据集按 (主,客) 合成，只保留满足时点护栏的行。

    同一场比赛有多份快照时取**最晚**的一份（最接近开赛、信息最全）。
    """
    idx: dict[tuple[str, str], dict] = {}

    def slot(row: dict) -> dict | None:
        ko = _row_kickoff(row)
        if not _eligible(row, ko):
            return None
        k = key(row.get("home", ""), row.get("away", ""))
        if not all(k):
            return None
        return idx.setdefault(k, {"home": row.get("home"), "away": row.get("away"),
                                  "kickoff": row.get("kickoff", "")})

    for r in _rows("features", date):
        s = slot(r)
        if s is None:
            continue
        opening = r.get("opening")
        if opening:
            fair = _devig([opening.get("home"), opening.get("draw"), opening.get("away")])
            if fair:
                s["open_fair"] = fair
        hs = (r.get("home_stats") or {}).get("home") or {}
        as_ = (r.get("away_stats") or {}).get("away") or {}
        if hs.get("ppg") is not None and as_.get("ppg") is not None:
            s["home_ppg_home"] = float(hs["ppg"])
            s["away_ppg_away"] = float(as_["ppg"])
        s["snapshot_at"] = r["_snapshot"]

    for r in _rows("site_odds", date):
        s = slot(r)
        if s is None:
            continue
        fair = _devig(r.get("odds"))
        if fair:
            s["site_fair"] = fair
            s["snapshot_at"] = r["_snapshot"]

    for r in _rows("community", date):
        s = slot(r)
        if s is None:
            continue
        s["crowd_people"] = int(r.get("people") or 0)
        # 当前快照没有市场标识，无法把 code 映射到 1x2；保持 False 直到采集侧补齐
        s["crowd_has_1x2"] = False
        s["snapshot_at"] = r["_snapshot"]

    out: dict[tuple[str, str], SideSignal] = {}
    for k, s in idx.items():
        sig = SideSignal(
            home=s["home"] or "",
            away=s["away"] or "",
            kickoff=s["kickoff"],
            snapshot_at=s.get("snapshot_at", ""),
            open_fair=s.get("open_fair"),
            home_ppg_home=s.get("home_ppg_home"),
            away_ppg_away=s.get("away_ppg_away"),
            site_fair=s.get("site_fair"),
            crowd_people=s.get("crowd_people", 0),
            crowd_has_1x2=s.get("crowd_has_1x2", False),
        )
        # 只有"读到了东西"的行才算侧挂命中；空壳不进索引，免得覆盖率虚高
        if sig.open_fair or sig.site_fair or sig.crowd_people or sig.home_ppg_home is not None:
            out[k] = sig
    return out


def for_fixture(home: str, away: str, date: str = "") -> SideSignal | None:
    """单场查询。找不到或时点不合法 → None（侧挂缺席，调用方照常跑）。"""
    return build_index(date).get(key(home, away))


def _our_fixtures(date: str) -> list[tuple[str, str]]:
    """当天我们真正要预测的场次（有 predictions.json 才算）。"""
    p = ROOT / "data" / "daily" / date / "predictions.json"
    doc = _read_json(p)
    rows = doc.get("predictions", doc) if isinstance(doc, dict) else doc
    return [key(m.get("home_team", ""), m.get("away_team", ""))
            for m in (rows or []) if isinstance(m, dict)]


def coverage(date: str = "") -> dict:
    """覆盖率报告：每类侧挂信号到底能覆盖多少场。

    分母有两个：侧挂自己认识的场次（matches），和我们当天真的要预测的场次
    （fixtures）。后者才是"能不能进模型"的真分母——侧挂能覆盖一堆我们根本
    不预测的比赛，没有意义。
    """
    idx = build_index(date)
    n = len(idx)
    fixtures = _our_fixtures(date) if date else []
    joined = sum(1 for k in fixtures if k in idx)
    return {
        "date": date or "all",
        "matches": n,
        "fixtures": len(fixtures),
        "joined": joined,
        "open_fair": sum(1 for s in idx.values() if s.open_fair),
        "home_away_stats": sum(1 for s in idx.values() if s.home_ppg_home is not None),
        "site_fair": sum(1 for s in idx.values() if s.site_fair),
        "crowd_ge_min": sum(1 for s in idx.values() if s.crowd_people >= MIN_CROWD_PEOPLE),
        "crowd_1x2": sum(1 for s in idx.values() if s.crowd_has_1x2),
        "model_eligible": sum(1 for s in idx.values() if s.to_features()),
        "generated_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NJStats 侧挂信号覆盖率")
    ap.add_argument("--date", default="", help="YYYY-MM-DD，缺省为全部")
    ap.add_argument("--report", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    cov = coverage(args.date)
    if args.report:
        print(json.dumps(cov, ensure_ascii=False))
        return 0

    n = cov["matches"]
    if cov["fixtures"]:
        print(f"侧挂信号 {cov['date']}：{n} 场（已过时点护栏）｜当天预测 "
              f"{cov['fixtures']} 场，匹配上 {cov['joined']} 场")
    else:
        print(f"侧挂信号 {cov['date']}：合计 {n} 场（已过时点护栏）")
    for label, key in (("开盘赔率（reference，可进模型）", "open_fair"),
                       ("主客分离统计（可进模型）", "home_away_stats"),
                       ("站点价（目标市场，只给策略层）", "site_fair"),
                       (f"社区票 ≥{MIN_CROWD_PEOPLE} 人", "crowd_ge_min"),
                       ("社区票可映射 1x2（采集缺口）", "crowd_1x2")):
        v = cov[key]
        print(f"  {label:<34} {v:>5} / {n}" + (f"  ({v / n:.1%})" if n else ""))
    print(f"  {'能进模型（有任一 reference 特征）':<34} {cov['model_eligible']:>5} / {n}"
          + (f"  ({cov['model_eligible'] / n:.1%})" if n else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
