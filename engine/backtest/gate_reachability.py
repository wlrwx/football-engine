#!/usr/bin/env python3
"""E3：决策层闸门（edge / EV / Kelly）到底能不能被"调参"救回来？（2026-10-11）

## 这个问题为什么值得单独做一个实验

E0/E1/E2 都指向同一个结论：去水收盘价不可逾越。于是自然会有人问
"那决策层的参数是不是调错了？edge 阈值、Kelly 系数、闸门，是不是调一调就有正 EV？"

E3 就是来回答这个问题的。结论先放前面：

> **不能 —— 但原因不是「阈值调小了」。** w=0.10 相对抽水 12.1% 其实不算小：
> 即便模型完全正确，EV 闸门所需权重中位也只有 0.064，72% 的选项够得着。
> 真正锁死闸门的是**模型质量**：换成实测模型，所需权重中位飙到 0.55，
> 只有 5% 的场次够得着配置值（与 E0 的 Brier Δ=+0.02018, t=+34.74 一致）。
>
> 所以降阈值救不回来 —— 那只是把不可达的闸门强行打开，去下负期望的注。

## 数学

`engine/strategy/kelly.py` 的主路径判定：

    market_prob = 1.0 / odds            # 原始赔率倒数，**含抽水**
    edge = model_prob - market_prob
    ev   = model_prob * odds - 1.0
    if edge < min_probability_edge or ev < min_ev: continue

设 p_system = w·p_dc + (1−w)·p_fair，odds = 1/(p_fair·(1+v))。

**EV 闸门**要求 p_system·odds ≥ 1 + min_ev，代入得

    w ≥ p_fair·[(1+min_ev)(1+v) − 1] / (p_dc − p_fair)

括号里的量在 v=0.121、min_ev=0.03 时是 0.1546。所以：

- 模型必须**优于**市场（p_dc > p_fair）才可能触发；
- 需要的权重通常远大于配置的 0.10（实测中位 0.550）；
- E0 已证明模型显著**劣于**市场（Brier Δ=+0.02018, t=+34.74）。

**edge 闸门**额外踩了一个坑：它拿 `1/odds` 当"市场怎么看"，而系统其它所有
模块（`clv.py` / `upgrade_tracker.py` / `post_match.py` / 账本的 `market_fair`）
用的都是**去水**概率。于是 `min_probability_edge=0.03` 实际强制的是

    p_model − p_fair ≥ 0.03 + p_fair·v

对 p_fair=0.4 的热门选项，v=0.121 → 实际要求 ≥ 0.0784，比参数名暗示的严 2.6 倍。
这不是"更安全"，是**参数名与行为不符**：没人能按字面理解这个阈值。

## 用法

    python -m engine.backtest.gate_reachability            # 全量
    python -m engine.backtest.gate_reachability --ledger   # 附带账本实测的模型-市场差
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from engine.backtest.historical_replay import load_football_data, devig  # noqa: E402

OUT_JSON = ROOT / "data" / "state" / "gate_reachability.json"
LEDGER = ROOT / "data" / "state" / "review_ledger.jsonl"

# 竞彩 1X2 实测 overround 中位数 1.1212（71,838 行样本）
VIG = 0.121
# config/prediction.json 的 fusion.model_weight
W_CONFIG = 0.10
# config/strategy.json 的 edge_gates
MIN_EDGE = 0.03
MIN_EV = 0.03


def required_weight(p_fair: float, p_dc: float, vig: float = VIG,
                    min_ev: float = MIN_EV) -> float:
    """EV 闸门通过所需的模型权重。模型不优于市场时返回负数（需反向跟随）。

    解 w ≥ p_fair·[(1+min_ev)(1+vig) − 1] / (p_dc − p_fair)。
    分母为 0 时返回 +inf（权重再大也没用）。
    """
    den = p_dc - p_fair
    if abs(den) < 1e-12:
        return float("inf") if p_fair > 0 else 0.0
    return p_fair * ((1.0 + min_ev) * (1.0 + vig) - 1.0) / den


def edge_gate_effective(p_fair: float, vig: float = VIG,
                        min_edge: float = MIN_EDGE) -> float:
    """edge 闸门实际强制的"相对去水市场"的最小 edge。

    因为 kelly.py 用 1/odds（含抽水）而非去水概率，实际门槛是
    min_edge + p_fair·vig —— 参数名说 0.03，行为上不是。
    """
    return min_edge + p_fair * vig


def analyse_matches(matches) -> dict:
    """在历史收盘价上，统计 EV 闸门的可达性。"""
    rows = [m for m in matches if m.close and m.open_]
    n_sel = 0
    reachable = 0
    req_pos: list[float] = []
    for m in rows:
        pf = devig(m.close, "proportional")
        for i in range(3):
            n_sel += 1
            p_fair = pf[i]
            if not (0 < p_fair < 1):
                continue
            # 最有利假设：模型完全正确 p_dc=1
            w_need = required_weight(p_fair, 1.0)
            if w_need <= W_CONFIG:
                reachable += 1
            if 0 < w_need < float("inf"):
                req_pos.append(w_need)
    return {
        "n_matches": len(rows),
        "n_selections": n_sel,
        "ev_gate_reachable_share": round(reachable / n_sel, 4) if n_sel else None,
        "ev_gate_required_weight": {
            "assumption": "p_dc=1.0（模型完全正确，已是理论最优）",
            "median": round(statistics.median(req_pos), 4) if req_pos else None,
            "mean": round(statistics.mean(req_pos), 4) if req_pos else None,
            "config_weight": W_CONFIG,
        },
    }


def analyse_ledger(path: Path = LEDGER) -> dict | None:
    """从账本反解真实的 (p_dc − p_fair) 与所需权重。

    账本同时存了 model_raw（模型原始）、market_fair（去水市场）、final_prob（融合后），
    所以能直接读出模型在"它自己最看好的方向"上偏离市场多少。
    """
    if not path.exists():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    diffs, reqs = [], []
    for r in rows:
        mr, mf = r.get("model_raw"), r.get("market_fair")
        if not (mr and mf):
            continue
        i = max(range(3), key=lambda k: mr[k])
        p_fair, p_dc = mf[i], mr[i]
        if not (0 < p_fair < 1):
            continue
        d = p_dc - p_fair
        diffs.append(d)
        w = required_weight(p_fair, p_dc)
        if 0 < w < float("inf"):
            reqs.append(w)
    if not diffs:
        return None
    worse = sum(1 for d in diffs if d < 0)
    return {
        "n_rows": len(diffs),
        "model_minus_market": {
            "mean": round(statistics.mean(diffs), 4),
            "median": round(statistics.median(diffs), 4),
            "share_model_worse": round(worse / len(diffs), 4),
        },
        "ev_gate_required_weight": {
            "note": "只统计 p_dc>p_fair（模型确实更看好）的场次；其余需负权重",
            "n": len(reqs),
            "median": round(statistics.median(reqs), 4) if reqs else None,
            "share_within_config": round(
                sum(1 for w in reqs if w <= W_CONFIG) / len(reqs), 4) if reqs else None,
        },
    }


def verdict(res: dict) -> str:
    """判定。注意两个数字必须一起读，单独看任一个都会得出相反结论。"""
    hist = res.get("historical") or {}
    led = res.get("ledger") or {}
    hist_med = (hist.get("ev_gate_required_weight") or {}).get("median")
    hist_share = hist.get("ev_gate_reachable_share")
    led_block = led.get("ev_gate_required_weight") or {}
    led_med = led_block.get("median")
    led_share = led_block.get("share_within_config")

    parts = [
        "E3 未通过：决策层闸门**不是**调错了，而是在 model_weight=0.10 + 抽水 12.1% "
        "下被模型质量锁死。两个数字必须一起读——"
    ]
    if hist_med is not None:
        parts.append(
            f"(a) 即便模型完全正确(p_dc=1，理论最优)，EV 闸门所需权重中位仅 {hist_med}，"
            f"低于配置的 {W_CONFIG}，可达 {hist_share*100:.1f}% 的选项；"
            f"也就是说 w=0.10 本身不算小，抽水才是主要门槛。")
    if led_med is not None:
        parts.append(
            f"(b) 但换成实测模型，所需权重中位飙到 {led_med}，"
            f"只有 {led_share*100:.1f}% 的场次够得着配置值。"
            f"差距来自 E0 已实测的模型劣于市场(Brier Δ=+0.02018, t=+34.74)。")
    parts.append(
        "因此调 min_ev/min_edge 救不回来：降阈值只是把不可达的闸门强行打开，"
        "让系统去下负期望的注，不产生正 EV。"
        "另有一处口径问题：kelly.py 的 edge 用含抽水的 1/odds 计算，"
        "而 clv.py / upgrade_tracker.py / post_match.py / 账本 market_fair 全用去水概率，"
        "导致 min_probability_edge 的实际门槛比参数名暗示的严 1.4~2.6 倍 —— "
        "参数名与行为不符，需要人来决定是改口径还是改文档。")
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", action="store_true", help="附带账本实测的模型-市场偏离")
    args = ap.parse_args()

    print("=" * 72)
    print("E3：决策层闸门可达性")
    print("=" * 72)
    print(f"  参数: model_weight={W_CONFIG}  抽水={VIG:.1%}  "
          f"min_edge={MIN_EDGE}  min_ev={MIN_EV}")

    res: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": {"model_weight": W_CONFIG, "vig": VIG,
                   "min_probability_edge": MIN_EDGE, "min_ev": MIN_EV},
    }

    print("\n--- 历史收盘价上的可达性（假设模型完全正确 p_dc=1）---")
    hist = analyse_matches(load_football_data())
    res["historical"] = hist
    print(f"  样本 {hist['n_matches']} 场 / {hist['n_selections']} 个 1X2 选项")
    rw = hist["ev_gate_required_weight"]
    print(f"  EV 闸门可达占比: {hist['ev_gate_reachable_share']*100:.2f}%")
    print(f"  所需权重中位 {rw['median']}（配置 {rw['config_weight']}）")

    print("\n--- edge 闸门的实际强度（含抽水口径的代价）---")
    print(f"  {'p_fair':>9}{'参数名':>9}{'实际门槛':>11}{'倍数':>8}")
    edge_rows = []
    for pf in (0.10, 0.15, 0.20, 0.30, 0.40):
        eff = edge_gate_effective(pf)
        edge_rows.append({"p_fair": pf, "nominal": MIN_EDGE,
                          "effective": round(eff, 4),
                          "ratio": round(eff / MIN_EDGE, 2)})
        print(f"  {pf:>9.2f}{MIN_EDGE:>9.2f}{eff:>11.4f}{eff/MIN_EDGE:>7.2f}x")
    res["edge_gate"] = {"rows": edge_rows,
                        "note": "kelly.py 用 1/odds（含抽水），系统其它模块用去水概率"}

    if args.ledger:
        print("\n--- 账本实测：模型在自己最看好的方向上偏离市场多少 ---")
        led = analyse_ledger()
        if led:
            res["ledger"] = led
            mm = led["model_minus_market"]
            print(f"  n={led['n_rows']}  p_dc−p_fair 中位 {mm['median']:+.4f}"
                  f"（均值 {mm['mean']:+.4f}）")
            print(f"  模型更差的场次占比: {mm['share_model_worse']*100:.1f}%")
            rq = led["ev_gate_required_weight"]
            if rq["n"]:
                print(f"  需正权重的 n={rq['n']}，中位 {rq['median']}，"
                      f"其中 ≤{W_CONFIG} 的仅 {rq['share_within_config']*100:.1f}%")
        else:
            print("  账本不可读或无 model_raw/market_fair 字段")

    res["verdict"] = verdict(res)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 已写入 {OUT_JSON.relative_to(ROOT)}")
    print(f"  判定: {res['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
