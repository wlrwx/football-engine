#!/usr/bin/env python3
"""E2 实验：市场自身的跨盘口不一致是否可提取？（2026-10-11 新增）

## 与 E0/E1 的区别（这才是它还值得做的原因）

E0 用**球队实力模型**和市场比 → 显著更差（t=+34.74）
E1 用**市场自身的修正**和市场收盘比 → 无差异（t=−0.80）

两者都失败，但**失败的原因不同**，而 E2 不依赖任何外部模型：

    E0 失败 = 我们对赛果的估计不如市场
    E1 失败 = 市场的修正已被市场自己的收盘价吸收

E2 问的是第三个问题：**市场把 1X2、亚盘、大小球分别定价，
这三份定价彼此矛盾吗？矛盾的部分能不能下注？**

这是纯市场对市场，不需要任何球队实力假设 ——
1X2 的隐含进球分布只是把 1X2 价格翻译成进球的代数映射，不是预测模型。

## 方法

1. 从收盘 1X2（AvgCH/CD/CA）去水得 (p_h, p_d, p_a)
2. 查表反解 (λ_h, λ_a)：使独立泊松 1X2 概率最接近 (p_h, p_d, p_a)
   —— 这只是代数量换，不是模型
3. 用 (λ_h, λ_a) 给亚盘和大小球定价
4. 与市场实际报价（AvgCAHH/AvgCAHA、AvgC>2.5/AvgC<2.5）比较
5. **信号 = 1X2 蕴含的概率 − 该盘口的实际报价**
   信号 > 0 = 那个盘口相对市场自己的 1X2 定价被低估
6. 按信号分桶，按实际赔率下注，测 ROI

## 判据

信号若有效，应满足：高分桶 ROI 明显高于盲买成本（约 −6%）且 t 统计显著。
无效则同样得到"多空各扫一遍都是抽水"的结论。

用法：
    python -m engine.backtest.cross_market --limit 20000
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from engine.backtest.historical_replay import (  # noqa: E402
    load_football_data, devig,
)

OUT_JSON = ROOT / "data" / "state" / "cross_market.json"
MAX_GOALS = 12
_EPS = 1e-9

# ---- 泊松 pmf 查表（lambda 粒度 0.02，覆盖 0.05~4.5）--------------------
_LAM = np.arange(0.05, 4.52, 0.02)
_K = np.arange(MAX_GOALS + 1)
_LOG_FACT = np.array([math.lgamma(k + 1) for k in range(MAX_GOALS + 1)])
# 截断到 k<=MAX_GOALS 的 pmf 天然和小于 1（λ=4.5 时尾部约 5.5e-4）。
# 不归一化的话 j=outer(ph,pa) 的总和 < 1，会给 resid 引入恒定偏差，
# 让 tol=1e-6 永远达不到、白白跑满 5 个起点。归一化后总和恰为 1。
_RAW = np.exp(-_LAM[:, None] + _K[None, :] * np.log(_LAM)[:, None] - _LOG_FACT[None, :])
_POISSON = _RAW / _RAW.sum(axis=1, keepdims=True)     # [n_lam, n_goals]

# 比分网格的布尔掩码，预先算好避免在优化器内反复重建
_H = _K[:, None]
_A = _K[None, :]
_DIFF = _H - _A
_TOT = _H + _A
_HGT_A = _DIFF > 0        # 主胜
_HLT_A = _DIFF < 0        # 客胜
_DIAG = _DIFF == 0        # 平局


def _pois_at(lam: float) -> np.ndarray:
    """按 lambda 线性插值出比分 pmf（已归一化，和为 1）。"""
    if lam <= _LAM[0]:
        return _POISSON[0]
    if lam >= _LAM[-1]:
        return _POISSON[-1]
    i = int((lam - _LAM[0]) / 0.02)
    w = (lam - _LAM[0]) / 0.02 - i
    return (1 - w) * _POISSON[i] + w * _POISSON[min(i + 1, len(_POISSON) - 1)]


def _joint(lam_h: float, lam_a: float) -> np.ndarray:
    return np.outer(_pois_at(lam_h), _pois_at(lam_a))


# ---------------------------------------------------------------- 反解

def invert_1x2(p_h: float, p_d: float, p_a: float, tol: float = 1e-6):
    """反解 (λ_h, λ_a)，使独立泊松 1X2 匹配给定概率。

    纯代数换算，不涉及任何球队数据 —— 这正是 E2 区别于 E0 的地方。

    实现历经三次修正，都是"看起来能跑但结果错"的类型：
      1. 乘性不动点迭代：平局率偏低时两个 λ 一起塌到下界 0.05。
      2. L-BFGS + 手写解析梯度：梯度里的 I1 写成 (ph-pm).sum()，
         而归一化 pmf 的和恒为 1 → 差分为 0 → 梯度全零 → 优化器原地不动。
      3. resid 返回三维向量，而 scipy 要求标量 → L-BFGS 每次调用抛
         ValueError 被 except 吞掉，全部场次静默回退到初值 1.3/1.3。
         网格真值 (2.7, 0.8) 差了 5 倍，输出却"看起来正常"。
    现在用无梯度 L-BFGS（2 个参数，每次迭代多 3 次函数求值，成本可忽略）。
    """
    from scipy.optimize import minimize

    def resid(theta):
        j = _joint(float(np.exp(theta[0])), float(np.exp(theta[1])))
        # j[h,a] = P(H=h)P(A=a)，对任一轴求边际都恒等于 1。
        # P(home) 必须对 h>a 的三角区域求和。
        return np.array([j[_HGT_A].sum() - p_h,
                         j[_DIAG].sum() - p_d,
                         j[_HLT_A].sum() - p_a])

    def sse(theta):
        r = resid(theta)
        return float(np.dot(r, r))

    best, best_err = None, float("inf")
    for s0 in (0.6, 0.9, 1.3, 1.9, 2.6):
        try:
            r = minimize(sse, np.log([s0, s0]), method="L-BFGS-B",
                         bounds=[(np.log(0.05), np.log(4.5))] * 2,
                         options={"maxiter": 300, "ftol": 1e-18, "gtol": 1e-16})
        except Exception as e:      # 绝不静默：记下来
            print(f"    [invert] s0={s0} 优化失败: {type(e).__name__}: {e}")
            continue
        e = float(np.abs(resid(r.x)).max())
        if e < best_err:
            best_err, best = e, r.x
        if e < tol:
            break

    if best is None:
        raise RuntimeError(f"invert_1x2 全部起点优化失败 (p={p_h:.3f},{p_d:.3f},{p_a:.3f})")

    # 残差不达标也抛：本函数的价值全在反解精度上，悄悄返回近似值会毁掉整个实验
    if best_err > 0.02:
        raise RuntimeError(
            f"invert_1x2 残差过大 {best_err:.4f} (p={p_h:.3f},{p_d:.3f},{p_a:.3f}, "
            f"lam={np.exp(best[0]):.2f}/{np.exp(best[1]):.2f}) —— 独立泊松无法拟合该组概率")
    return float(np.exp(best[0])), float(np.exp(best[1]))


# ---------------------------------------------------------------- 盘口定价

def _side_unit(margin: float) -> float:
    """让球线下一侧的单位净收益：赢 +1 / 走 0 / 输 −1。

    margin = (h−a) + line 的取值集合是「整数」或「整数 + 0.5」，
    所以赢/走/输**只能按符号判**，不能按幅度判：半盘线（0.5）下
    margin = 0.5 表示主队赢（平局球赢半球），是赢不是走盘。

    ## 符号约定（33,060 场实测确认，勿改）

    football-data.co.uk 的 AHCh 是**加在主队进球上的让球数**：
    强主队为负（主队让球），弱主队为正（主队受让）。
    实测主胜赔率 <1.6 的强主队让球线均值 -1.473，>3.0 的弱主队 +0.680。
    所以 line 越负，主队越难打出。

    独立佐证：按本约定平注 ROI 为主队 -4.89% / 客队 -2.56%，
    与亚盘 overround 4.59% 吻合；若把 line 取反，ROI 变成 +15.88%
    —— 那种"稳定赚钱"只可能是符号搞错了。
    """
    if margin > _EPS:
        return 1.0
    if margin > -_EPS:
        return 0.0
    return -1.0


def _leg_win_prob(lam_h: float, lam_a: float, line: float) -> tuple[float, float]:
    """单条整线/半球线下主队（赢+走盘）的概率。"""
    j = _joint(lam_h, lam_a)
    margin = _DIFF + line
    return float(j[margin > _EPS].sum()), float(j[margin > -_EPS].sum())


def ah_probs(lam_h: float, lam_a: float, line: float) -> tuple[float, float, float]:
    """亚盘（line 加在主队进球上，负=主队让球）**亚式**结算下的主/平/客概率。

    2026-10-11 修正 1：初版把 -1.75 这类四球线当欧式三路处理。
    四球/半球线（x.25 / x.75）必须拆成相邻两条整线各半注，结算取两段结果的平均。
    错用欧式口径会让信号出现 ~16 个百分点的系统性偏差（实测信号均值 -0.1634），
    看起来像市场无效率，实际是口径错误。

    ## 返回值的定义（重要，弄错就等于把口径误差当成市场无效率）

    p_home / p_draw / p_away 是**注资份额**而非比分结果概率：
    四球线下一半注押在 lo、一半押在 hi，所以

        p_home = 0.5*P(win_lo) + 0.5*P(win_hi)
        p_draw = 0.5*P(push_lo) + 0.5*P(push_hi)

    三者和为 1，且恒等式 E[u] = p_home − p_away 成立（见 verify 脚本）。
    注意 p_draw ≠ "整注走盘的概率"——后者对四球线恒为 0（两段是相邻的
    两条不同线，净胜球不可能同时等于 -lo 和 -hi），但押回的注资份额非零。
    2026-10-11 曾把 p_draw 一度改成 0，正是这个区分没搞清楚。
    """
    quarter = abs(line * 4) % 2 == 1          # 1.75 → 7 % 2 = 1 → 是
    if not quarter:
        p_home, p_home_or_push = _leg_win_prob(lam_h, lam_a, line)
        p_draw = p_home_or_push - p_home
        assert p_draw >= -_EPS, f"走盘份额为负 {p_draw}"
        return p_home, max(0.0, p_draw), max(0.0, 1.0 - p_home - p_draw)
    # 四球线：line-0.25 与 line+0.25 各半注
    lo, hi = line - 0.25, line + 0.25
    w_lo, hp_lo = _leg_win_prob(lam_h, lam_a, lo)
    w_hi, hp_hi = _leg_win_prob(lam_h, lam_a, hi)
    p_home = 0.5 * (w_lo + w_hi)
    p_draw = 0.5 * ((hp_lo - w_lo) + (hp_hi - w_hi))
    assert p_draw >= -_EPS, f"走盘份额为负 {p_draw}"
    return p_home, max(0.0, p_draw), max(0.0, 1.0 - p_home - p_draw)


def ou_probs(lam_h: float, lam_a: float, line: float = 2.5) -> tuple[float, float]:
    """大小球 over/under 概率。line 取 2.5 时无走盘。"""
    j = _joint(lam_h, lam_a)
    p_over = float(j[_TOT > line - _EPS].sum())
    return p_over, 1.0 - p_over


# ---------------------------------------------------------------- 公平赔率

def ah_fair_odds(lam_h: float, lam_a: float, line: float) -> tuple[float, float]:
    """给定 (λ_h, λ_a, line)，亚盘两侧的**公平赔率**（含走盘/半赢半输）。

    ## 为什么不能直接比"去水后的概率"

    亚盘的赔付不是二值的：赢了拿 O、走盘退本、输了没。对 1 单位本金，
    公平赔率满足

        w·O + p = 1        w = 赢的注资份额，p = 走盘的注资份额
        ⟹ O_fair = (1 − p) / w

    而 devig2 把两路赔率归一化得到的那个"概率"，对有走盘的盘口并不等于
    赢盘概率。实测 line=−1.00：两路去水后恒为 0.5005（中位 0.5000），
    但实际赢注资份额只有 0.3795（走盘率 26.9%）—— 差的正是走盘那一块。

    初版 E2 用去水概率做差，AH 信号均值因此出现 −0.0679 的系统性偏差，
    看起来像"市场大幅高估主队"，实际是自己的口径问题。
    改用赔率比之后，均值应当回到 0 附近；这是本次修正的自检指标。

    大小球 2.5 线无走盘，O_fair = 1/p，与概率口径等价。
    """
    p_home, p_draw, p_away = ah_probs(lam_h, lam_a, line)
    if p_home <= 1e-6 or p_away <= 1e-6:
        raise RuntimeError(f"亚盘公平赔率退化 line={line} p=({p_home:.4f},{p_draw:.4f},{p_away:.4f})")
    return (1.0 - p_draw) / p_home, (1.0 - p_draw) / p_away


def ou_fair_odds(lam_h: float, lam_a: float, line: float = 2.5) -> tuple[float, float]:
    """大小球两侧的公平赔率。2.5 线无走盘，等价于 1/p。"""
    p_over, p_under = ou_probs(lam_h, lam_a, line)
    if p_over <= 1e-6 or p_under <= 1e-6:
        raise RuntimeError(f"大小球公平赔率退化 line={line}")
    return 1.0 / p_over, 1.0 / p_under


def ah_units(goal_diff: int, line: float, quarter: bool | None = None) -> float:
    """给定实际净胜球差 goal_diff（= h−a），主队一侧的单位净收益。

    取值 ∈ {−1, −0.5, 0, 0.5, 1}：全输 / 半输 / 走盘 / 半赢 / 全赢。
    客队一侧恒为 -u_home。

    ## 这里曾经把 goal_diff 当成"已加过让球线的 margin"而完全忽略了 line，
    导致同一 λ 下所有让球线的期望收益完全相同（λ=1/1 时恒为 0）。
    定价(ah_probs)和结算(ah_units)口径不一致时，实验测到的是口径误差。

    校验方式：把本函数在同一个比分 pmf 上求期望，必须等于
    ah_probs 返回的 p_home − p_away。verify 脚本逐格检查 96 组组合。
    """
    if quarter is None:
        quarter = abs(line * 4) % 2 == 1
    margin = goal_diff + line
    if quarter:
        return 0.5 * (_side_unit(margin + 0.25) + _side_unit(margin - 0.25))
    return _side_unit(margin)


# ---------------------------------------------------------------- 主流程

def run(matches, limit: int | None = None, verbose: bool = True) -> dict:
    rows = [m for m in matches
            if m.close and m.open_ and m.ah_line is not None and m.ou_line is not None]
    if limit:
        rows = rows[-limit:]

    out_ah, out_ou = [], []
    skipped = defaultdict(int)
    for m in rows:
        try:
            p = np.asarray(devig(m.close, "proportional"))
            lam_h, lam_a = invert_1x2(float(p[0]), float(p[1]), float(p[2]))
        except RuntimeError:
            skipped["invert"] += 1
            continue
        except Exception as e:
            skipped[type(e).__name__] += 1
            if verbose:
                print(f"    [skip] {type(e).__name__}: {e}")
            continue
        out_ah.append((lam_h, lam_a, m))
        out_ou.append((lam_h, lam_a, m))

    if verbose and skipped:
        print(f"  ⚠ 跳过 {sum(skipped.values())}/{len(rows)} 场: {dict(skipped)}")

    def scan(entries, kind: str) -> dict:
        # 收集 (signal, odds, 单位净收益)；signal = log(市场赔率 / 模型公平赔率)
        sigs: list[tuple[float, float, float, object]] = []
        for lam_h, lam_a, m in entries:
            if kind == "ah":
                if m.ah_home_odds is None or m.ah_away_odds is None or m.ah_line is None:
                    continue
                try:
                    f_h, f_a = ah_fair_odds(lam_h, lam_a, m.ah_line)
                except RuntimeError:
                    skipped["ah_fair_odds"] += 1
                    continue
                u_home = ah_units(m.hg - m.ag, m.ah_line)
                group = m.ah_line
                picks = [(m.ah_home_odds, f_h, u_home), (m.ah_away_odds, f_a, -u_home)]
            else:
                if m.ou_over_odds is None or m.ou_under_odds is None:
                    continue
                f_o, f_u = ou_fair_odds(lam_h, lam_a, 2.5)
                over_win = 1.0 if (m.hg + m.ag) > 2.5 else -1.0
                group = 2.5
                picks = [(m.ou_over_odds, f_o, over_win), (m.ou_under_odds, f_u, -over_win)]

            for odds, fair, u in picks:
                if odds is None or odds <= 1.0 or fair <= 1.0:
                    continue
                # 信号 = log(市场赔率 / 模型公平赔率)。>0 = 市场给得比公平价好 = 低估。
                sig = math.log(odds / fair)
                # 赢 → 净赚 (odds−1)；半赢 → 0.5×(odds−1)；走盘 → 0；输 → −1。
                # 写成 odds*u 会把本金也算成利润，ROI 直接虚高一倍。
                pnl_u = u * (odds - 1.0) if u > 0 else u
                sigs.append((sig, odds, pnl_u, group))

        res: dict = {"n": len(sigs)}
        if not sigs:
            return res
        sig_arr = np.array([s for s, _, _, _ in sigs])
        pnl_arr = np.array([u for _, _, u, _ in sigs])
        res["signal_distribution"] = {
            "mean": round(float(sig_arr.mean()), 4),
            "std": round(float(sig_arr.std()), 4),
            "p05": round(float(np.percentile(sig_arr, 5)), 4),
            "p95": round(float(np.percentile(sig_arr, 95)), 4),
        }

        # ----------------------------------------------------------------
        # 组内去均值：信号含一个巨大的、与"市场无效率"无关的常数偏差
        #
        # 实测（8,000 场）：独立泊松反解出的 λ_h+λ_a 均值只有 2.420，
        # 而真实总进球均值 2.672 —— 两参数族拟合 1X2 之后无法还原真实的
        # 进球联合分布，导致模型 P(over2.5)=0.4309 而市场 0.5105、实际 0.5050。
        # 这个 −0.18 的 log 差是**模型设定误差**，不是市场矛盾。
        # 直接按原始信号分桶，会把这份设定误差误读成"市场大幅错价"。
        #
        # 亚盘的偏差还随让球线变化（走盘率不同），所以按让球线分组去均值。
        # ----------------------------------------------------------------
        groups: dict = defaultdict(list)
        for s, _, _, g in sigs:
            groups[g].append(s)
        gmean = {g: float(np.mean(v)) for g, v in groups.items()}
        res["signal_demeaned_by_group"] = {
            "n_groups": len(groups),
            "group_means": {str(k): round(v, 4) for k, v in sorted(gmean.items(), key=lambda kv: str(kv[0]))},
        }
        sigs_dm = [(s - gmean[g], o, u) for s, o, u, g in sigs]
        sig_dm_arr = np.array([s for s, _, _ in sigs_dm])
        res["signal_demeaned"] = {
            "mean": round(float(sig_dm_arr.mean()), 8),
            "std": round(float(sig_dm_arr.std()), 4),
        }

        # ----------------------------------------------------------------
        # 回归：实际单位收益对去均值信号的斜率。
        # 斜率显著 ≠ 0 才是真正可提取的不一致；单纯看 |信号| 大小的 ROI
        # 会被上面那份常数偏差完全污染。
        # ----------------------------------------------------------------
        if len(sigs_dm) > 2 and float(sig_dm_arr.std()) > 0:
            x = sig_dm_arr
            y = pnl_arr
            sxx = float(((x - x.mean()) ** 2).sum())
            bxy = float(((x - x.mean()) * (y - y.mean())).sum())
            slope = bxy / sxx
            intercept = float(y.mean()) - slope * float(x.mean())
            resid_v = y - (intercept + slope * x)
            df = len(x) - 2
            se_slope = math.sqrt(float((resid_v ** 2).sum()) / df / sxx) if df > 0 else 0.0
            t_slope = slope / se_slope if se_slope > 0 else 0.0
            ss_tot = float(((y - y.mean()) ** 2).sum())
            r2 = 1.0 - float((resid_v ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
            res["regression"] = {
                "slope": round(slope, 4), "t": round(t_slope, 2), "r2": round(r2, 5),
                "intercept": round(intercept, 4), "n": len(x),
                "note": "单位收益 = a + b·去均值信号。b 的 t 显著才是真信号。",
            }
        else:
            res["regression"] = None

        scan_rows = []
        for label, pool in (("raw", [(s, o, u) for s, o, u, _ in sigs]),
                            ("demeaned", sigs_dm)):
            for th in (0.0, 0.01, 0.02, 0.03, 0.05, 0.08):
                for direction in ("both", "underpriced", "overpriced"):
                    sel = [(o, u) for s, o, u in pool
                           if abs(s) >= th - _EPS and (
                               direction == "both"
                               or (direction == "underpriced" and s > _EPS)
                               or (direction == "overpriced" and s < -_EPS))]
                    if not sel:
                        continue
                    us = np.array([u for _, u in sel])
                    n = len(us)
                    roi = float(us.mean())
                    sd = float(us.std(ddof=1)) if n > 1 else 0.0
                    t = roi / (sd / math.sqrt(n)) if sd > 0 else 0.0
                    scan_rows.append({
                        "basis": label, "threshold": th, "direction": direction,
                        "n_bets": n, "roi": round(roi, 4), "t": round(t, 2),
                        "pnl_units": round(float(us.sum()), 1),
                        "hit_rate": round(float((us > _EPS).mean()), 4),
                        "flat_rate": round(float((np.abs(us) <= _EPS).mean()), 4),
                    })
        res["scan"] = scan_rows

        # 十分位表：比"按阈值分桶"更能暴露非线性关系。
        # 分桶 ROI 受常数偏差污染，十分位至少能看出收益对信号到底单调不单调。
        deciles = []
        for label, pool in (("raw", [(s, o, u) for s, o, u, _ in sigs]),
                            ("demeaned", sigs_dm)):
            if not pool:
                continue
            xs = np.array([s for s, _, _ in pool])
            ys = np.array([u for _, _, u in pool])
            qs = np.percentile(xs, np.arange(0, 101, 10))
            for i in range(10):
                lo, hi = qs[i], qs[i + 1]
                m = (xs >= lo) & (xs <= hi) if i == 9 else (xs >= lo) & (xs < hi)
                if not m.any():
                    continue
                us = ys[m]
                sd = float(us.std(ddof=1)) if m.sum() > 1 else 0.0
                deciles.append({
                    "basis": label, "decile": i + 1,
                    "sig_lo": round(float(lo), 4), "sig_hi": round(float(hi), 4),
                    "n": int(m.sum()), "roi": round(float(us.mean()), 4),
                    "t": round(float(us.mean()) / (sd / math.sqrt(len(us))), 2)
                    if sd > 0 else 0.0,
                    "hit_rate": round(float((us > _EPS).mean()), 4),
                })
        res["deciles"] = deciles

        # ----------------------------------------------------------------
        # 稳健性检查：单一 OLS 斜率会被极端值和非单调关系骗到。
        #
        # 实测反面案例（66,116 注亚盘）：OLS 斜率 t=+2.97 看着显著，
        # 但十分位 ROI 是 −5.82/−5.28/−3.77/−2.77/−1.44/−4.98/−2.82/
        # −3.64/−4.94/−1.79，既非单调也不为正。斜率几乎全由 D1 那段
        # 信号低到 −2.09 的离群尾巴撑着。
        # 所以判定必须同时满足三条：斜率显著 + 十分位单调 + 最高十分位显著为正。
        # ----------------------------------------------------------------
        def _rank(v: np.ndarray) -> np.ndarray:
            order = np.argsort(v)
            r = np.empty(len(v), dtype=float)
            r[order] = np.arange(len(v), dtype=float)
            return r

        res["robustness"] = {}
        for basis in ("raw", "demeaned"):
            dm = [d for d in deciles if d["basis"] == basis]
            if len(dm) < 5:
                continue
            rois = np.array([d["roi"] for d in dm])
            rho = float(np.corrcoef(_rank(rois), _rank(np.arange(len(rois), dtype=float)))[0, 1])
            top = dm[-1]
            res["robustness"][basis] = {
                "spearman_decile_roi": round(rho, 3),
                "monotone": bool(rho > 0.7),
                "top_decile_roi": top["roi"],
                "top_decile_t": top["t"],
                "top_decile_n": top["n"],
                "all_deciles_negative": bool((rois < 0).all()),
            }
        return res

    ah = scan(out_ah, "ah")
    ou = scan(out_ou, "ou")
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_rows_in": len(rows),
        "n_matches_ah": ah.get("n", 0),
        "n_matches_ou": ou.get("n", 0),
        "skipped": dict(skipped),
        "note": ("信号 = log(市场赔率 / 模型公平赔率)，其中模型公平赔率由 1X2 反解出的"
                 "独立泊松 (λ_h, λ_a) 定价。结算按亚式（含半赢/半输/走盘），"
                 "ROI 为每注单位净收益。"
                 "关键：信号含一个与市场无效率无关的常数偏差（独立泊松拟合 1X2 后"
                 "总进球被系统性低估约 9%），所以只有去均值口径与回归斜率才可判定。"),
        "asian_handicap": ah,
        "over_under": ou,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    print("=" * 72)
    print("E2：市场跨盘口不一致性实验")
    print("=" * 72)

    ms = load_football_data()
    usable = [m for m in ms if m.close and m.open_ and m.ah_line is not None and m.ou_line is not None]
    print(f"  可用样本: {len(usable)} 场")

    res = run(usable, limit=args.limit)

    verdicts = []
    for name in ("asian_handicap", "over_under"):
        r = res[name]
        print(f"\n--- {name}  (n={r.get('n', 0)}) ---")
        sd = r.get("signal_distribution")
        if sd:
            print(f"  原始信号: 均值 {sd['mean']:+.4f}  标准差 {sd['std']:.4f}  "
                  f"P5 {sd['p05']:+.4f}  P95 {sd['p95']:+.4f}")
        sdm = r.get("signal_demeaned")
        if sdm:
            print(f"  组内去均值后: 均值 {sdm['mean']:+.8f}  标准差 {sdm['std']:.4f}")
        reg = r.get("regression")
        if reg:
            print(f"  回归 单位收益 ~ 去均值信号: 斜率 {reg['slope']:+.4f}  t={reg['t']:.2f}  "
                  f"R²={reg['r2']:.5f}  (n={reg['n']})")
            verdicts.append((name, reg))
        rows = r.get("scan") or []
        if not rows:
            print("  无可扫描数据")
            continue
        for basis in ("raw", "demeaned"):
            sub = [x for x in rows if x["basis"] == basis]
            if not sub:
                continue
            print(f"  [{basis}] {'阈值':>6}{'方向':>13}{'注数':>8}{'胜率':>8}{'走盘':>8}{'ROI':>9}{'t':>8}{'PnL':>11}")
            for x in sub:
                print(f"  {'':9}{x['threshold']:>6}{x['direction']:>13}{x['n_bets']:>8}"
                      f"{x['hit_rate']*100:>7.1f}%{x.get('flat_rate',0)*100:>7.1f}%"
                      f"{x['roi']*100:>8.2f}%{x['t']:>8.2f}{x['pnl_units']:>11.1f}")

    # 判定以**去均值回归斜率的 t 值**为准：原始信号的 ROI 被常数偏差污染，
    # 而常数偏差恰恰来自"独立泊松拟合 1X2 会低估总进球"这一模型设定误差。
    # 判定必须三条同时成立，单看 OLS 斜率会被离群尾巴骗到（见 scan 里 robustness）
    reasons = []
    for name, r in verdicts:
        rb = (res[name].get("robustness") or {}).get("demeaned") or {}
        rho = rb.get("spearman_decile_roi")
        mono = rb.get("monotone")
        top_roi = rb.get("top_decile_roi")
        top_t = rb.get("top_decile_t")
        ok = abs(r["t"]) > 2 and bool(mono) and (top_roi or 0) > 0 and (top_t or 0) > 2
        print(f"    {name}: slope t={r['t']:+.2f} | 十分位 Spearman={rho} 单调={mono} | "
              f"最高十分位 ROI={top_roi} t={top_t} → {'通过' if ok else '不通过'}")
        if not ok:
            why = []
            if abs(r["t"]) <= 2:
                why.append(f"斜率不显著(t={r['t']:.2f})")
            if not mono:
                why.append(f"十分位非单调(Spearman={rho})")
            if (top_roi or 0) <= 0:
                why.append(f"最高十分位 ROI={top_roi} 未为正")
            elif (top_t or 0) <= 2:
                why.append(f"最高十分位 ROI={top_roi} 但 t={top_t} 不显著")
            reasons.append(f"{name} " + "、".join(why))
    res["regression_verdict"] = {name: r for name, r in verdicts}
    res["verdict"] = ("E2 未通过：" + "；".join(reasons)) if reasons else \
        ("E2 有信号：去均值后信号仍能解释实际收益，且十分位单调、最高分位显著为正")
    print("\n  判定依据 = 去均值回归斜率的 t 值（|t|>2 才算信号）")
    for name, r in verdicts:
        print(f"    {name}: slope={r['slope']:+.4f}  t={r['t']:+.2f}  R²={r['r2']:.5f}")

    print("\n  去均值信号十分位（收益对信号是否单调）:")
    for name in ("asian_handicap", "over_under"):
        dec = [d for d in (res[name].get("deciles") or []) if d["basis"] == "demeaned"]
        if not dec:
            continue
        print(f"    [{name}]")
        print(f"      {'D':>3}{'信号区间':>22}{'注数':>8}{'胜率':>8}{'ROI':>9}{'t':>8}")
        for d in dec:
            print(f"      {d['decile']:>3}[{d['sig_lo']:+.4f},{d['sig_hi']:+.4f}){d['n']:>8}"
                  f"{d['hit_rate']*100:>7.1f}%{d['roi']*100:>8.2f}%{d['t']:>8.2f}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 已写入 {OUT_JSON.relative_to(ROOT)}")
    print(f"  判定: {res['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
