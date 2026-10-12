"""污染样本隔离（2026-10-11 新增）

## 背景

2026-08-30 ~ 10-10 期间，`league_draw_baseline` 步骤用一个错误的
"判平精度"值（巴甲 0.60，实际真实平局率是 0.266）把平局概率抬到 0.51，
锚定后落在 0.5025。该 bug 已修（config 开关关闭 + 代码 0.45 上限），
但已落盘的 20 场样本仍在 v2 洁净链里，而 v2 样本会喂给：

- `calibration_auto`（校准器拟合曲线）
- `lgbm_shadow`（影子训练）
- `upgrade_tracker`（Q1 判定）
- `ablation_replay`（融合链消融回放）

等于用一批"平局概率被人为钉在 0.5025"的样本教校准器，方向是系统性的。

## 为什么用"标记 + 过滤"而不是升 chain 版本

升 chain 会作废全部 558 条样本重来（2026-08-29 已经做过一次，
legacy 426 条被丢弃）。但污染只有 20 条（3.6%），成因明确、签名唯一。
按 match_id 精确剔除即可，不该让 538 条干净样本陪葬。

## 检测方式

权威来源是 `fusion_trace`，它记录了每一步对概率的实际改动。
账本本身没有 trace 字段，所以：

- 新增样本：结算时从 `fusion_trace` 直接判定，写 `contaminated` 字段。
- 历史样本：`build_quarantine()` 扫描 `data/daily/*/predictions.json`
  重建隔离名单，落到 `data/state/quarantine.json`，供学习侧过滤。

## 用法

    python -m engine.integrity.quarantine --rebuild

    from engine.integrity.quarantine import clean_records
    samples = clean_records(all_records, ROOT)
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent.parent
QUARANTINE_FILE = "state/quarantine.json"

# 融合链里会造成系统性污染的步骤 -> (步骤名, 允许的抬升后平局概率上限)
# 真实联赛平局率区间 0.20~0.33；抬到 0.45 以上必然是"判平精度"误当"平局率"。
BAD_STEPS: tuple[tuple[str, float], ...] = (
    ("league_draw_baseline", 0.45),
    ("league_draw_anchor", 0.40),
)


def detect_from_trace(trace: Iterable[dict] | None) -> bool:
    """从 fusion_trace 判定该场是否被污染。这是权威判定。"""
    if not trace:
        return False
    for step in trace:
        name = step.get("step")
        after = step.get("after")
        for bad_step, cap in BAD_STEPS:
            if name != bad_step or not after or len(after) != 3:
                continue
            try:
                draw = float(after[1])
            except (TypeError, ValueError):
                continue
            if draw > cap:
                return True
    return False


def build_quarantine(root: Path = ROOT) -> dict[str, Any]:
    """扫描历史 predictions.json，重建隔离名单。"""
    daily = root / "data" / "daily"
    hits: dict[str, dict[str, Any]] = {}
    scanned = 0
    for pf in sorted(daily.glob("*/predictions.json")):
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        preds = data.get("predictions", data) if isinstance(data, dict) else data
        if not isinstance(preds, list):
            continue
        scanned += 1
        for p in preds:
            if not isinstance(p, dict):
                continue
            if detect_from_trace(p.get("fusion_trace")):
                mid = p.get("match_id")
                if mid:
                    hits[mid] = {
                        "date": pf.parent.name,
                        "league": p.get("competition", ""),
                        "draw_prob": p.get("draw_prob"),
                    }
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rule": "fusion_trace 中 league_draw_baseline/anchor 抬升后平局概率越界",
        "scanned_days": scanned,
        "count": len(hits),
        "match_ids": hits,
    }
    dest = root / "data" / QUARANTINE_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_quarantine(root: Path = ROOT) -> set[str]:
    """读隔离名单（match_id 集合）。文件不存在返回空集，不阻断主流程。"""
    f = root / "data" / QUARANTINE_FILE
    if not f.exists():
        return set()
    try:
        return set(json.loads(f.read_text(encoding="utf-8")).get("match_ids", {}))
    except Exception:
        return set()


def contaminated_ids(records: Iterable[dict], root: Path = ROOT) -> set[str]:
    """找出被污染的 match_id。两条判据取并集：
    1. 记录自带 contaminated 字段（新增样本，结算时判定，最可靠）
    2. match_id 在隔离名单里（历史样本，由 build_quarantine 重建）
    """
    q = load_quarantine(root)
    out: set[str] = set()
    for r in records:
        if r.get("contaminated"):
            mid = r.get("match_id")
            if mid:
                out.add(mid)
        mid = r.get("match_id")
        if mid and mid in q:
            out.add(mid)
    return out


def clean_records(records: Iterable[dict], root: Path = ROOT) -> list[dict]:
    """学习侧统一入口：剔除污染样本。"""
    recs = list(records)
    bad = contaminated_ids(recs, root)
    return [r for r in recs if r.get("match_id") not in bad]


def main() -> int:
    ap = argparse.ArgumentParser(description="重建污染样本隔离名单")
    ap.add_argument("--rebuild", action="store_true", help="扫描并写入 quarantine.json")
    args = ap.parse_args()
    if not args.rebuild:
        ap.print_help()
        return 0
    out = build_quarantine(ROOT)
    print(f"扫描 {out['scanned_days']} 天，隔离 {out['count']} 场")
    for mid, info in list(out["match_ids"].items())[:25]:
        print(f"  {mid:<28} {info['date']} {info['league']:<8} draw={info['draw_prob']}")
    if out["count"] > 25:
        print(f"  ... 共 {out['count']} 场")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

