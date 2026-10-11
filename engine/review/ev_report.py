#!/usr/bin/env python3
"""EV 价值区评估（2026-08-04 新增，方法论源自 world-cup-predictor 的 ev_evaluator）

核心问题：为什么"胜率高还亏钱"？
答案：钱亏在押注结构，不在预测能力——热门赔率太低，赢1场只赚0.5，输1场亏1。

本模块对全部已结算预测按"赔率区间 × 联赛"分层计算 ROI，
识别价值区（ROI>0）与送钱区（ROI<-10%），产出报告供页面展示与 Kelly 参考。

产出：data/state/ev_report.json（build_site 读取渲染）
"""
from __future__ import annotations
import json
import glob
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent

# 与 league_report.classify_league 对齐（2026-08-14）：n<50 不给价值区/送钱区
# 定性——小样本 ROI 是噪声（changelog 2026-08-14 遗留项），禁投/推荐需实打实证据。
MIN_VERDICT_SAMPLES = 50

STRAT_CFG = ROOT / "config" / "strategy.json"


def _favorite_band_cfg() -> dict:
    try:
        return json.loads(STRAT_CFG.read_text(encoding="utf-8")).get("favorite_band", {})
    except Exception:
        return {}


def gate_passes(odds: float, band_cfg: dict) -> bool:
    """该赔率是否会真的被下注。

    2026-10-11 修正口径脱节：本报告原口径对**全部预测**算 ROI，而
    下注口径（main.py:1029 的 favorite_band_factor）会直接乘 0 掉热门区。
    两套口径不一致 → "L1/L2 送钱区"是**反事实**结论，系统实际并不下这些注。
    这里复用与执行层同一个闸门函数，把反事实口径和可下注口径分开呈现。
    """
    try:
        from engine.strategy.kelly import favorite_band_factor
        return favorite_band_factor(odds, band_cfg) > 0
    except Exception:
        return True


def layer_of(odds: float) -> str:
    """L1-L5 赔率分层（单一真源在 engine/odds_bands.py）。

    2026-10-11：此前本函数与 review/post_match.py 的 _odds_band 各写一份，
    边界不一致（这里 1.8/2.2，那里 2.0/3.0），导致账本里看不出 L3 缺口。
    现在统一委派，改边界只需改一处。
    """
    from engine.odds_bands import layer_of as _layer_of
    return _layer_of(odds)


def _load_settled_predictions(daily_root: Path) -> list:
    """扫描所有 daily 目录已结算的 predictions（含方向/赔率/赛果）"""
    rows = []
    for f in sorted(glob.glob(str(daily_root / "*" / "predictions.json"))):
        try:
            preds = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for p in preds:
            ah, aa = p.get("actual_home_score"), p.get("actual_away_score")
            if ah is None or aa is None:
                continue
            actual = "home" if ah > aa else ("draw" if ah == aa else "away")
            direction = p.get("direction")
            if not direction:
                probs = [p.get("home_win_prob", 0), p.get("draw_prob", 0), p.get("away_win_prob", 0)]
                direction = ["home", "draw", "away"][probs.index(max(probs))]
            odds = p.get(f"{direction}_odds") or 0
            if not odds:
                continue
            rows.append({
                "date": Path(f).parent.name,
                "league": p.get("competition", "?"),
                "direction": direction,
                "actual": actual,
                "odds": float(odds),
                "match_id": p.get("match_id", ""),
            })
    return rows


def build_report(daily_root: Path | None = None, out_path: Path | None = None) -> dict:
    daily_root = daily_root or ROOT / "data" / "daily"
    out_path = out_path or ROOT / "data" / "state" / "ev_report.json"
    rows = _load_settled_predictions(daily_root)

    layers = {k: {"n": 0, "hits": 0, "pnl": 0.0}
              for k in ["L1 大热(<1.5)", "L2 热(1.5-1.8)", "L3 中(1.8-2.2)",
                        "L4 冷(2.2-3.0)", "L5 深冷(≥3.0)"]}
    leagues = defaultdict(lambda: {"n": 0, "hits": 0, "pnl": 0.0})
    total = {"n": 0, "hits": 0, "pnl": 0.0}

    # 2026-10-11：闸门口径分流。反事实（全部预测）与实际可下注分两套统计。
    _band_cfg = _favorite_band_cfg()
    stakable = {k: {"n": 0, "hits": 0, "pnl": 0.0} for k in layers}
    stakable_total = {"n": 0, "hits": 0, "pnl": 0.0}

    for r in rows:
        won = r["direction"] == r["actual"]
        pnl = (r["odds"] - 1) if won else -1.0
        layers[layer_of(r["odds"])]["n"] += 1
        layers[layer_of(r["odds"])]["hits"] += won
        layers[layer_of(r["odds"])]["pnl"] += pnl
        leagues[r["league"]]["n"] += 1
        leagues[r["league"]]["hits"] += won
        leagues[r["league"]]["pnl"] += pnl
        total["n"] += 1
        total["hits"] += won
        total["pnl"] += pnl
        if gate_passes(r["odds"], _band_cfg):
            _L = stakable[layer_of(r["odds"])]
            _L["n"] += 1
            _L["hits"] += won
            _L["pnl"] += pnl
            stakable_total["n"] += 1
            stakable_total["hits"] += won
            stakable_total["pnl"] += pnl

    def _finalize(d: dict) -> dict:
        d["hit_rate"] = d["hits"] / d["n"] if d["n"] else 0
        d["roi"] = d["pnl"] / d["n"] if d["n"] else 0
        # 2026-08-14：verdict 加样本门槛（与 league_report 同口径）。
        # n<50 只给谨慎/观望，不产生"价值区/送钱区"标签 → 页面与 Kelly 不再被小样本噪声驱动。
        if d["n"] < 3:
            d["verdict"] = "样本不足"
        elif d["n"] < MIN_VERDICT_SAMPLES:
            d["verdict"] = "谨慎" if d["roi"] < 0 else "观望"
        else:
            d["verdict"] = ("价值区 ✅" if d["roi"] > 0
                            else "送钱区 ❌" if d["roi"] < -0.10 else "中性")
        return d

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": _finalize(dict(total)),
        "layers": {k: _finalize(v) for k, v in layers.items() if v["n"] > 0},
        "leagues": {k: _finalize(v) for k, v in sorted(
            leagues.items(), key=lambda x: x[1]["pnl"] / max(x[1]["n"], 1)) if v["n"] >= 3},
        # 2026-10-11：闸门口径分流。counterfactual_* 对全部预测算（历史口径），
        # stakable_* 只统计过了 favorite_band 闸门、系统真会下注的场次。
        # 两者必须分开看：反事实口径的"送钱区"若本身已被闸门禁投，就不是活的问题。
        "stakable": {
            "total": _finalize(dict(stakable_total)),
            "layers": {k: _finalize(v) for k, v in stakable.items() if v["n"] > 0},
            "gate": _band_cfg,
            "note": ("favorite_band 闸门生效后系统实际可下注的口径。"
                     "与 layers 的差异 = 被闸门禁投的场次（反事实口径）。"),
        },
        "takeaways": [],
    }

    # 自动生成结论（2026-08-14：只引用"有实据"的 verdict——n<50 不再出现在价值/送钱结论里）
    t = report["total"]
    report["takeaways"].append(
        f"整体 {t['n']} 场命中率 {t['hit_rate']*100:.1f}%，ROI {t['roi']*100:+.1f}%"
    )
    value_layers = [k for k, v in report["layers"].items() if v["verdict"] == "价值区 ✅"]
    bad_layers = [k for k, v in report["layers"].items() if v["verdict"] == "送钱区 ❌"]
    if value_layers:
        report["takeaways"].append(f"价值区: {', '.join(value_layers)}")
    if bad_layers:
        report["takeaways"].append(f"送钱区(回避): {', '.join(bad_layers)}")
    bad_leagues = [k for k, v in report["leagues"].items() if v["verdict"] == "送钱区 ❌"]
    if bad_leagues:
        report["takeaways"].append(f"送钱联赛(回避): {', '.join(bad_leagues)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    r = build_report()
    print(json.dumps(r, ensure_ascii=False, indent=2))
