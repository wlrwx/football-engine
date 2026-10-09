"""诊断引擎：从真实数据里**主动挖出**系统性问题。

【为什么必须有这个文件】
上一版的 known_issues.py 是人工审计一次性产出的静态清单。闭环每轮只是
重放它 —— 也就是说，闭环自己**永远不会发现新问题**，它只会重读我喂的
13 条。这不是自进化，是自动复读。

本模块反过来：不等清单，直接从账本/预测落盘里扫描，输出结构化诊断。
它的产物才应该进 known_issues 注册表（或直接当候选），让「发现问题」
这一步从人工审计变成持续计算。

【设计原则】
  1. 每个诊断器必须给出**可复核的数字**，不能只说「感觉有问题」。
  2. 区分「事实性错误」（可自动修）与「效果不佳」（需统计裁决）。
  3. 同一问题重复出现才提；一次性噪声不构成诊断。
  4. 诊断只读数据，不改任何东西。

【为什么这些诊断值得自动化】
它们全部是「用系统自己的输出交叉验证系统自己」的类型 ——
聚合恒定性、单调性违反、市场对比退步、校准反常。这类模式
每天都在产生新数据，人工不可能天天盯。
"""

from __future__ import annotations

import collections
import math
import statistics


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def _entropy(probs) -> float:
    """香农熵（bits），用于检测分布坍缩。"""
    return -sum(p * math.log2(p) for p in probs if p and p > 0)


# --------------------------------------------------------------- 诊断器
# 每个函数返回 list[dict]；空列表 = 没发现问题。

def d_output_constant(ledger: list[dict], min_n: int = 8) -> list[dict]:
    """DIAG-OUTPUT-CONSTANT：输出概率退化成常数。

    症状：某个联赛/分桶下 final_prob 或 draw_prob 的方差趋近 0。
    危害：系统对该场景**完全没有区分度**，无论输入怎么变输出都一样。
    这就是「把概率钉死」的通用检测版 —— 巴甲 draw=0.5025 那类 bug
    会被它自动抓到，而不需要我知道巴甲这个具体案例。

    检测方法：按联赛分组，若组内 >= min_n 场，draw_prob 的极差
    （max-min）小于 0.02 判为退化成常数。
    """
    out = []
    by_league = collections.defaultdict(list)
    for r in ledger:
        lg = r.get("league")
        fp = r.get("final_prob")
        if lg and fp and len(fp) >= 3:
            by_league[lg].append(r)

    for lg, rs in by_league.items():
        if len(rs) < min_n:
            continue
        draws = [r["final_prob"][1] for r in rs]
        spread = max(draws) - min(draws)
        if spread < 0.02:
            out.append({
                "id": "DIAG-OUTPUT-CONSTANT",
                "severity": "blocking",
                "league": lg,
                "n": len(rs),
                "draw_prob_mean": round(statistics.mean(draws), 4),
                "draw_prob_spread": round(spread, 5),
                "detail": (f"{lg} {len(rs)} 场 draw_prob 极差仅 {spread:.4f}"
                           f"（均值 {statistics.mean(draws):.4f}）—— "
                           "输出已退化为常数，对输入无区分度"),
            })
    return out


def d_worse_than_market(ledger: list[dict], min_n: int = 30) -> list[dict]:
    """DIAG-WORSE-THAN-MARKET：融合结果比它的市场输入还差。

    逻辑：融合的输入里已经含市场公允概率。如果输出比市场输入差，
    说明融合层在**破坏**信息 —— 这比「模型不够强」严重得多，
    因为它证明系统的核心组件（融合层）有 bug。

    分联赛 + 整体都查。分联赛很重要：整体指标会把单联赛的灾难稀释掉
    （巴甲 final Brier 比市场差 0.109，但整体只体现为 +0.016）。
    """
    out = []

    def _cmp(rows, label):
        pairs = [(r["brier_market"], r["brier_final"]) for r in rows
                 if r.get("brier_market") is not None
                 and r.get("brier_final") is not None]
        if len(pairs) < min_n:
            return
        mkt = statistics.mean(p[0] for p in pairs)
        fin = statistics.mean(p[1] for p in pairs)
        worse = sum(1 for m, f in pairs if f > m)
        gap = fin - mkt
        if gap > 0.005:
            out.append({
                "id": "DIAG-WORSE-THAN-MARKET",
                "severity": "blocking" if gap > 0.02 else "high",
                "scope": label,
                "n": len(pairs),
                "brier_market": round(mkt, 4),
                "brier_final": round(fin, 4),
                "gap": round(gap, 4),
                "final_worse_frac": round(worse / len(pairs), 3),
                "detail": (f"{label}: final Brier {fin:.4f} 比市场 {mkt:.4f} "
                           f"差 {gap:+.4f}；{worse}/{len(pairs)} 场融合后变差"),
            })

    _cmp(ledger, "整体")
    by_league = collections.defaultdict(list)
    for r in ledger:
        if r.get("league"):
            by_league[r["league"]].append(r)
    for lg, rs in by_league.items():
        _cmp(rs, f"联赛:{lg}")
    return out


def d_calibration_inversion(ledger: list[dict], min_n: int = 5) -> list[dict]:
    """DIAG-CALIBRATION-INVERSION：校准曲线非单调 / 反向。

    概率校准要求：预测 40% 的事件应真实发生约 40%。若高概率段反而
    偏差更负（预测过度自信），说明有东西在系统性推高概率。
    combo_boost 就是这么表现的（0.4 档 -0.054，0.7 档 -0.076，反向最大）。

    按 prob_band 分桶算偏差，检查是否单调。
    """
    out = []
    bands = collections.defaultdict(list)
    for r in ledger:
        fp = r.get("final_prob")
        ai = r.get("actual_idx")
        if not fp or ai is None or len(fp) < 3:
            continue
        conf = max(fp)
        band = round(conf * 10) / 10
        bands[band].append(conf - (1.0 if ai == int(max(range(3), key=lambda i: fp[i]))
                                   else 0.0))

    pts = []
    for band in sorted(bands):
        vals = bands[band]
        if len(vals) < min_n:
            continue
        pts.append((band, len(vals), statistics.mean(vals)))

    if len(pts) < 3:
        return out

    # 单调性：置信度越高，偏差应越接近 0（不超过 0）
    violations = sum(1 for i in range(len(pts) - 1)
                     if pts[i + 1][2] < pts[i][2] - 0.01)
    if violations >= max(1, len(pts) // 2):
        desc = ", ".join(f"{b:.1f}档(n={n}):{d:+.3f}" for b, n, d in pts)
        out.append({
            "id": "DIAG-CALIBRATION-INVERSION",
            "severity": "high",
            "n": sum(n for _, n, _ in pts),
            "violations": violations,
            "bands": [{"band": b, "n": n, "bias": round(d, 4)} for b, n, d in pts],
            "detail": (f"校准曲线非单调（{violations} 处反向）：{desc}。"
                       "置信度越高偏差反而越负 = 系统在高概率段过度自信"),
        })
    return out


def d_dead_feature(ledger: list[dict], pred_inputs: dict | None = None,
                   min_n: int = 20) -> list[dict]:
    """DIAG-DEAD-FEATURE：声明生效但实际无效果的字段。

    检测：某个落盘字段在所有样本里取值恒定。恒定 = 该字段对应的
    计算分支从未被真实数据触发 —— 配置里的权重是摆设。
    """
    out = []
    candidates = ["home_xg", "away_xg", "confidence_tier", "prob_band",
                  "freshness_risk", "goal_framework_hit", "market_signal_hit"]
    for fld in candidates:
        vals = {json_round(r.get(fld)) for r in ledger if r.get(fld) is not None}
        if 0 < len(vals) <= 1 and len(ledger) >= min_n:
            out.append({
                "id": "DIAG-DEAD-FEATURE",
                "severity": "medium",
                "field": fld,
                "n": len(ledger),
                "distinct_values": len(vals),
                "detail": (f"字段 {fld} 在 {len(ledger)} 场中取值恒为 "
                           f"{vals} —— 对应权重/分支从未生效"),
            })

    if pred_inputs:
        for key in ("injury", "rest_days"):
            vals = set()
            for p in pred_inputs.values():
                if isinstance(p, dict) and key in p:
                    vals.add(p[key])
            if len(vals) == 1 and len(pred_inputs) >= min_n:
                out.append({
                    "id": "DIAG-DEAD-FEATURE",
                    "severity": "medium",
                    "field": key,
                    "n": len(pred_inputs),
                    "distinct_values": 1,
                    "detail": f"预测输入 {key} 在 {len(pred_inputs)} 场中恒为 {vals}",
                })
    return out


def d_xg_drift(ledger: list[dict]) -> list[dict]:
    """DIAG-XG-DRIFT：模型 xG 系统性偏离实际总进球。

    误差方向一致且幅度大 → 模型有系统性偏置（不是噪声）。
    分联赛看，因为不同联赛进球水位差异很大。
    """
    out = []
    by_league = collections.defaultdict(list)
    for r in ledger:
        lg = r.get("league")
        tg = r.get("total_goals_actual")
        hx = r.get("home_xg")
        ax = r.get("away_xg")
        if lg and tg is not None and hx and ax:
            by_league[lg].append((hx + ax) - tg)

    for lg, diffs in by_league.items():
        if len(diffs) < 10:
            continue
        m = statistics.mean(diffs)
        sd = statistics.pstdev(diffs) if len(diffs) > 1 else 0.0
        if abs(m) < 0.30 or sd == 0:
            continue
        # 效应量：偏置相对于自身波动的倍数
        effect = m / sd
        if abs(effect) < 0.5:
            continue
        out.append({
            "id": "DIAG-XG-DRIFT",
            "severity": "high" if abs(effect) > 1.0 else "medium",
            "league": lg,
            "n": len(diffs),
            "mean_bias": round(m, 3),
            "sd": round(sd, 3),
            "effect_size": round(effect, 2),
            "detail": (f"{lg}: 预测总进球平均{'高估' if m > 0 else '低估'} "
                       f"{abs(m):.2f} 球（效应量 {effect:+.2f}，n={len(diffs)}）"),
        })
    return out


def d_prob_not_summing(ledger: list[dict]) -> list[dict]:
    """DIAG-PROB-NOT-SUMMING：概率不归一或含非法值。

    纯数据健全性检查。这类 bug 一旦出现，后面所有 Brier/RPS 都是垃圾。
    """
    out = []
    bad = []
    for r in ledger:
        for fld in ("final_prob", "model_raw", "market_fair"):
            p = r.get(fld)
            if p is None:
                continue
            if len(p) < 2:
                continue
            s = sum(p)
            # 两类异常分开处理：
            #   (1) 负值/单值>1 —— 真非法，无条件告警
            #   (2) 和偏离 1 超过 0.002 —— 不是浮点舍入能解释的
            # 0.9999 这类是 round(x,4) 落盘的舍入误差，不告警（否则满屏噪声）
            if (any(not (0 <= x <= 1) for x in p)
                    or (len(p) >= 3 and abs(s - 1.0) > 0.002)):
                bad.append((r.get("match_id", "?"), fld, [round(x, 5) for x in p],
                            round(s, 5)))
                break
    if bad:
        out.append({
            "id": "DIAG-PROB-NOT-SUMMING",
            "severity": "blocking",
            "n": len(bad),
            "examples": bad[:5],
            "detail": (f"{len(bad)} 场概率非法（负值/>1/和不为1）—— "
                       "该时段所有评估指标不可信"),
        })
    return out


def d_market_anchor_missing(ledger: list[dict],
                            pred_inputs: dict | None = None) -> list[dict]:
    """DIAG-NO-MARKET-ANCHOR：融合完全没用到市场。

    若某场 market_fair 为空，融合走 fuse_model_only，输出 100% 来自
    裸模型。这类场次的市场信息是**静默丢失**的 —— 没有 trace 警示，
    页面也看不出来。
    """
    out = []
    no_mkt = [r for r in ledger
              if r.get("final_prob") and not r.get("market_fair")]
    if no_mkt and len(no_mkt) >= 3:
        syn = 0
        if pred_inputs:
            for r in no_mkt:
                p = pred_inputs.get(r.get("match_id"))
                if p and p.get("odds_synthetic"):
                    syn += 1
        out.append({
            "id": "DIAG-NO-MARKET-ANCHOR",
            "severity": "high",
            "n": len(no_mkt),
            "total": len(ledger),
            "rate": round(len(no_mkt) / max(1, len(ledger)), 4),
            "synthetic": syn,
            "detail": (f"{len(no_mkt)}/{len(ledger)} 场无市场锚"
                       f"（其中 {syn} 场为合成赔率）—— "
                       "这些场次概率 100% 来自裸模型"),
        })
    return out


def d_pnl_negative(ledger: list[dict], min_n: int = 20) -> list[dict]:
    """DIAG-PNL-NEGATIVE：实盘持续亏损。

    账本里的 pnl 是系统真实记账的结果。分层看 edge 档位：
    若没有任何 edge 档位为正，说明系统在负 EV 空间里精确。
    """
    out = []
    pnl_rows = [r for r in ledger if r.get("pnl") not in (None, 0)]
    if len(pnl_rows) < min_n:
        return out
    total = sum(r["pnl"] for r in pnl_rows)
    hits = sum(1 for r in pnl_rows if r.get("hit"))
    if total < 0 and hits / len(pnl_rows) < 0.45:
        out.append({
            "id": "DIAG-PNL-NEGATIVE",
            "severity": "blocking",
            "n": len(pnl_rows),
            "pnl": round(total, 2),
            "hit": round(hits / len(pnl_rows), 4),
            "detail": (f"{len(pnl_rows)} 注已结算，pnl 合计 {total:.2f}，"
                       f"命中率 {hits/len(pnl_rows)*100:.1f}% —— "
                       "系统在负 EV 空间里做精确估计"),
        })
    return out


def d_entropy_collapse(ledger: list[dict], min_n: int = 30) -> list[dict]:
    """DIAG-ENTROPY-COLLAPSE：输出分布过度集中。

    熵低于理论上限太多 = 系统对几乎所有比赛都给出同样的高置信判断。
    这会直接毁掉 Brier：即使 argmax 命中率还行，概率质量也会崩。
    """
    out = []
    ents = [_entropy(r["final_prob"]) for r in ledger
            if r.get("final_prob") and len(r["final_prob"]) >= 3]
    if len(ents) < min_n:
        return out
    m = statistics.mean(ents)
    if m > 1.45:      # 均匀分布熵 = log2(3) = 1.585
        return out
    out.append({
        "id": "DIAG-ENTROPY-COLLAPSE",
        "severity": "medium",
        "n": len(ents),
        "mean_entropy": round(m, 4),
        "uniform_entropy": 1.585,
        "detail": (f"输出平均熵 {m:.3f} bits（均匀分布 1.585）—— "
                   "分布过度集中，概率质量可能失真"),
    })
    return out


def json_round(v, nd=4):
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, (list, tuple)):
        return tuple(json_round(x, nd) for x in v)
    return v


# --------------------------------------------------------------- 汇总

ALL_DIAGNOSTICS = (
    d_output_constant,
    d_worse_than_market,
    d_calibration_inversion,
    d_dead_feature,
    d_xg_drift,
    d_prob_not_summing,
    d_market_anchor_missing,
    d_pnl_negative,
    d_entropy_collapse,
)


def run_all(ledger: list[dict], pred_inputs: dict | None = None) -> dict:
    """跑全部诊断器，返回 {诊断器名: [findings]}。

    只对账本字段做诊断的部分不需要 pred_inputs；需要区分「合成赔率」
    的诊断器会自动降级。
    """
    # 显式声明哪些诊断器需要第二个参数 —— 不能用 co_argcount 判断，
    # 因为带默认值的参数同样计入，会把 pred_inputs 传给不需要它的诊断器。
    needs_pred = {d_market_anchor_missing, d_dead_feature}
    results: dict[str, list] = {}
    for fn in ALL_DIAGNOSTICS:
        try:
            findings = (fn(ledger, pred_inputs) if fn in needs_pred else fn(ledger))
        except Exception as ex:  # noqa: BLE001
            # 诊断器自身出错不能拖垮整轮 —— 但必须显式暴露，
            # 否则「没发现问题」会被误读成「系统健康」
            results[f"{fn.__name__}:ERROR"] = [{
                "id": f"{fn.__name__}:ERROR",
                "severity": "medium",
                "detail": f"诊断器执行失败：{type(ex).__name__}: {ex}",
            }]
            continue
        if findings:
            results.setdefault(fn.__name__.lstrip("d_"), []).extend(findings)
    return results


def summary(results: dict) -> dict:
    """把诊断结果压成可比较的计数，供 PR 头部展示。"""
    by_sev = collections.Counter()
    total = 0
    for findings in results.values():
        for f in findings:
            by_sev[f.get("severity", "?")] += 1
            total += 1
    return {
        "total_findings": total,
        "by_severity": dict(by_sev),
        "diagnostic_kinds": len([k for k in results if not k.endswith(":ERROR")]),
        "errors": [k for k in results if k.endswith(":ERROR")],
    }
