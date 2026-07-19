"""静态报告生成器 - GitHub Pages 全功能仪表盘

展示: 预测概率/赔率对比/价值检测/xG/置信度/冷门风险/三票方案/熔断状态/决策链完整性
"""
import json
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent


def build_site():
    """生成静态 HTML 报告"""
    web_dir = ROOT / "web"
    web_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    daily_dir = ROOT / "data" / "daily" / today

    # 加载所有数据
    predictions = _load_json(daily_dir / "predictions.json", [])
    bundle = _load_json(daily_dir / f"decision_bundle_{today}.json", {})
    # 尝试带版本号
    if not bundle:
        bundle = _load_json(daily_dir / f"decision_bundle_{today}_v1.json", {})
    ticket = _load_json(daily_dir / "ticket_plan.json", {})
    breaker = _load_json(ROOT / "data" / "state" / "circuit_breaker.json", {})
    health = _load_json(web_dir / "health-status.json", {"healthy": True})

    html = _render_html(today, predictions, bundle, ticket, breaker, health)
    (web_dir / "index.html").write_text(html, encoding="utf-8")

    status = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "prediction_count": len(predictions),
        "bundle_hash": bundle.get("bundle_sha256", "")[:16],
        "healthy": health.get("healthy", True),
    }
    (web_dir / "report-status.json").write_text(json.dumps(status, indent=2))
    print(f"[build_site] 仪表盘已生成: web/index.html ({len(predictions)} 场)")


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _render_html(today, predictions, bundle, ticket, breaker, health):
    # 计算摘要
    total = len(predictions)
    # 三票方案中的场次 = 真正的价值投注
    value_matches = set()
    for it in ticket.get("stable", []) + ticket.get("value", []):
        value_matches.add(it.get("match", ""))
    value_bets = [p for p in predictions if _is_value(p, value_matches)]
    avg_conf = sum(p.get("confidence", 0) for p in predictions) / max(1, total)
    total_stake = ticket.get("total_stake", 0)
    exp_roi = ticket.get("expected_roi", 0)
    breaker_mult = ticket.get("breaker_multiplier", 1.0)
    streak = breaker.get("current_streak", 0)
    tier = _breaker_tier(breaker)

    # 渲染比赛卡片
    cards = ""
    for p in sorted(predictions, key=lambda x: -x.get("confidence", 0)):
        cards += _match_card(p, value_matches)

    # 三票方案
    ticket_html = _ticket_section(ticket, predictions)

    # 系统面板
    system_html = _system_panel(breaker, bundle, tier, breaker_mult)

    health_badge = '<span class="badge ok">SYSTEM ONLINE</span>' if health.get("healthy") else '<span class="badge warn">DEGRADED</span>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Football Engine - {today}</title>
<style>
:root {{
  --bg: #0a0e13; --surface: #131920; --surface2: #1a222c;
  --border: #2a3440; --text: #e8ecf0; --dim: #8899a6;
  --blue: #3b82f6; --red: #ef4444; --green: #22c55e;
  --amber: #f59e0b; --purple: #a855f7;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
.page {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px; }}

/* Header */
.header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }}
.header h1 {{ font-size: 1.4rem; font-weight: 700; letter-spacing: -0.5px; }}
.header .sub {{ color: var(--dim); font-size: 0.82rem; margin-top: 4px; }}
.badge {{ display: inline-block; padding: 3px 10px; border-radius: 10px; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.5px; }}
.badge.ok {{ background: #0d3320; color: var(--green); }}
.badge.warn {{ background: #3d2000; color: var(--amber); }}

/* Stats bar */
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 24px; }}
.stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }}
.stat .label {{ font-size: 0.68rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px; }}
.stat .value {{ font-size: 1.3rem; font-weight: 700; margin-top: 2px; }}
.stat .value.green {{ color: var(--green); }}
.stat .value.amber {{ color: var(--amber); }}
.stat .value.red {{ color: var(--red); }}

/* Section titles */
.section-title {{ font-size: 0.9rem; font-weight: 600; margin: 28px 0 12px; padding-left: 10px; border-left: 3px solid var(--blue); }}

/* Match cards */
.match {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 10px; transition: border-color .2s; }}
.match:hover {{ border-color: var(--blue); }}
.match.value-pick {{ border-left: 3px solid var(--green); }}
.match-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
.league-tag {{ font-size: 0.7rem; color: var(--dim); background: var(--surface2); padding: 2px 8px; border-radius: 4px; }}
.match-id {{ font-size: 0.68rem; color: var(--dim); }}
.teams {{ display: flex; justify-content: center; align-items: center; gap: 16px; margin: 6px 0 12px; }}
.team {{ font-size: 1.05rem; font-weight: 600; min-width: 80px; }}
.team.home {{ text-align: right; }}
.team.away {{ text-align: left; }}
.vs {{ color: var(--dim); font-size: 0.75rem; }}

/* Prob bar */
.prob-row {{ display: flex; height: 26px; border-radius: 6px; overflow: hidden; margin: 8px 0; }}
.prob-seg {{ display: flex; align-items: center; justify-content: center; font-size: 0.68rem; font-weight: 600; color: #fff; min-width: 36px; }}
.prob-seg.h {{ background: var(--blue); }}
.prob-seg.d {{ background: #4b5563; }}
.prob-seg.a {{ background: var(--red); }}

/* Detail grid */
.detail-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-top: 10px; }}
.detail {{ font-size: 0.72rem; color: var(--dim); }}
.detail b {{ color: var(--text); font-weight: 600; }}
.detail .val {{ color: var(--green); }}
.detail .risk {{ color: var(--red); }}

/* Confidence meter */
.conf-meter {{ display: inline-flex; align-items: center; gap: 4px; }}
.conf-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
.conf-dot.high {{ background: var(--green); }}
.conf-dot.med {{ background: var(--amber); }}
.conf-dot.low {{ background: var(--dim); }}

/* Ticket section */
.ticket-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }}
.ticket-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
.ticket-card h4 {{ font-size: 0.8rem; margin-bottom: 8px; }}
.ticket-card h4.stable {{ color: var(--green); }}
.ticket-card h4.value {{ color: var(--blue); }}
.ticket-card h4.lottery {{ color: var(--purple); }}
.ticket-item {{ display: flex; justify-content: space-between; font-size: 0.75rem; padding: 4px 0; border-bottom: 1px solid var(--border); }}
.ticket-item:last-child {{ border-bottom: none; }}
.ticket-empty {{ font-size: 0.75rem; color: var(--dim); font-style: italic; }}

/* System panel */
.sys-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }}
.sys-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
.sys-card h4 {{ font-size: 0.78rem; color: var(--dim); margin-bottom: 8px; }}
.sys-row {{ display: flex; justify-content: space-between; font-size: 0.75rem; padding: 3px 0; }}
.sys-row .k {{ color: var(--dim); }}
.hash {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.68rem; color: var(--purple); word-break: break-all; }}

/* Footer */
.footer {{ margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--border); font-size: 0.72rem; color: var(--dim); }}
.footer a {{ color: var(--blue); text-decoration: none; }}

@media (max-width: 600px) {{
  .stats {{ grid-template-columns: repeat(2, 1fr); }}
  .detail-grid {{ grid-template-columns: 1fr 1fr; }}
  .teams {{ gap: 8px; }}
  .team {{ font-size: 0.9rem; min-width: 60px; }}
}}
</style>
</head>
<body>
<div class="page">
  <div class="header">
    <div>
      <h1>Football Engine</h1>
      <div class="sub">{today} | DC+MC集成 | Shin去水 | 逆向赔率 | 四源融合</div>
    </div>
    {health_badge}
  </div>

  <div class="stats">
    <div class="stat"><div class="label">Matches</div><div class="value">{total}</div></div>
    <div class="stat"><div class="label">Value Bets</div><div class="value green">{len(value_bets)}</div></div>
    <div class="stat"><div class="label">Avg Conf</div><div class="value">{avg_conf:.0%}</div></div>
    <div class="stat"><div class="label">Total Stake</div><div class="value amber">&yen;{total_stake:.0f}</div></div>
    <div class="stat"><div class="label">Exp ROI</div><div class="value {'green' if exp_roi > 1 else 'red'}">{exp_roi:.2f}x</div></div>
    <div class="stat"><div class="label">Breaker</div><div class="value {'green' if tier == 0 else 'red'}">T{tier} x{breaker_mult:.1f}</div></div>
  </div>

  <div class="section-title">Match Predictions</div>
  {cards if cards else '<p style="color:var(--dim);padding:40px;text-align:center;">Waiting for daily pipeline...</p>'}

  {ticket_html}

  {system_html}

  <div class="footer">
    <p>Prediction Chain: Dixon-Coles(60%) + Monte Carlo 50K(40%) &rarr; Shin Devig &rarr; Reverse Odds &rarr; Same-Odds &rarr; Fusion(Model 60% + Market 25% + DJYY 15%) &rarr; LGBM(10%) &rarr; Isotonic &rarr; Wilson</p>
    <p style="margin-top:6px;">Data: Sporttery / Sina / 500wan / DJYY | Zero-server GitHub Actions | <a href="https://github.com/wlrwx/football-engine">Source</a></p>
    <p style="margin-top:6px;">仅供研究学习，不构成投注建议。</p>
  </div>
</div>
</body>
</html>"""


def _match_card(p, value_matches=None):
    hp = p.get("home_win_prob", 0) * 100
    dp = p.get("draw_prob", 0) * 100
    ap = p.get("away_win_prob", 0) * 100
    conf = p.get("confidence", 0)
    conf_cls = "high" if conf > 0.6 else "med" if conf > 0.4 else "low"
    is_val = _is_value(p, value_matches)

    # 赔率 vs 模型隐含对比
    odds_h = p.get("home_odds") or 0
    odds_d = p.get("draw_odds") or 0
    odds_a = p.get("away_odds") or 0
    implied_h = (1 / odds_h * 100) if odds_h else 0
    edge_h = hp - implied_h

    upset = p.get("reverse_upset_risk", 0)
    xg_h = p.get("home_xg", 0)
    xg_a = p.get("away_xg", 0)
    djyy = p.get("djyy_model_prob")
    same = p.get("same_odds_matched", 0)

    djyy_str = ""
    if djyy and djyy.get("home"):
        djyy_str = f'DJYY: {djyy["home"]*100:.0f}/{djyy.get("draw",0)*100:.0f}/{djyy.get("away",0)*100:.0f}%'

    return f"""
  <div class="match {'value-pick' if is_val else ''}">
    <div class="match-top">
      <span class="league-tag">{p.get('competition','')}</span>
      <span class="match-id">{p.get('match_id','').split('_',1)[-1]}</span>
    </div>
    <div class="teams">
      <span class="team home">{p.get('home_team','')}</span>
      <span class="vs">VS</span>
      <span class="team away">{p.get('away_team','')}</span>
    </div>
    <div class="prob-row">
      <div class="prob-seg h" style="width:{hp:.1f}%">H {hp:.0f}%</div>
      <div class="prob-seg d" style="width:{dp:.1f}%">D {dp:.0f}%</div>
      <div class="prob-seg a" style="width:{ap:.1f}%">A {ap:.0f}%</div>
    </div>
    <div class="detail-grid">
      <div class="detail">Confidence: <span class="conf-meter"><span class="conf-dot {conf_cls}"></span><b>{conf:.0%}</b></span></div>
      <div class="detail">xG: <b>{xg_h:.2f}</b> - <b>{xg_a:.2f}</b></div>
      <div class="detail">Odds: <b>{odds_h}/{odds_d}/{odds_a}</b></div>
      <div class="detail">Edge(H): <b class="{'val' if edge_h > 3 else ''}">{edge_h:+.1f}%</b></div>
      <div class="detail">Upset Risk: <b class="{'risk' if upset > 40 else ''}">{upset:.0f}%</b></div>
      <div class="detail">Same-Odds: <b>{same}</b> hits</div>
      {f'<div class="detail">{djyy_str}</div>' if djyy_str else ''}
      {f'<div class="detail"><b class="val">VALUE PICK</b></div>' if is_val else ''}
    </div>
  </div>"""


def _ticket_section(ticket, predictions):
    if not ticket:
        return ""
    stable = ticket.get("stable", [])
    value = ticket.get("value", [])
    lottery = ticket.get("lottery", [])

    def _items(items):
        if not items:
            return '<div class="ticket-empty">None</div>'
        html = ""
        for it in items:
            match_short = it.get("match", "").split("_", 1)[-1]
            # find team names
            teams = match_short
            for p in predictions:
                if p.get("match_id") == it.get("match"):
                    teams = f'{p["home_team"]} vs {p["away_team"]}'
                    break
            sel_map = {"home": "主胜", "draw": "平局", "away": "客胜"}
            sel = sel_map.get(it.get("sel", ""), it.get("sel", ""))
            html += f'<div class="ticket-item"><span>{teams} [{sel}]</span><span>@{it.get("odds",0):.2f} / &yen;{it.get("stake",0):.0f}</span></div>'
        return html

    return f"""
  <div class="section-title">Betting Plan (三票制 60/30/10)</div>
  <div class="ticket-grid">
    <div class="ticket-card"><h4 class="stable">稳胆 (60%)</h4>{_items(stable)}</div>
    <div class="ticket-card"><h4 class="value">搏冷 (30%)</h4>{_items(value)}</div>
    <div class="ticket-card"><h4 class="lottery">彩票 (10%)</h4>{_items(lottery)}</div>
  </div>"""


def _system_panel(breaker, bundle, tier, mult):
    streak = breaker.get("current_streak", 0)
    wins = breaker.get("total_wins", 0)
    losses = breaker.get("total_losses", 0)
    wr = wins / max(1, wins + losses)
    daily_pnl = breaker.get("daily_pnl", 0)
    weekly_pnl = breaker.get("weekly_pnl", 0)
    sha = bundle.get("bundle_sha256", "N/A")
    created = bundle.get("created_at", "")

    return f"""
  <div class="section-title">System Status</div>
  <div class="sys-grid">
    <div class="sys-card">
      <h4>CIRCUIT BREAKER</h4>
      <div class="sys-row"><span class="k">Tier</span><span>{tier} (x{mult:.2f})</span></div>
      <div class="sys-row"><span class="k">Streak</span><span>{streak:+d}</span></div>
      <div class="sys-row"><span class="k">Win Rate</span><span>{wr:.1%} ({wins}W/{losses}L)</span></div>
      <div class="sys-row"><span class="k">Daily PnL</span><span>&yen;{daily_pnl:+.0f}</span></div>
      <div class="sys-row"><span class="k">Weekly PnL</span><span>&yen;{weekly_pnl:+.0f}</span></div>
    </div>
    <div class="sys-card">
      <h4>DECISION INTEGRITY</h4>
      <div class="sys-row"><span class="k">Created</span><span>{created[:19] if created else 'N/A'}</span></div>
      <div class="sys-row"><span class="k">Version</span><span>{bundle.get('version', 'v1')}</span></div>
      <div class="hash">SHA-256: {sha}</div>
    </div>
    <div class="sys-card">
      <h4>MODEL CONFIG</h4>
      <div class="sys-row"><span class="k">Ensemble</span><span>DC 60% + MC 40%</span></div>
      <div class="sys-row"><span class="k">Fusion</span><span>Model 60 / Market 25 / DJYY 15</span></div>
      <div class="sys-row"><span class="k">LGBM</span><span>Cold start (&lt;500 samples)</span></div>
      <div class="sys-row"><span class="k">Calibration</span><span>Isotonic (pending)</span></div>
      <div class="sys-row"><span class="k">MC Sims</span><span>50,000</span></div>
    </div>
  </div>"""


def _is_value(p, value_matches=None):
    """判断是否为价值投注: 仅当被三票方案选中（稳胆/搏冷）"""
    if value_matches and p.get("match_id") in value_matches:
        return True
    return False


def _breaker_tier(breaker):
    streak = abs(min(breaker.get("current_streak", 0), 0))
    if breaker.get("halted"):
        return 4
    if streak >= 15:
        return 4
    if streak >= 12:
        return 3
    if streak >= 6:
        return 2
    if streak >= 3:
        return 1
    return max(0, breaker.get("tier", 0))


if __name__ == "__main__":
    build_site()
