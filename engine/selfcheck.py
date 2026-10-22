#!/usr/bin/env python3
"""数据完整性自检（2026-08-13 新增，防静默数据断裂重演）

背景：波胆/总进球/半全场赔率 207 场 0% 抓取率持续三周无人发现（字段名
不匹配导致数据流静默死亡），直到用户质疑才挖出。本脚本在每次预测/结算
后运行，检查关键数据完整性，异常输出 ⚠ 并写 selfcheck 报告。

检查项：
1. 盘口字段完整性：crs_odds / ttg_odds / hafu_odds / handicap
2. 数据源覆盖率：sina_odds / DJYY 增强 / market_fair
3. 串关选腿赔率分布：1.5 以下大热占比（价值选腿纪律）
4. 比分串：是否用了官方赔率（模拟赔率 = 数据断裂征兆）
5. 概率分布健康度：高置信段（≥70%）样本占比（融合稀释检测）
6. DJYY 通道存活率：djyy_enriched 近 3 天中 2 天 <50% 报警（防第三通道静默死亡重演）

用法：python3 engine/selfcheck.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# 2026-08-16 修复：脚本方式运行（python engine/selfcheck.py）时 sys.path[0]
# 是 engine/ 目录，import engine 包失败（Actions 里 ModuleNotFoundError）。
# 加仓库根引导，脚本/模块两种方式都能跑。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.beijing_time import beijing_today
STATE = ROOT / "data" / "state"
DAILY = ROOT / "data" / "daily"

# 阈值：低于此覆盖率报警（部分场次缺失可接受，全缺=数据流断裂）
FIELD_THRESHOLD = {
    "crs_odds": 0.80,       # 波胆赔率（串关比分串生命线）
    "ttg_odds": 0.80,       # 总进球
    "hafu_odds": 0.80,      # 半全场
    "handicap": 0.80,       # 让球
    "sina_odds": 0.60,      # 新浪（历史 65%，单日抖动可容忍）
    "market_fair": 0.80,    # 市场公平概率
}
LOW_ODDS_CAP = 0.30        # 串关腿 1.5 以下占比上限（价值选腿纪律）


def _djyy_alive(pred: dict) -> bool:
    """第三通道是否真的供上了概率。

    2026-10-22：CI 实时源被 Cloudflare 拦、快照补位 10/13，而本检查只看
    djyy_enriched（实时富化标志，5/13）→ 在通道已恢复时仍报「静默死亡」。
    存活口径必须是「预测里有 djyy_model_prob」（实时或快照），不是「实时源通」。
    """
    return bool(pred.get("djyy_model_prob") or pred.get("djyy_enriched"))


def check_day(day_str: str) -> list[str]:
    """检查单日 predictions.json，返回告警列表"""
    alerts: list[str] = []
    pf = DAILY / day_str / "predictions.json"
    if not pf.exists():
        return [f"⚠ {day_str}: predictions.json 不存在"]
    try:
        preds = json.loads(pf.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"⚠ {day_str}: predictions.json 解析失败: {e}"]
    if isinstance(preds, dict):
        preds = preds.get("predictions", preds)
    if not preds:
        return [f"⚠ {day_str}: predictions 为空"]

    n = len(preds)
    # 1. 盘口字段完整性
    for fld, thr in FIELD_THRESHOLD.items():
        cnt = sum(1 for p in preds if p.get(fld))
        ratio = cnt / n
        if ratio == 0:
            alerts.append(f"⚠ {day_str}: {fld} 全缺（0/{n}）——数据流可能断裂")
        elif ratio < thr:
            alerts.append(f"⚠ {day_str}: {fld} 覆盖率 {ratio:.0%} < {thr:.0%}（{cnt}/{n}）")

    # 2. 比分串是否用了官方赔率
    tp_path = DAILY / day_str / "ticket_plan.json"
    if tp_path.exists():
        try:
            tp = json.loads(tp_path.read_text(encoding="utf-8"))
            sp = tp.get("score_parlay") or []
            for t in sp:
                if t.get("odds_source") == "simulated":
                    alerts.append(f"⚠ {day_str}: 比分串用了模拟赔率（{t.get('type')}）——数据断裂征兆")
            # 3. 串关腿赔率分布（1.5 以下大热占比）
            parlay = tp.get("parlay") or []
            if parlay:
                legs = [l for t in parlay for l in (t.get("legs") or [])]
                if legs:
                    low = [l for l in legs if (l.get("odds") or 0) < 1.5]
                    ratio = len(low) / len(legs)
                    if ratio > LOW_ODDS_CAP:
                        alerts.append(
                            f"⚠ {day_str}: 串关腿 {ratio:.0%} 在 1.5 以下大热"
                            f"（{len(low)}/{len(legs)}）——价值选腿纪律被破坏"
                        )
        except Exception:
            pass

    # 4. 概率分布健康度（高置信段占比）
    # predictions.json 存的是 home_win_prob/draw_prob/away_win_prob 三路概率
    # （final_prob 是结算后账本才有的字段，预测当天不存在）。
    # 2026-08-13 市场主导权重后：市场去水概率天然保守（max 均值 ~52%，极少超
    # 70%），融合概率落在 35-55% 属正常分布。因此高置信告警改用 market_fair
    # 判定（市场源自身高置信缺失 = 数据问题），不再用融合后概率。
    def _mkt_max(p):
        mf = p.get("market_fair")
        if mf and isinstance(mf, (list, tuple)) and len(mf) == 3:
            return max(mf)
        return None

    mkt_maxs = [_mkt_max(p) for p in preds]
    mkt_maxs = [m for m in mkt_maxs if m is not None]
    if mkt_maxs and len(mkt_maxs) >= max(3, n // 2):
        hi_ratio = sum(1 for m in mkt_maxs if m >= 0.70) / len(mkt_maxs)
        if hi_ratio > 0.60:
            alerts.append(f"⚠ {day_str}: 市场高置信段占比 {hi_ratio:.0%}——市场概率可能过度自信")
    else:
        alerts.append(f"⚠ {day_str}: market_fair 覆盖率不足（{len(mkt_maxs)}/{n}）——市场源缺失")

    # 5. 合成赔率裸奔（2026-10-09）：当数据源缺真实赔率时会合成赔率，
    #    合成赔率是从 final 概率反推的（1/(final*1.05)），去水后≈final 本身，
    #    等于把模型自己的输出佯装成市场。此类场次 market_fair=None 时融合
    #    走 fuse_model_only，概率 100% 来自裸模型，无任何市场锚。
    #    单独列告警，不让它被笼统的「覆盖率不足」淹没。
    _synthetic_bare = [p for p in preds
                       if p.get("odds_synthetic") and not p.get("market_fair")]
    if _synthetic_bare:
        alerts.append(
            f"⚠ {day_str}: {len(_synthetic_bare)}/{n} 场合成赔率且 market_fair=None，"
            "完全绕过市场融合（裸模型概率）")

    # 6. 融合源存活与有效权重（2026-10-11）
    #    背景：DJYY 断供两个月期间，config 一直挂着 djyy_weight=0.20，
    #    融合层靠重新归一化兜住（实际贡献 0），日志/配置/账本三处都看不出
    #    异常——直到人工翻账本才发现 529 场 djyy_prob 全空。
    #    融合层现在逐场落盘 fusion_effective_weights / fusion_source，
    #    这里把它变成**自动告警**：名义权重非零但实际贡献 0 = 死源。
    _have_w = [p for p in preds if p.get("fusion_effective_weights")]
    if not _have_w:
        alerts.append(
            f"⚠ {day_str}: predictions 无 fusion_effective_weights 字段"
            "（旧版本引擎产出，或融合层未记录）——权重不可审计"
        )
        return alerts

    from collections import Counter
    srcs = Counter(p.get("fusion_source", "?") for p in _have_w)
    m = len(_have_w)

    # 名义值从 config 读，读不到就不告警（不猜）
    _nominal = {}
    try:
        _nominal = json.loads(
            (ROOT / "config" / "prediction.json").read_text(encoding="utf-8")
        ).get("fusion", {})
    except Exception:
        _nominal = {}

    # 6a. 死源：某源实际贡献恒为 0 但名义权重不为 0
    for _key, _label in (("djyy", "DJYY"), ("market", "市场"), ("model", "模型")):
        if not _nominal.get(f"{_key}_weight"):
            continue                     # 名义权重本来就是 0，不是死源
        _alive = sum(
            1 for p in _have_w
            if (p.get("fusion_effective_weights") or {}).get(_key, 0) > 0
        )
        if _alive == 0:
            alerts.append(
                f"⚠ {day_str}: {_label} 是死源——名义权重 "
                f"{_nominal[f'{_key}_weight']:.2f} 但实际贡献 0（{m} 场全部为 0）"
            )
        elif _alive < m * 0.5:
            alerts.append(
                f"⚠ {day_str}: {_label} 覆盖率偏低 {_alive}/{m}——该源大面积失效"
            )

    # 6b. 模型有效权重过低：融合名义上在做"模型+市场"，实际模型占比却
    #     小到几乎等于纯市场。这不是错误，但值得每天可见——它决定了
    #     "系统到底在干什么"。当前实测约 6.25%。
    _mw = [
        (p.get("fusion_effective_weights") or {}).get("model")
        for p in _have_w
    ]
    _mw = [x for x in _mw if x is not None]
    if _mw:
        _avg_mw = sum(_mw) / len(_mw)
        if _avg_mw < 0.10:
            alerts.append(
                f"⚠ {day_str}: 模型有效权重均值仅 {_avg_mw:.1%}"
                f"（名义 {_nominal.get('model_weight', '?')}）"
                "——系统实质接近纯市场基准"
            )

    # 6c. 融合分支分布
    print(f"  融合分支({day_str}): "
          + " / ".join(f"{k}={v}" for k, v in sorted(srcs.items())))

    return alerts


def main() -> int:
    parser = argparse.ArgumentParser(description="数据完整性自检")
    parser.add_argument("--date", default="", help="目标日期，默认最近 3 天")
    parser.add_argument("--all", action="store_true", help="检查全部日期")
    parser.add_argument("--fail-on-critical", action="store_true",
                        help="关键字段（盘口/市场）全缺时以退出码 2 失败，供 workflow 在预测前阻断")
    args = parser.parse_args()

    if args.date:
        days = [args.date]
    elif args.all:
        days = sorted(d.name for d in DAILY.iterdir() if d.is_dir())
    else:
        days = []
        _today = date.fromisoformat(beijing_today())
        for i in range(3):
            d = (_today - timedelta(days=i)).isoformat()
            if (DAILY / d).exists():
                days.append(d)
        if not days:
            # fallback: 最近 3 个目录
            days = sorted(d.name for d in DAILY.iterdir() if d.is_dir())[-3:]

    all_alerts: list[str] = []
    for d in days:
        all_alerts.extend(check_day(d))

    # 6. DJYY 通道存活率（2026-10-11）：djyy_prob 曾静默死亡 6 周——CI 被
    #    Cloudflare 屏蔽期间生成的预测无 DJYY 数据，结算入账全 null，融合
    #    优化器把 DJYY 权重压到 0 而无人察觉。近 3 天中 2 天存活率 <50% 即
    #    报警；单日 <5 场视为小样本噪声跳过。
    djyy_rates: list[tuple[str, float]] = []
    for d in days:
        _pf = DAILY / d / "predictions.json"
        if not _pf.exists():
            continue
        try:
            _preds = json.loads(_pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(_preds, dict):
            _preds = _preds.get("predictions", _preds)
        if not _preds or len(_preds) < 5:
            continue
        _cnt = sum(1 for p in _preds if _djyy_alive(p))
        djyy_rates.append((d, _cnt / len(_preds)))
    _low_days = [d for d, r in djyy_rates if r < 0.5]
    if len(_low_days) >= 2:
        all_alerts.append(
            "⚠ DJYY 通道存活率 " + "、".join(f"{d}:{r:.0%}" for d, r in djyy_rates)
            + f" ——{len(_low_days)} 天低于 50%，融合优化器将失去第三通道样本"
            "（检查 DJYY 接口/Worker 或本地富化是否失效）")

    out = STATE / "selfcheck_report.json"
    out.write_text(json.dumps({
        "generated_at": beijing_today(),
        "checked_days": days,
        "n_alerts": len(all_alerts),
        "alerts": all_alerts,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 关键字段全缺 = 数据流断裂（波胆/总进球/半全场/让球/新浪/市场）
    # 2026-09-21: 小样本日（<5 场）不视为断裂——8/13 原型事故是 207 场 0% 抓取；
    # 周一仅 1 场且新浪恰好无该场时 0/1 全缺属覆盖噪声，误报会阻断整条流水线
    # （连正常数据一起提交不了）。告警仍保留展示，仅不触发阻断。
    _critical_fields = ("crs_odds", "ttg_odds", "hafu_odds", "handicap", "sina_odds", "market_fair")

    def _alert_day_n(a: str) -> int:
        import re
        m = re.search(r"全缺（0/(\d+)）", a)
        return int(m.group(1)) if m else 0

    _critical = [a for a in all_alerts
                 if "全缺" in a and any(f in a for f in _critical_fields)
                 and _alert_day_n(a) >= 5]

    if all_alerts:
        print(f"数据完整性自检: {len(all_alerts)} 条告警")
        for a in all_alerts:
            print(f"  {a}")

    if args.fail_on_critical and _critical:
        print(f"::error::关键字段全缺 {len(_critical)} 条，数据流断裂，中止（no-bet）")
        return 2

    # 2026-08-18 修复：普通告警只留痕，不阻断 workflow。
    # 此前 `if all_alerts: return 1` 让任何告警（含"当天 predictions.json
    # 不存在"——凌晨数据未出属正常）都以退出码 1 失败，bash -e 直接中止
    # step，导致 8/17 22:43 起 workflow 连续失败（每 30 分钟红一次）。
    # 真正的数据流断裂由上方 --fail-on-critical 的 return 2 负责，
    # 普通告警（历史留痕 + 当天数据未出）一律返回 0。
    if all_alerts:
        print(f"数据完整性自检完成（{len(days)} 天，{len(all_alerts)} 条告警，非致命）")
    else:
        print(f"数据完整性自检通过（{len(days)} 天无异常）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
