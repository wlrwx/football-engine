#!/usr/bin/env python3
"""挑战自动提交 · CI 端（确定性，1x2 市场；无浏览器无对话）

分工：CI 跑胜平负（数据全在仓库可定；本脚本）；让球市场需弹窗线路水位，
留给 Mac 端 ZCode cron（仅让球模式）——两侧共用 repo 内 challenge_state.json，git 同步。
凭据 env NJSTATS_JWT（Mac 每日 08:15 续推 secret）；缺失/过期→静默跳过退 0。
门槛逻辑与 docs/CHALLENGE_SUBSYSTEM.md 三层合同一致（只写策略层状态）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import njstats_api_submit as api  # noqa: E402
from engine.team_aliases import normalize_team  # noqa: E402

STATE = ROOT / "data" / "state" / "challenge_state.json"
POLICY = ROOT / "data" / "state" / "challenge_policy.json"
BJT = timezone(timedelta(hours=8))

# 挑战池译名 → 预测侧译名。池子用全称/异译，竞彩赛程用简称。
# 只用于挑战匹配，**不写进 engine/team_aliases.py**：那张表是 team_ratings
# 的键（normalize_team 的输出），改它等于合并 Elo 条目——实测这四个名字
# 两侧都已有独立 rating（萨迪纳摩 1598 vs 萨格勒布迪纳摩 1648、
# 利勒斯特伦 1454 vs 利勒斯特罗姆 1493、拉普大学 1679 vs 拉普拉塔大学生
# 1438），属模型层变更，要动得单独评估，不能混在挑战修复里。
_POOL_ALIAS = {
    "萨格勒布迪纳摩": "萨迪纳摩",
    "OFI克雷迪": "克里特",
    "利勒斯特罗姆": "利勒斯特伦",
    "拉普拉塔大学生": "拉普大学",
}
# 子串互含必须挡住梯队/女队：10-21 板上同时有「里尔 vs 加拉塔萨雷」和
# 「里尔U19 vs 加拉塔萨雷U19」，不挡就会把青年队当成同一场（一次两注）。
_SQUAD_MARKERS = ("B队", "二队", "II队", "U21", "U23", "U20", "U19", "U18",
                  "U17", "U16", "预备队", "青年队", "卡斯蒂亚", "女足", "女子")


def _canon(name: str) -> str:
    n = normalize_team(name)
    return _POOL_ALIAS.get(n, n)


def _contains(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a in b or b in a)


def _is_squad_variant(name: str) -> bool:
    return any(m in name for m in _SQUAD_MARKERS)


def match_prediction(pred: dict, home: str, away: str) -> dict | None:
    """池子 (home, away) → 预测记录。

    顺序：原文精确 → 归一化精确 → 归一化互含（主客同向，B 队不混）。
    2026-10-22 修：原实现的互含把主客写反了（拿池子主队比预测客队、
    拿池子客队比预测主队），实际只剩精确匹配。实测 10-22 板 11 场预测
    只进池 6 场，另 5 场都在板上（4 场是译名差异，1 场子串差异）。
    """
    p = pred.get((home, away))
    if p:
        return p
    hn, an = _canon(home), _canon(away)
    for (ph, pa), pp in pred.items():
        if hn == _canon(ph) and an == _canon(pa):
            return pp
    for (ph, pa), pp in pred.items():
        phn, pan = _canon(ph), _canon(pa)
        if not (_contains(hn, phn) and _contains(an, pan)):
            continue
        if _is_squad_variant(hn) != _is_squad_variant(phn) or _is_squad_variant(an) != _is_squad_variant(pan):
            continue
        return pp
    return None


# E10（用户口头称 E9；登记表 E9 已被 NJStats 侧挂实验占用，ID 不重用）：
# 旧筛「体彩价≥1.60」是 edge 时代的风险控制，ev_site 口径下会系统性偏掉
# 抽样——站点价对体彩价中位溢价 11.6%，低价热门换到站点价正好落进正 EV 区。
# 默认 1.60 = 不启用；policy 写 0.0 才放开。低赔候选单独成池，排在 E8
# 之后只吃本轮剩余名额，E8 的候选集合与排序完全不变。
LEGACY_MIN_SPORTTERY_ODDS = 1.60


def mult(o):
    if o < 1.60: return 0.0
    if o < 1.80: return 1.2
    if o < 2.31: return 1.4
    if o < 3.51: return 1.6
    if o < 4.51: return 1.8
    return 2.0


def iso_week(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"week": None, "quota": 28, "used": 0, "records": []}


def main() -> int:
    jwt = os.environ.get("NJSTATS_JWT", "").strip()
    if not jwt:
        print("challenge: 无 NJSTATS_JWT（待 Mac 续期），跳过")
        return 0
    now = datetime.now(BJT)
    st = load_state()
    wk = iso_week(now)
    rolled = st.get("week") != wk
    if rolled:
        st["week"], st["used"] = wk, 0
        if st.get("mode") == "W42-fill":
            st["mode"] = None
    # 周翻转必须立刻落盘（2026-10-19 修）：此前 STATE.write_text 只在「提交成功
    # 注数 > 0」的路径上执行，而零合格/已满/列表失败都会提前 return 0——
    # 于是周翻转后的 week/used 从未持久化。现场：state 停在 W42/27/28 且
    # mode 仍是 W42-fill，直到某轮投出 ≥1 注才整份写回自愈。
    # 注意别把影响说大：门槛没坏。翻转在算 g 之前，g 取自内存里翻转后的
    # mode，duty.log 的「门槛1.5%」即证。坏的是落盘状态——任何直接读该文件
    # 的消费者（人/看板/复盘脚本）会拿到错的周次配额，残留 mode 还会误导。
    # 这里只写「翻转」这一个动作，不触碰 records，避免零合格轮制造噪音提交。
    if rolled:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    remaining = st["quota"] - st["used"]
    if remaining <= 0:
        print(f"challenge: 本周已 {st['used']}/{st['quota']}，满")
        return 0
    fill = st.get("mode") == "W42-fill"
    g = 0.001 if fill else (0.015 if now.weekday() <= 3 else 0.008)
    # 闸门量纲（策略层口径，改动由 docs/EXPERIMENTS.md E8 裁决，默认保持 edge）：
    #   edge    = P − market_fair，模型与市场的可能性分歧，**与赔率无关**
    #   ev_site = P × 站点结算价 − 1，赌的是盘口软，不是模型聪明
    # 2026-10-22 实测：7 场未开赛对照里 edge 全为负（中位 −0.99pp，最大 −0.23pp），
    # 1.5pp 这一档不是"少"而是不可达（与 E3 结论一致：实测模型需权重 0.577，
    # 仅 4.8% 场次够得着）；同一批换成站点价 EV 则 5/7 ≥ 0、2 场 ≥ +2%。
    gate_mode, ev_floor = "edge", 0.02
    pol = {}
    try:
        pol = json.loads(POLICY.read_text()) if POLICY.exists() else {}
        if pol.get("gate_hint") == "tighten" and (pol.get("n_settled") or 0) >= 80:
            g += 0.005
        if pol.get("gate_mode") == "ev_site":
            # 先取数再切换：policy 写坏时整条丢弃，不能留下「口径换了、地板没换」
            ev_floor = float(pol.get("ev_floor", 0.02))
            gate_mode = "ev_site"
    except Exception as exc:
        print(f"challenge: policy 读取失败（{exc}），本轮用默认闸门 {gate_mode}/{ev_floor}")

    # E10 的开关独立于 E8：写坏只回落旧价筛，不能连带把 ev_site 闸门关掉。
    min_sp = LEGACY_MIN_SPORTTERY_ODDS
    if gate_mode == "ev_site":
        try:
            min_sp = float(pol.get("min_sporttery_odds", LEGACY_MIN_SPORTTERY_ODDS))
        except (TypeError, ValueError) as exc:
            print(f"challenge: E10 参数读取失败（{exc}），维持体彩价≥"
                  f"{LEGACY_MIN_SPORTTERY_ODDS:.2f} 旧筛")
            min_sp = LEGACY_MIN_SPORTTERY_ODDS

    # 用 board_with_odds 而不是 board_items：同一请求（modId=1012/msgId=1）、
    # 同一份响应，只是多带 f2 赔率包。挑战赛按**站点自己的赔率**结算，而
    # predictions.json 里是体彩官方赔率（实测首选方向价差中位 1.1161），
    # 所以下单那一刻的站点价必须跟着注单存进 records——事后每日快照是
    # 覆盖式的，存的是最后一轮的价，不是下单时的价。
    items = api.board_with_odds(jwt)
    if items is None:
        print("challenge: 列表失败（JWT 过期？），本轮跳过，明日 Mac 续期自愈")
        return 0
    pred = {}
    for d in (now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")):
        pf = ROOT / "data" / "daily" / d / "predictions.json"
        if not pf.exists():
            continue
        try:
            ps = json.loads(pf.read_text(encoding="utf-8"))
            ps = ps.get("predictions", ps) if isinstance(ps, dict) else ps
            for p in ps:
                if p.get("home_team"):
                    pred[(p["home_team"], p["away_team"])] = p
        except Exception:
            continue
    # 去重名单 = 本地 state ∪ 服务端已预测列表。
    #
    # 只靠本地 state 是不够的：CI 与本机是两个真正独立的仓库副本，各自
    # pull/push 同一份 challenge_state.json；两边同时跑到同一个候选时，
    # 谁先 push 谁赢，另一个仍会按自己那份旧 state 再投一遍。
    # 实测这个缺口真实存在：服务端 33 条 vs 本地 state 28 条，差 5 条
    # （朗斯vs葡萄牙体育、比利亚雷亚尔vs那不勒斯、莱比锡vs埃因霍温、
    #  阿森纳vs里尔、马竞vs曼联）——都是历史上 state 回写失败漏记的。
    #
    # msgId=11 是账号私有的「我已预测」服务端真值（无效 JWT 返回空，
    # 有效 JWT 返回全部）。以它为准，跨机器并发也不会重复下单。
    done = {r.get("match", "") for r in st.get("records", [])}
    _srv = api.my_predictions(jwt)
    if _srv:
        _extra = 0
        for x in _srv:
            m = f"{x.get('home')} vs {x.get('away')}"
            if m not in done:
                _extra += 1
            done.add(m)
        if _extra:
            print(f"challenge: 服务端已预测 {len(_srv)} 条，其中 {_extra} 条本地 state 没记（已并入去重）")
    else:
        # 取不到就退回纯本地 state，并留痕——这时的重复风险要能被看见
        print("challenge: ⚠ 取不到服务端已预测列表，本轮仅按本地 state 去重")

    cands = []
    e10_cands = []  # 体彩价 <1.60 但 ev_site 达标的候选，E10 独立池
    near = []  # 临时灌仓通道：-0.5pp ≤ edge < 门槛（CHALLENGE_TOPFILL 启用时按 EV 补足）
    used_pred = set()  # 同一预测命中多个板条目（梯队/异写）时只取第一条
    n_match = n_cand = 0
    best_ev = None
    floor_hits = []  # 被价筛挡住但站点EV达标的场次，仅用于日志归因
    for it in items:
        sid, h, a = it.get("sid"), it.get("home"), it.get("away")
        kts = it.get("kick_ts")
        if not sid or not kts or kts < now.timestamp() + 45 * 60:
            continue
        match = f"{h} vs {a}"
        if match in done:
            continue
        p = match_prediction(pred, h, a)
        if not p:
            continue
        pk = (p.get("home_team"), p.get("away_team"))
        if pk in used_pred:
            continue
        used_pred.add(pk)
        n_match += 1
        fp = [p.get("home_win_prob"), p.get("draw_prob"), p.get("away_win_prob")]
        mf, odds = p.get("market_fair"), [p.get("home_odds"), p.get("draw_odds"), p.get("away_odds")]
        if None in fp or not mf or not odds[0]:
            continue
        sel = max(range(3), key=lambda i: fp[i])
        o, P = odds[sel], fp[sel]
        if not o:
            continue
        e = P - mf[sel]
        site = it.get("odds")
        o_site = site[sel] if site and len(site) > sel else None
        ev_site = P * o_site - 1 if o_site else None
        # 上界护栏：外部盘口的解析出过错（f1 选项码不跨场次同义那类），
        # 站点价软到 EV>25% 不会是价值，是脏数据。实测今天最高 +6.9%。
        ev_ok = ev_site is not None and ev_floor <= ev_site <= 0.25
        # 旧筛 default 1.60；E10 由 policy.min_sporttery_odds 放开（设 0.0）。
        # 未放开时仍在日志留「价筛外」痕迹，供 E8 抽样偏差归因。
        if o < min_sp:
            if ev_ok:
                floor_hits.append((match, o, ev_site))
            continue
        n_cand += 1
        if ev_site is not None and (best_ev is None or ev_site > best_ev):
            best_ev = ev_site
        qualified = ev_ok if gate_mode == "ev_site" else e >= g
        is_e10 = o < LEGACY_MIN_SPORTTERY_ODDS
        rec = {"sid": sid, "match": match, "dir": sel, "p": P, "mkt_p": mf[sel],
               "edge": e, "odds": o, "site_odds": o_site, "ev_site": ev_site,
               "ev": P * 100 * mult(o), "gate": gate_mode, "e10": is_e10}
        if qualified:
            (e10_cands if is_e10 else cands).append(rec)
        elif not is_e10 and e >= float(os.environ.get("CHALLENGE_TOPFILL_FLOOR", "-0.005")):
            near.append(rec)
    # 排序：ev_site 档按价差质量排；edge 档保持原状按 `ev`（赔率档倍数×P）排，
    # 不顺手改既有口径——mult() 那套倍数与站点 33 注实盘得分对不上，是另一个待核项。
    cands.sort(key=lambda c: -(c["ev_site"] if gate_mode == "ev_site" else c["ev"]))
    K = min(8 if fill else 6, remaining)
    picked = cands[:K]
    try:
        topfill = min(int(os.environ.get("CHALLENGE_TOPFILL", "0") or 0), remaining)
    except ValueError:
        topfill = 0
    if topfill > len(picked):
        have = {c["sid"] for c in picked}
        for c in sorted(near, key=lambda c: -c["ev"]):
            if len(picked) >= topfill:
                break
            if c["sid"] in have:
                continue
            c["fill"] = True
            picked.append(c)
    if gate_mode == "ev_site" and e10_cands:
        # E10 排在 E8（含 topfill）之后，只吃本轮剩余名额：
        # E8 的候选集合、排序、每轮上限都不受影响，代价是 E8 满员时 E10 攒注慢。
        e10_cands.sort(key=lambda c: -c["ev_site"])
        room = K - len(picked)
        if room > 0:
            picked += e10_cands[:room]
    if not picked:
        thr = ev_floor if gate_mode == "ev_site" else g
        diag = f"/匹配{n_match}/候选{n_cand}"
        if best_ev is not None:
            diag += f"/最高{best_ev:+.2%}"
        if gate_mode == "ev_site" and floor_hits:
            fm, fo, fev = max(floor_hits, key=lambda x: x[2])
            diag += f"/价筛外{len(floor_hits)}场达标最高{fev:+.2%}({fm}@体彩{fo:.2f})"
        print(f"challenge: 零合格（{gate_mode} 门槛{thr:.1%}/池{len(items)}{diag}）——正常")
        return 0
    ok_n = 0
    for c in picked:
        if api.submit(jwt, c["sid"], 1, c["dir"] + 1) or api.submit(jwt, c["sid"], 1, c["dir"] + 1):
            ok_n += 1
            # 记录结构：market 表示玩法，line 表示让球线（1x2 时为 None）。
            # 本脚本只投 1x2（api.submit 第二参固定 1），所以这两个值是确定的；
            # 历史上有 5 条让球注被套用了这里的 "1x2"/None，导致复盘把让球
            # 混进 1x2 池子。这里写成显式常量 + 注释，避免后来者再复制错。
            st["records"].append({"match": c["match"], "pick": f"{['主胜','平','客胜'][c['dir']]}@{c['odds']:.2f}",
                                  "market": "1x2", "play": "1x2", "line": None,
                                  "sid": c["sid"], "p": round(c["p"], 4),
                                  "mkt_p": round(c["mkt_p"], 4), "edge": round(c["edge"], 4),
                                  "ev": round(c["ev"], 1), "via": "api-ci",
                                  # pick 里的数字是**体彩价**（predictions.json 口径）；
                                  # 挑战赛按站点价结算，两者差约 10%，故必须同时存下
                                  # 下单那一刻的站点价与两种量纲的合格判据。
                                  "gate": c["gate"],
                                  # 实验归属：E10 的低赔注必须可被 E8 裁决排除，
                                  # 否则两种抽样混在一起，两边都说不清。
                                  "exp": "E10" if c.get("e10") else "E8",
                                  "site_odds": round(c["site_odds"], 3) if c["site_odds"] else None,
                                  "ev_site": round(c["ev_site"], 4) if c["ev_site"] is not None else None,
                                  "mode": "临时灌仓1018(实验注)" if c.get("fill") else (st.get("mode") or "normal"),
                                  "submitted_at": now.strftime("%F %T")})
    st["used"] += ok_n
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    thr = ev_floor if gate_mode == "ev_site" else g
    _e10_diag = f"/E10候选{len(e10_cands)}" if e10_cands else ""
    print(f"challenge: 提交 {ok_n} 注 → {st['used']}/{st['quota']} {gate_mode} 门槛{thr:.1%}"
          f"（匹配{n_match}/候选{n_cand}{_e10_diag}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
