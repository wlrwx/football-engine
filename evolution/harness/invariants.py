"""结构不变式检查 —— 检测「量纲错 / 符号反 / 概率非法」这类**事实性错误**。

与 Brier 门槛的本质区别：
  Brier 问「这个改动有没有统计显著的收益」；
  不变式问「这段代码算出来的东西在数学上是否成立」。

一个量纲用错的函数，即使它碰巧让 Brier 变好一点，也是错的。
因此事实性错误必须能绕过统计门槛被直接拦截，否则系统会因为
「市场权重占大头、看不出效果」而永远不修已确认的 bug。
"""

from __future__ import annotations

import math


def _res(issue_id: str, violated: bool, detail: str = "", observed=None,
         expected=None) -> dict:
    """构造不变式结果。violated=True 表示检测到缺陷。

    （历史：曾把第二参数当 'ok' 再反相，导致全部标志翻转；
      现第二参数直接就是 violated，避免再分。）
    """
    return {
        "issue_id": issue_id,
        "violated": bool(violated),
        "detail": detail,
        "observed": observed,
        "expected": expected,
    }


def check_probability_vector(probs) -> bool:
    """1X2 概率必须非负、和为 1。"""
    if probs is None or len(probs) != 3:
        return False
    if any((p is None) or p < -1e-9 for p in probs):
        return False
    return abs(sum(probs) - 1.0) < 1e-6


def check_unit_dimension(model_core_module=None, sample_rating: dict | None = None) -> dict:
    """攻防项的量纲检查。

    真实缺陷（DI-ATTACK-LOG）：`team_ratings.json` 里 attack/defense 是
    **以 1.0 为中心的比例因子**，但在期望进球的对数域里必须以
    `log(ratio)` 进入。如果把比例因子直接加进 log 域，结果是：

        防守好(defense≈0.84)  →  +0.84  →  **抬高**对手 xG（方向反了）

    判据：直接看 `model_core.dc_expected_goals` 的实现用的是线性还是 log。
    由于比例因子以 1.0 为中心，正确做法必须包含 math.log(attack)。
    """
    smp = sample_rating or {"elo": 1500.0, "attack": 1.0, "defense": 1.0,
                           "form": 0.0, "injury": 0.0, "rest_days": 3}
    import importlib
    from . import model_core as M
    mod = model_core_module or M
    h = type("R", (), {"elo": smp["elo"], "attack": 1.0,
                       "defense": smp["defense"], "form": 0.0,
                       "injury": 0.0, "rest_days": 3, "name": "H"})()
    a1 = type("R", (), {"elo": smp["elo"], "attack": 1.0,
                        "defense": 0.7, "form": 0.0,
                        "injury": 0.0, "rest_days": 3, "name": "A"})()
    a2 = type("R", (), {"elo": smp["elo"], "attack": 1.0,
                        "defense": 1.4, "form": 0.0,
                        "injury": 0.0, "rest_days": 3, "name": "A"})()
    cfg = dict(mod.DEFAULT_PRED) if hasattr(mod, "DEFAULT_PRED") else {}
    try:
        hx1, _ = mod.dc_expected_goals(h, a1, cfg)
        hx2, _ = mod.dc_expected_goals(h, a2, cfg)
    except Exception as ex:  # noqa: BLE001
        return _res("DI-ATTACK-LOG-DIMENSION", True,
                    f"无法运行 dc_expected_goals：{ex}")
    # 正确：对手防守好(defense 0.7) → 主队 xG 更低；防守差(1.4) → 更高
    correct = hx1 < hx2
    return _res(
        "DI-ATTACK-LOG-DIMENSION",
        not correct,
        f"对手 defense 0.7→1.4 时主队 xG {hx1:.3f}→{hx2:.3f}；"
        + ("方向正确（防守越差对手进球越多）" if correct
           else "方向错误（防守变好反而抬高对手 xG）= 量纲/符号缺陷"),
        observed={"xG_when_def_0.7": round(hx1, 4),
                  "xG_when_def_1.4": round(hx2, 4)},
        expected="defense 变好（值变小）→ 对手 xG 下降",
    )


def check_dead_features(ratings: dict) -> dict:
    """DI-DEAD-FEATURES：injury / rest_days 是否恒为常量。

    若 injury 全为 0、rest_days 全为默认值，则
    `injury_weight` / `rest_weight` 两个配置项从未参与任何计算 ——
    配置文件里存在从未生效的旋钮。
    """
    if not ratings:
        return _res("DI-DEAD-FEATURES", True, "ratings 为空，无法检查")
    inj = {round(float(v.get("injury", 0.0)), 6) for v in ratings.values()}
    rest = {int(v.get("rest_days", 3)) for v in ratings.values()}
    n_inj, n_rest = len(inj), len(rest)
    violated = (n_inj <= 1 and n_rest <= 1)   # 恒为常量 => 从未生效
    return _res(
        "DI-DEAD-FEATURES",
        violated,
        f"injury 取值种数={n_inj} {sorted(inj)[:5]}；"
        f"rest_days 取值种数={n_rest} {sorted(rest)[:5]}；"
        f"两者均为常量 => injury_weight/rest_weight 从未生效",
        observed={"injury_values": n_inj, "rest_values": n_rest, "n_teams": len(ratings)},
        expected="injury / rest_days 应随比赛历史变化",
    )


def check_anchor_semantics(league_params: dict, ledger: list[dict]) -> dict:
    """DI-DRAW-BASELINE-SEMANTICS：draw_baseline 是否被误当作真实平局率。

    缺陷机制：`league_params.draw_baseline` 的文档写「该联赛实际平局率」，
    但其数据来源是 `draw_hits / draw_predictions` —— 即
    **系统判平的场次中真正打平的比例**，而不是联赛的真实平局率。

    这构成自我强化：判平越多 → draw_predictions 越多 → 若判平常错则该值越低；
    而它被当作平局率用于 `target_d = baseline × strength`，会把平局概率
    推到错误位置（实测巴甲钉死在 0.5025，真实平局率仅 0.316）。
    """
    if not league_params or not ledger:
        return _res("DI-DRAW-BASELINE-SEMANTICS", True, "数据不足")
    actual = {}
    for r in ledger:
        lg = r.get("league")
        if lg:
            a = actual.setdefault(lg, [0, 0])
            a[0] += 1
            a[1] += 1 if r.get("actual_idx") == 1 else 0

    violations = []
    for lg, v in league_params.items():
        db = v.get("draw_baseline")
        if db is None or db < 0.35:
            continue          # 只检查会触发抬升的联赛
        if lg not in actual or actual[lg][0] < 20:
            continue          # 样本不足不下结论
        real_rate = actual[lg][1] / actual[lg][0]
        if abs(db - real_rate) > 0.15:
            violations.append({
                "league": lg,
                "draw_baseline": round(db, 3),
                "real_draw_rate": round(real_rate, 3),
                "n": actual[lg][0],
                "error": round(db - real_rate, 3),
            })
    violated = len(violations) > 0
    return _res(
        "DI-DRAW-BASELINE-SEMANTICS",
        violated,
        ("draw_baseline 与真实平局率严重背离：" +
         "; ".join(f"{v['league']} 基线{v['draw_baseline']} vs 真实{v['real_draw_rate']}"
                   f"(n={v['n']}, 误差{v['error']:+})" for v in violations))
        if violated else "各联赛 draw_baseline 与真实平局率一致（n≥20）",
        observed=violations,
        expected="|draw_baseline − 真实平局率| ≤ 0.15",
    )


def check_market_anchor_missing(ledger: list[dict]) -> dict:
    """SYNTHETIC-ODDS-NO-MARKET：合成赔率场次是否丢失市场锚。

    实测 2026-10-09 的 12 场中有 1 场 `odds_synthetic=True` 且
    `market_fair=None`，trace 只有 `base_model → combo_boost`，
    **完全绕过融合**，概率 100% 来自裸模型。
    近 12 天 116 场中 8 场如此。
    """
    n = 0
    for r in ledger:
        if r.get("_odds_synthetic") and not r.get("market_fair"):
            n += 1
    violated = n > 0
    return _res(
        "SYNTHETIC-ODDS-NO-MARKET",
        violated,
        f"{n} 场合成赔率且 market_fair=None，完全绕过市场融合" if violated
        else "所有场次均有市场锚",
        observed={"n_synthetic_without_market": n},
        expected=0,
    )


def check_ledger_chain_purity(ledger: list[dict], current_chain: str = "v2") -> dict:
    """DI-LEDGER-MULTI-CHAIN：账本是否跨了多代融合链。

    若账本混有旧链行，用当前代码 replay 会得到系统性偏差
    （实测混放时 0.018，单独当前链 0.0001），会让整个自检失去意义。
    """
    chains = {}
    for r in ledger:
        c = r.get("chain", "?")
        chains[c] = chains.get(c, 0) + 1
    multi = len([c for c in chains if c != "?"]) > 1
    return _res(
        "DI-LEDGER-MULTI-CHAIN",
        multi,
        f"账本含多代融合链 {chains}；replay 必须限定到 {current_chain}"
        if multi else f"账本链单一 {chains}",
        observed=chains,
        expected=f"仅 {current_chain}",
    )


def check_combo_boost_entropy(ctx: dict | None = None) -> dict:
    """DI-COMBO-BOOST-ENTROPY：combo_boost 无条件给最大项加分，不含新信息。

    这是一种结构性的熵减：把概率往已是最高的方向挤，不引入任何信号。
    判据：若该步骤被启用（post_fusion.combo_boost=True），且它收到正注入量，
    它就必然作用于 argmax 方向 —— 这是代码结构决定的，不是数据结论。
    """
    cfg = (ctx or {}).get("_combo_boost_on", None)
    if cfg is None:
        cfg = True
    violated = bool(cfg)
    return _res(
        "DI-COMBO-BOOST-ENTROPY",
        violated,
        "combo_boost 步骤若启用，会无条件给当前 argmax 方向加分"
        "（fusion.py 步骤 3：best=max([('H',h),('D',d),('A',a)])），"
        "不引入任何新信息，只是把分布往峰值挤",
        observed={"enabled": cfg},
        expected="该步骤应关闭，或改为仅在有独立信号证据时触发",
    )


def run_all(ratings: dict | None = None, league_params: dict | None = None,
            ledger: list[dict] | None = None,
            pred_inputs: dict | None = None,
            ctx: dict | None = None) -> dict[str, dict]:
    """跑全部不变式。返回 {issue_id: result}。"""
    out = {}
    out["DI-ATTACK-LOG-DIMENSION"] = check_unit_dimension(
        None, {"elo": 1500.0, "attack": 1.0, "defense": 1.0,
               "form": 0.0, "injury": 0.0, "rest_days": 3})
    if ratings:
        out["DI-DEAD-FEATURES"] = check_dead_features(ratings)
    if league_params and ledger:
        out["DI-DRAW-BASELINE-SEMANTICS"] = check_anchor_semantics(
            league_params, ledger)
    if ledger:
        out["SYNTHETIC-ODDS-NO-MARKET"] = check_market_anchor_missing(ledger)
        out["DI-LEDGER-MULTI-CHAIN"] = check_ledger_chain_purity(ledger)
    out["DI-COMBO-BOOST-ENTROPY"] = check_combo_boost_entropy(ctx)
    return out
