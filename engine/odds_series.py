"""盘口水位时间序列 - 读取累积快照，计算水位变化特征

背景（2026-08-05 盘口系统修复）：
- 此前 odds_history 依赖新浪接口自带快照，系统自己不累积 → 单次 initial→current，
  无法识别"赛前资金流"的近期变化/加速，realtime 监控脚本也从没被调度（死代码）。
- 现在每次 fetch_sina_odds 都把当前快照 append 到 data/state/odds_series/<match_id>.jsonl，
  多次 run 累积成真实时间序列。本模块负责读序列、算特征。

特征定义（特征本身不预设有效/无效，结算后由账本累积验证命中率，数据说话）：
- points: 累积快照点数
- span_min: 首次到末次的时间跨度（分钟）
- overall_home/overall_away: 全程水位变化（末次 vs 首次，%）
- recent_home/recent_away: 近 2 条变化（%），识别赛前最后一小时资金流
- accel_home/accel_away: 加速 = recent - 全程斜率（%），正=近期资金加速流入
"""
from __future__ import annotations

import json
from pathlib import Path

SERIES_DIR = Path(__file__).resolve().parent.parent / "data" / "state" / "odds_series"


def load_series(match_id: str) -> list[dict]:
    """读取某场比赛的水位时间序列（按时间正序）。

    match_id 形如 "2026-08-14_周五001"。写入侧（fetch_sina_odds）用新浪数字
    matchId 作为文件名（如 3632615.jsonl），与竞彩 match_id 不匹配 —— 2026-08-14
    之前导致水位监控永远 points=0。这里做两层查找：
    1) 精确 match_id.jsonl；
    2) 兜底：按文件内容里的 match_no（"周五001"）+ 开赛日期反查。
    """
    if not match_id:
        return []
    path = SERIES_DIR / f"{match_id}.jsonl"
    if not path.exists():
        _no = match_id.split("_", 1)[-1] if "_" in match_id else ""
        _date = match_id[:10] if len(match_id) >= 10 and match_id[4] == "-" else ""
        if _no and SERIES_DIR.exists():
            _best = None
            _best_score = -1
            for cand in sorted(SERIES_DIR.glob("*.jsonl")):
                try:
                    _first = json.loads(cand.read_text(encoding="utf-8").splitlines()[0])
                except Exception:
                    continue
                if _first.get("match_no") != _no:
                    continue
                _mt = str(_first.get("match_time", ""))[:10]
                if not _date or not _mt:
                    continue
                # 必须同一天（或差 1 天，凌晨场）；否则是跨周的"周五001"，拒绝。
                try:
                    from datetime import datetime
                    _gap = abs((datetime.strptime(_mt, "%Y-%m-%d") - datetime.strptime(_date, "%Y-%m-%d")).days)
                except Exception:
                    continue
                if _gap > 1:
                    continue
                _score = 2 if _gap == 0 else 1
                if _score > _best_score:
                    _best_score = _score
                    _best = cand
            if _best is not None:
                path = _best
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue

    # 2026-08-30 跨周污染过滤：快照捕获日期必须落在 [销售日-3天, 销售日+1天]。
    # 序列文件按编号兜底匹配时，可能命中上周同编号（如"周日002"）场次的文件，
    # 其快照是上周另一场比赛的水位——展示出来就是错的数据（济州SK/曼联实例）。
    # 无效时间戳的快照同样剔除（无法定位时间的"变动"不可信）。
    _sale = match_id[:10] if len(match_id) >= 10 and match_id[4] == "-" else ""
    if _sale:
        from datetime import datetime as _dt
        try:
            _sale_d = _dt.strptime(_sale, "%Y-%m-%d").date()
        except Exception:
            return out
        kept = []
        for r in out:
            try:
                _ts_d = _dt.fromisoformat(str(r.get("ts", ""))[:19]).date()
            except Exception:
                continue
            if -3 <= (_ts_d - _sale_d).days <= 1:
                kept.append(r)
        out = kept
    return out


def series_features(match_id: str) -> dict:
    """计算水位时间序列特征。无序列/序列过短时返回空特征（不硬编信号）"""
    seq = load_series(match_id)
    feat: dict = {"points": len(seq)}
    if len(seq) < 2:
        return feat

    def _pct(a: float, b: float) -> float | None:
        if not a or not b or a <= 0:
            return None
        return round((b - a) / a * 100, 2)

    first, last = seq[0], seq[-1]
    fh = first.get("euro", {}).get("home")
    lh = last.get("euro", {}).get("home")
    fa = first.get("euro", {}).get("away")
    la = last.get("euro", {}).get("away")
    feat["overall_home"] = _pct(fh, lh)
    feat["overall_away"] = _pct(fa, la)

    # 近 2 条变化（赛前最新资金流）
    if len(seq) >= 2:
        prev, cur = seq[-2], seq[-1]
        ph = prev.get("euro", {}).get("home")
        ch = cur.get("euro", {}).get("home")
        pa = prev.get("euro", {}).get("away")
        ca = cur.get("euro", {}).get("away")
        feat["recent_home"] = _pct(ph, ch)
        feat["recent_away"] = _pct(pa, ca)

    # 时间跨度
    try:
        from datetime import datetime
        t0 = datetime.fromisoformat(first.get("ts", ""))
        t1 = datetime.fromisoformat(last.get("ts", ""))
        feat["span_min"] = round((t1 - t0).total_seconds() / 60, 1)
    except Exception:
        pass

    return feat


def monitor_status() -> dict:
    """监控状态汇总（页面展示：多少场在累积、总快照数）"""
    if not SERIES_DIR.exists():
        return {"matches": 0, "snapshots": 0}
    matches = 0
    snapshots = 0
    for p in SERIES_DIR.glob("*.jsonl"):
        matches += 1
        try:
            snapshots += len(p.read_text(encoding="utf-8").splitlines())
        except Exception:
            pass
    return {"matches": matches, "snapshots": snapshots}
