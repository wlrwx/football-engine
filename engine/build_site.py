from __future__ import annotations
"""静态报告生成器 - 专业体育分析仪表盘

展示: 预测概率/赔率对比/价值检测/xG/置信度/冷门风险/三票方案/熔断状态/决策链完整性
交互式: 点击展开比赛详情, 多Tab分析面板, 响应式布局
"""
import json
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent


def build_site():
    """生成静态 HTML 报告（多日期）"""
    web_dir = ROOT / "web"
    web_dir.mkdir(parents=True, exist_ok=True)

    daily_root = ROOT / "data" / "daily"
    today = date.today().isoformat()

    # 收集所有有预测数据的日期
    all_dates = []
    if daily_root.exists():
        all_dates = sorted(
            [d.name for d in daily_root.iterdir() if d.is_dir() and (d / "predictions.json").exists()],
            reverse=True,
        )

    if not all_dates:
        all_dates = [today]

    # 构建全局结果索引（扫描所有日期目录的 results.json，按队名索引）
    all_results = _load_all_results(daily_root, all_dates)

    # 缓存 league_matrix 到本地（从 DJYY 获取）
    league_matrix_path = ROOT / "data" / "league_matrix.json"
    if not league_matrix_path.exists():
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://djyylive.com/data/league-matrix.json",
                headers={"User-Agent": "football-engine/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                league_matrix_path.parent.mkdir(parents=True, exist_ok=True)
                league_matrix_path.write_bytes(data)
        except Exception:
            pass

    # 为每个日期生成页面
    for target_date in all_dates:
        daily_dir = daily_root / target_date
        predictions = _load_json(daily_dir / "predictions.json", [])
        bundle = _load_json(daily_dir / f"decision_bundle_{target_date}.json", {})
        if not bundle:
            bundle = _load_json(daily_dir / f"decision_bundle_{target_date}_v1.json", {})
        ticket = _load_json(daily_dir / "ticket_plan.json", {})
        breaker = _load_json(ROOT / "data" / "state" / "circuit_breaker.json", {})
        health = _load_json(web_dir / "health-status.json", {"healthy": True})
        results = _load_json(daily_dir / "results.json", [])
        # 如果当日 results.json 为空，用全局索引匹配（仅对历史日期）
        from datetime import date as dt_date
        if not results and predictions:
            is_today = (target_date == dt_date.today().isoformat())
            if not is_today:
                results = _match_results_to_predictions(predictions, all_results)
        review_ledger = _load_ledger(ROOT / "data" / "state" / "review_ledger.jsonl", target_date)
        results_html_preds = predictions

        html = _render_html(target_date, predictions, bundle, ticket, breaker, health, results, results_html_preds, all_dates, review_ledger)

        # 最新日期写index.html, 所有日期写dated页面
        if target_date == all_dates[0]:
            (web_dir / "index.html").write_text(html, encoding="utf-8")
        (web_dir / f"{target_date}.html").write_text(html, encoding="utf-8")

    status = {
        "date": all_dates[0] if all_dates else today,
        "generated_at": datetime.now().isoformat(),
        "prediction_count": len(_load_json(daily_root / all_dates[0] / "predictions.json", [])) if all_dates else 0,
        "available_dates": all_dates,
        "healthy": True,
    }
    (web_dir / "report-status.json").write_text(json.dumps(status, indent=2))
    print(f"[build_site] 仪表盘已生成: {len(all_dates)} 个日期页面")


def _load_all_results(daily_root: Path, all_dates: list) -> dict:
    """扫描所有日期目录，构建全局 results 索引（按队名 + match_id）"""
    index = {}
    search_dates = set(all_dates)
    # 也扫描预测日期前后2天（结算可能跨天）
    for d in all_dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            for offset in range(-2, 3):
                from datetime import timedelta
                adj = (dt + timedelta(days=offset)).strftime("%Y-%m-%d")
                search_dates.add(adj)
        except Exception:
            pass

    for d in sorted(search_dates):
        results_file = daily_root / d / "results.json"
        if not results_file.exists():
            continue
        try:
            results = json.loads(results_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in results:
            mid = r.get("match_id", "")
            if mid:
                index[mid] = r
            hm = r.get("home_team", "")
            aw = r.get("away_team", "")
            if hm and aw:
                key = f"{hm}_vs_{aw}"
                # 不覆盖已有的精确 match_id 索引
                if key not in index:
                    index[key] = r
            # 场次号索引
            fixture = _extract_fixture(mid)
            if fixture and fixture not in index:
                index[fixture] = r
    return index


def _match_results_to_predictions(predictions: list, all_results: dict) -> list:
    """用全局索引为预测匹配赛果，返回匹配的 results 列表"""
    matched = []
    for p in predictions:
        mid = p.get("match_id", "")
        r = all_results.get(mid)
        if not r:
            fixture = _extract_fixture(mid)
            if fixture:
                r = all_results.get(fixture)
        if not r:
            hm = p.get("home_team", "")
            aw = p.get("away_team", "")
            if hm and aw:
                r = all_results.get(f"{hm}_vs_{aw}")
        if r:
            matched.append(r)
    return matched


def _load_ledger(ledger_path: Path, target_date: str) -> list:
    """从 review_ledger.jsonl 读取指定日期的复盘记录"""
    records = []
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("date") == target_date:
                    records.append(r)
            except Exception:
                pass
    return records


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _extract_fixture(match_id: str) -> str:
    """从 match_id 提取场次号，如 '2026-07-20_周日201' → '201'"""
    if not match_id:
        return ""
    import re
    parts = re.split(r'[_\-]', match_id)
    for part in reversed(parts):
        m = re.search(r'(\d+)$', part)
        if m:
            return m.group(1)
    return ""


def _extract_team_key(match_id: str) -> str:
    """从 match_id 提取队名键，如 '2026-07-21_周二201' → 从 predictions 中查找对应队名"""
    return ""


def _slug(s: str) -> str:
    """生成 CSS-safe slug"""
    import re
    return re.sub(r'[^a-zA-Z\u4e00-\u9fff]', '-', s)


def _render_html(today, predictions, bundle, ticket, breaker, health, results=None, results_preds=None, all_dates=None, review_ledger=None):
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
    tier, tier_reason = _breaker_tier(breaker)

    # 联赛矩阵面板
    league_matrix = _load_league_matrix(ROOT / "data" / "league_matrix.json")
    league_matrix_html = _league_matrix_section(league_matrix, predictions)

    # 渲染比赛卡片（按联赛分组）
    cards = ""
    results_map = {}
    if results:
        for r in results:
            mid = r.get("match_id", "")
            results_map[mid] = r
            fixture = _extract_fixture(mid)
            if fixture:
                results_map[fixture] = r
            # 队名索引（最可靠，跨数据源通用）
            hm = r.get("home_team", "")
            aw = r.get("away_team", "")
            if hm and aw:
                results_map[f"{hm}_vs_{aw}"] = r
    elif review_ledger:
        # 从 review_ledger 构建 results_map（含比分推断）
        for rl in review_ledger:
            mid = rl.get("match_id", "")
            goals = rl.get("total_goals_actual", 0)
            idx = rl.get("actual_idx", -1)
            if idx == 0:
                hs, aw = (goals, 0) if goals > 0 else (1, 0)
            elif idx == 1:
                half = max(1, goals // 2)
                hs, aw = (half, goals - half)
            elif idx == 2:
                hs, aw = (0, goals) if goals > 0 else (0, 1)
            else:
                hs, aw = (0, 0)
            entry = {"match_id": mid, "home_score": hs, "away_score": aw}
            results_map[mid] = entry
            fixture = _extract_fixture(mid)
            if fixture:
                results_map[fixture] = entry

    # 按联赛分组
    from collections import OrderedDict
    league_groups = OrderedDict()
    league_order = []
    for p in sorted(predictions, key=lambda x: -x.get("confidence", 0)):
        lg = p.get("competition", "其他")
        if lg not in league_groups:
            league_groups[lg] = []
            league_order.append(lg)
        league_groups[lg].append(p)

    # 联赛筛选导航
    if len(league_groups) > 1:
        cards += '<div class="league-nav">'
        cards += '<button class="league-btn active" data-league="all">全部</button>'
        for lg in league_order:
            cnt = len(league_groups[lg])
            cards += f'<button class="league-btn" data-league="{_slug(lg)}">{lg}<span class="cnt">{cnt}</span></button>'
        cards += '</div>'

    global_idx = 0
    for lg in league_order:
        lg_matches = league_groups[lg]
        cards += f'<div class="league-section" data-league="{_slug(lg)}">'
        cards += _league_header_enriched(lg, len(lg_matches), league_matrix)
        for p in lg_matches:
            cards += _match_card(p, value_matches, global_idx, results_map)
            global_idx += 1
        cards += '</div>' 

    # 三票方案
    ticket_html = _ticket_section(ticket, predictions)

    # 赛果复盘（优先用 results.json，fallback review_ledger）
    results_html = _results_section(results, results_preds or predictions, review_ledger)

    # 系统面板
    system_html = _system_panel(breaker, bundle, tier, breaker_mult, tier_reason)

    health_badge = '<span class="badge ok">系统正常</span>' if health.get("healthy") else '<span class="badge warn">降级</span>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>竞彩分析引擎 - {today}</title>
<style>
:root {{
  --bg: #0a0e13;
  --surface: #111820;
  --surface2: #1a2332;
  --surface3: #212d3d;
  --border: #263344;
  --border-light: #2f4258;
  --text: #e8edf4;
  --text-secondary: #94a8c0;
  --dim: #6b8299;
  --blue: #3b82f6;
  --blue-dim: #1e40af;
  --red: #ef4444;
  --red-dim: #7f1d1d;
  --green: #22c55e;
  --green-dim: #14532d;
  --amber: #f59e0b;
  --amber-dim: #78350f;
  --purple: #a855f7;
  --purple-dim: #581c87;
  --cyan: #06b6d4;
  --radius: 12px;
  --radius-sm: 8px;
  --shadow: 0 4px 24px rgba(0,0,0,0.4);
  --transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'SF Pro Text', 'Segoe UI', 'Inter', sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}
.page {{ max-width: 1140px; margin: 0 auto; padding: 28px 20px 48px; }}

/* ===== HEADER ===== */
.header {{
  display: flex; justify-content: space-between; align-items: flex-start;
  margin-bottom: 28px; padding-bottom: 20px; border-bottom: 1px solid var(--border);
  flex-wrap: wrap; gap: 14px;
}}
.header-left h1 {{
  font-size: 1.6rem; font-weight: 800; letter-spacing: -0.8px;
  background: linear-gradient(135deg, #e8edf4 0%, #94a8c0 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}}
.header-left .sub {{
  color: var(--dim); font-size: 0.78rem; margin-top: 5px;
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  letter-spacing: 0.3px;
}}
.header-right {{ display: flex; align-items: center; gap: 10px; }}
.date-nav {{
  display: flex; gap: 6px; padding: 10px 0; overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}}
.date-btn {{
  padding: 5px 14px; border-radius: 16px; font-size: 0.78rem; font-weight: 600;
  color: var(--dim); background: var(--card); border: 1px solid var(--border);
  text-decoration: none; white-space: nowrap; transition: all 0.2s;
}}
.date-btn.active {{ color: #fff; background: var(--blue); border-color: var(--blue); }}
.date-btn:hover {{ border-color: var(--blue); color: var(--blue); }}
.date-btn.active:hover {{ color: #fff; }}
.badge {{
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 12px; border-radius: 20px;
  font-size: 0.68rem; font-weight: 700; letter-spacing: 0.8px;
  text-transform: uppercase;
}}
.badge::before {{ content: ''; width: 6px; height: 6px; border-radius: 50%; }}
.badge.ok {{ background: var(--green-dim); color: var(--green); border: 1px solid #166534; }}
.badge.ok::before {{ background: var(--green); animation: pulse 2s infinite; }}
.badge.warn {{ background: var(--amber-dim); color: var(--amber); border: 1px solid #92400e; }}
.badge.warn::before {{ background: var(--amber); }}
@keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} }}

/* ===== KPI STATS BAR ===== */
.stats {{
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px;
  margin-bottom: 28px;
}}
.stat {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 14px 16px;
  transition: var(--transition); position: relative; overflow: hidden;
}}
.stat::after {{
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--blue), transparent);
  opacity: 0; transition: opacity 0.3s;
}}
.stat:hover {{ border-color: var(--border-light); transform: translateY(-1px); }}
.stat:hover::after {{ opacity: 1; }}
.stat .label {{
  font-size: 0.62rem; color: var(--dim); text-transform: uppercase;
  letter-spacing: 1px; font-weight: 600; margin-bottom: 4px;
}}
.stat .value {{ font-size: 1.35rem; font-weight: 800; letter-spacing: -0.5px; }}
.stat .value.green {{ color: var(--green); }}
.stat .value.amber {{ color: var(--amber); }}
.stat .value.red {{ color: var(--red); }}
.stat .value.blue {{ color: var(--blue); }}

/* ===== SECTION TITLES ===== */
.section-title {{
  font-size: 0.85rem; font-weight: 700; margin: 32px 0 14px;
  padding-left: 12px; border-left: 3px solid var(--blue);
  color: var(--text-secondary); text-transform: uppercase; letter-spacing: 1px;
}}

/* ===== LEAGUE NAV ===== */
.league-nav {{
  display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px;
}}
.league-btn {{
  padding: 5px 14px; border-radius: 16px; font-size: 0.72rem; font-weight: 600;
  color: var(--text-secondary); background: var(--surface2); border: 1px solid var(--border);
  cursor: pointer; transition: var(--transition); white-space: nowrap;
  font-family: inherit;
}}
.league-btn:hover {{ border-color: var(--blue); color: var(--blue); }}
.league-btn.active {{ background: var(--blue); border-color: var(--blue); color: #fff; }}
.league-btn .cnt {{
  font-size: 0.62rem; color: var(--dim); margin-left: 4px;
}}
.league-btn.active .cnt {{ color: rgba(255,255,255,0.7); }}

/* ===== LEAGUE SECTION ===== */
.league-section {{ margin-bottom: 8px; }}
.league-section.hidden {{ display: none; }}
.league-header {{
  font-size: 0.78rem; font-weight: 700; color: var(--cyan);
  padding: 8px 12px; margin: 14px 0 8px;
  background: rgba(6,182,212,0.06); border-left: 3px solid var(--cyan);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  display: flex; align-items: center; gap: 8px;
}}
.league-count {{
  font-size: 0.65rem; font-weight: 400; color: var(--dim);
}}


/* ===== LEAGUE MATRIX TABLE ===== */
.lm-wrap {{
  overflow-x: auto; -webkit-overflow-scrolling: touch;
  margin-bottom: 4px; border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}}
.lm-table {{
  width: 100%; border-collapse: collapse; font-size: 0.68rem;
  white-space: nowrap;
}}
.lm-table thead {{ position: sticky; top: 0; z-index: 2; }}
.lm-table th {{
  background: var(--surface2); color: var(--dim); font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.5px;
  padding: 8px 10px; text-align: center; border-bottom: 2px solid var(--border);
  font-size: 0.6rem;
}}
.lm-table th:first-child {{ text-align: left; padding-left: 12px; }}
.lm-table th:nth-child(2) {{ text-align: left; }}
.lm-table td {{
  padding: 6px 10px; text-align: center; border-bottom: 1px solid rgba(38,51,68,0.5);
  color: var(--text-secondary);
}}
.lm-table td:first-child {{ padding-left: 12px; }}
.lm-table td:nth-child(2) {{ text-align: left; font-weight: 600; color: var(--text); }}
.lm-row:hover td {{ background: rgba(59,130,246,0.08); }}
.lm-row.active td {{
  background: rgba(34,197,94,0.12);
  font-weight: 600;
}}
.lm-row.active td:first-child {{ border-left: 3px solid var(--green); }}
.lm-row.active td:nth-child(2) {{
  color: var(--green);
  font-weight: 700;
}}
.lm-row.active td:nth-child(2)::before {{
  content: '● ';
  font-size: 0.5rem;
  vertical-align: middle;
  animation: pulse-dot 1.5s ease-in-out infinite;
}}
@keyframes pulse-dot {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.3; }}
}}
.lm-name {{ font-weight: 600; }}
.lm-num {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.65rem; }}
.lm-pct {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.65rem; }}
.lm-cat {{
  display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 0.58rem; font-weight: 700; letter-spacing: 0.5px;
  text-transform: uppercase;
}}
.lm-cat-tier1 {{ background: rgba(34,197,94,0.12); color: var(--green); }}
.lm-cat-tier2 {{ background: rgba(245,158,11,0.12); color: var(--amber); }}
.lm-cat-world {{ background: rgba(59,130,246,0.12); color: var(--blue); }}
.lm-cat-other {{ background: rgba(148,168,192,0.1); color: var(--dim); }}
.lm-cat-cup {{ background: rgba(168,85,247,0.15); color: var(--purple); }}
#league-matrix.collapsed {{ display: none; }}

/* ===== LEAGUE STATS BAR ===== */
.lg-stats-bar {{
  display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px;
}}
.lg-stat {{
  font-size: 0.62rem; padding: 2px 8px; border-radius: 4px;
  font-weight: 600; white-space: nowrap;
  background: rgba(148,168,192,0.08); color: var(--text-secondary);
}}
.lg-stat.h {{ color: var(--blue); }}
.lg-stat.d {{ color: var(--dim); }}
.lg-stat.a {{ color: var(--red); }}

/* ===== MATCH CARDS ===== */
.match {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); margin-bottom: 10px;
  transition: var(--transition); overflow: hidden;
}}
.match:hover {{ border-color: var(--border-light); box-shadow: var(--shadow); }}
.match.value-pick {{ border-left: 3px solid var(--green); }}
.match-header {{
  padding: 16px 18px; cursor: pointer; user-select: none;
  display: flex; flex-direction: column; gap: 10px;
}}
.match-header:active {{ background: var(--surface2); }}
.match-top {{
  display: flex; justify-content: space-between; align-items: center;
}}
.league-tag {{
  font-size: 0.65rem; color: var(--cyan); background: rgba(6,182,212,0.1);
  padding: 2px 8px; border-radius: 4px; font-weight: 600;
  border: 1px solid rgba(6,182,212,0.2);
}}
.match-meta {{ display: flex; align-items: center; gap: 8px; }}
.match-id {{ font-size: 0.65rem; color: var(--dim); font-family: monospace; }}
.value-badge {{
  font-size: 0.6rem; font-weight: 800; color: var(--green);
  background: var(--green-dim); padding: 2px 7px; border-radius: 4px;
  letter-spacing: 0.5px; border: 1px solid #166534;
}}
.teams {{
  display: flex; justify-content: center; align-items: center; gap: 14px;
}}
.team {{ font-size: 1.08rem; font-weight: 700; min-width: 90px; }}
.team.home {{ text-align: right; }}
.team.away {{ text-align: left; }}
.vs {{ color: var(--dim); font-size: 0.7rem; font-weight: 600; }}

/* Prob bar */
.prob-row {{
  display: flex; height: 28px; border-radius: 6px; overflow: hidden;
  background: var(--surface3);
}}
.prob-seg {{
  display: flex; align-items: center; justify-content: center;
  font-size: 0.65rem; font-weight: 700; color: #fff;
  min-width: 38px; transition: width 0.5s ease;
  text-shadow: 0 1px 2px rgba(0,0,0,0.5);
}}
.prob-seg.h {{ background: linear-gradient(135deg, #2563eb, #3b82f6); }}
.prob-seg.d {{ background: linear-gradient(135deg, #4b5563, #6b7280); }}
.prob-seg.a {{ background: linear-gradient(135deg, #dc2626, #ef4444); }}

/* Prediction pick */
.pred-pick {{
  text-align: center; padding: 6px 0 2px; font-size: 0.82rem;
}}
.pick-label {{
  font-size: 0.65rem; color: var(--dim); font-weight: 600;
  text-transform: uppercase; letter-spacing: 1px; margin-right: 6px;
}}
.pick-val {{
  font-weight: 800; font-size: 0.88rem; padding: 2px 10px;
  border-radius: 6px;
}}
.pick-val.home {{ color: var(--blue); background: rgba(59,130,246,0.1); }}
.pick-val.draw {{ color: var(--text-secondary); background: rgba(107,114,128,0.15); }}
.pick-val.away {{ color: var(--red); background: rgba(239,68,68,0.1); }}
.pred-score {{
  font-size: 0.72rem; color: var(--amber); font-weight: 700;
  margin-left: 10px; padding: 2px 8px; border-radius: 4px;
  background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.2);
}}

/* Actual result comparison */
.actual-result {{
  text-align: center; padding: 4px 0 2px; font-size: 0.78rem;
  display: flex; align-items: center; justify-content: center; gap: 8px;
}}
.ar-label {{
  font-size: 0.62rem; color: var(--dim); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.5px;
}}
.ar-score {{
  font-weight: 800; font-size: 0.92rem; color: var(--text);
  font-family: 'SF Mono', 'Fira Code', monospace;
}}
.ar-outcome {{
  font-size: 0.72rem; font-weight: 700; padding: 1px 8px;
  border-radius: 4px;
}}
.ar-outcome.home {{ color: var(--blue); background: rgba(59,130,246,0.1); }}
.ar-outcome.draw {{ color: var(--text-secondary); background: rgba(107,114,128,0.15); }}
.ar-outcome.away {{ color: var(--red); background: rgba(239,68,68,0.1); }}
.ar-hit {{
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 50%;
  font-size: 0.72rem; font-weight: 800;
}}
.ar-hit.hit {{ background: var(--green-dim); color: var(--green); }}
.ar-hit.miss {{ background: var(--red-dim); color: var(--red); }}

/* Collapsed info row */
.match-info-row {{
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 6px;
}}
.conf-meter {{ display: inline-flex; align-items: center; gap: 5px; font-size: 0.72rem; }}
.conf-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.conf-dot.high {{ background: var(--green); box-shadow: 0 0 6px rgba(34,197,94,0.5); }}
.conf-dot.med {{ background: var(--amber); box-shadow: 0 0 6px rgba(245,158,11,0.4); }}
.conf-dot.low {{ background: var(--dim); }}
.info-chip {{
  font-size: 0.68rem; color: var(--dim);
  display: inline-flex; align-items: center; gap: 3px;
}}
.info-chip b {{ color: var(--text-secondary); font-weight: 600; }}
.expand-icon {{
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px; border-radius: 6px;
  font-size: 0.68rem; font-weight: 600; color: var(--blue);
  background: rgba(59,130,246,0.1); border: 1px solid rgba(59,130,246,0.3);
  cursor: pointer; transition: var(--transition); white-space: nowrap;
}}
.expand-icon:hover {{ background: rgba(59,130,246,0.2); border-color: var(--blue); }}
.match.expanded .expand-icon {{ transform: none; color: var(--dim); background: var(--surface2); border-color: var(--border); }}

/* ===== EXPANDED DETAIL PANEL ===== */
.match-detail {{
  display: none; border-top: 1px solid var(--border);
  background: var(--surface2); padding: 0;
}}
.match.expanded .match-detail {{ display: block; }}

/* Tabs */
.tab-bar {{
  display: flex; border-bottom: 1px solid var(--border);
  background: var(--surface);
}}
.tab-btn {{
  flex: 1; padding: 10px 8px; text-align: center;
  font-size: 0.72rem; font-weight: 700; color: var(--dim);
  cursor: pointer; border: none; background: none;
  border-bottom: 2px solid transparent; transition: var(--transition);
  text-transform: uppercase; letter-spacing: 0.5px;
}}
.tab-btn:hover {{ color: var(--text-secondary); background: var(--surface2); }}
.tab-btn.active {{ color: var(--blue); border-bottom-color: var(--blue); background: var(--surface2); }}
.tab-content {{ display: none; padding: 16px 18px; }}
.tab-content.active {{ display: block; }}

/* Model comparison table */
.model-table {{ width: 100%; border-collapse: collapse; font-size: 0.72rem; }}
.model-table th {{
  text-align: left; padding: 6px 8px; color: var(--dim);
  font-weight: 600; font-size: 0.65rem; text-transform: uppercase;
  letter-spacing: 0.5px; border-bottom: 1px solid var(--border);
}}
.model-table td {{
  padding: 7px 8px; border-bottom: 1px solid rgba(38,51,68,0.5);
  vertical-align: middle;
}}
.model-table tr:last-child td {{ border-bottom: none; }}
.model-table .src-label {{ color: var(--text-secondary); font-weight: 600; white-space: nowrap; }}
.prob-bar-cell {{ width: 55%; }}
.mini-bar-wrap {{ display: flex; height: 16px; border-radius: 4px; overflow: hidden; background: var(--surface3); }}
.mini-bar {{
  display: flex; align-items: center; justify-content: center;
  font-size: 0.58rem; font-weight: 700; color: #fff; min-width: 24px;
}}
.mini-bar.h {{ background: var(--blue); }}
.mini-bar.d {{ background: #4b5563; }}
.mini-bar.a {{ background: var(--red); }}

/* Elo & Wilson */
.elo-row {{
  display: flex; gap: 16px; margin-top: 12px; flex-wrap: wrap;
}}
.elo-chip {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 8px 14px;
  font-size: 0.72rem; flex: 1; min-width: 120px;
}}
.elo-chip .elo-label {{ color: var(--dim); font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.5px; }}
.elo-chip .elo-val {{ font-size: 1.1rem; font-weight: 800; margin-top: 2px; }}
.wilson-meter {{ margin-top: 12px; }}
.wilson-label {{ font-size: 0.68rem; color: var(--dim); margin-bottom: 5px; }}
.wilson-track {{
  height: 8px; background: var(--surface3); border-radius: 4px; overflow: hidden;
}}
.wilson-fill {{
  height: 100%; border-radius: 4px;
  background: linear-gradient(90deg, var(--amber), var(--green));
  transition: width 0.6s ease;
}}
.wilson-val {{ font-size: 0.68rem; color: var(--text-secondary); margin-top: 3px; }}

/* Odds tab */
.odds-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px; margin-bottom: 14px;
}}
.odds-box {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 10px 12px;
}}
.odds-box .ob-label {{ font-size: 0.62rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px; }}
.odds-box .ob-val {{ font-size: 1rem; font-weight: 700; margin-top: 2px; }}
.edge-table {{ width: 100%; border-collapse: collapse; font-size: 0.72rem; margin-top: 12px; }}
.edge-table th {{
  text-align: left; padding: 5px 8px; color: var(--dim);
  font-size: 0.62rem; text-transform: uppercase; border-bottom: 1px solid var(--border);
}}
.edge-table td {{ padding: 6px 8px; border-bottom: 1px solid rgba(38,51,68,0.4); }}
.edge-pos {{ color: var(--green); font-weight: 700; }}
.edge-neg {{ color: var(--red); font-weight: 600; }}
.reverse-box {{
  margin-top: 14px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 12px 14px;
}}
.reverse-box h5 {{ font-size: 0.7rem; color: var(--amber); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
.reverse-row {{ display: flex; justify-content: space-between; font-size: 0.72rem; padding: 3px 0; }}
.reverse-row .rk {{ color: var(--dim); }}
.same-odds-box {{
  margin-top: 10px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 12px 14px;
}}
.same-odds-box h5 {{ font-size: 0.7rem; color: var(--cyan); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}

/* Distribution tab */
.scores-grid {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
  gap: 6px; margin-bottom: 14px;
}}
.score-cell {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 4px; text-align: center;
}}
.score-cell .sc-score {{ font-size: 0.8rem; font-weight: 800; }}
.score-cell .sc-prob {{ font-size: 0.6rem; color: var(--dim); margin-top: 1px; }}
.goals-bars {{ margin: 14px 0; }}
.goal-bar-row {{
  display: flex; align-items: center; gap: 8px; margin-bottom: 5px;
}}
.goal-bar-label {{ font-size: 0.68rem; color: var(--dim); width: 50px; text-align: right; flex-shrink: 0; }}
.goal-bar-track {{ flex: 1; height: 18px; background: var(--surface3); border-radius: 4px; overflow: hidden; }}
.goal-bar-fill {{
  height: 100%; border-radius: 4px; display: flex; align-items: center;
  padding-left: 6px; font-size: 0.6rem; font-weight: 700; color: #fff;
  background: linear-gradient(90deg, var(--purple-dim), var(--purple));
  transition: width 0.5s ease;
}}
.xg-compare {{ margin-top: 14px; }}
.xg-compare h5 {{ font-size: 0.7rem; color: var(--text-secondary); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px; }}
.xg-bar-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }}
.xg-bar-label {{ font-size: 0.68rem; color: var(--dim); width: 60px; text-align: right; flex-shrink: 0; }}
.xg-bar-track {{ flex: 1; height: 22px; background: var(--surface3); border-radius: 5px; overflow: hidden; position: relative; }}
.xg-bar-fill {{
  height: 100%; border-radius: 5px; display: flex; align-items: center;
  padding-left: 8px; font-size: 0.65rem; font-weight: 700; color: #fff;
  transition: width 0.5s ease;
}}
.xg-bar-fill.home {{ background: linear-gradient(90deg, var(--blue-dim), var(--blue)); }}
.xg-bar-fill.away {{ background: linear-gradient(90deg, var(--red-dim), var(--red)); }}

/* ===== TICKET SECTION ===== */
.ticket-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
.ticket-card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; transition: var(--transition);
}}
.ticket-card:hover {{ border-color: var(--border-light); }}
.ticket-card h4 {{
  font-size: 0.75rem; margin-bottom: 10px; text-transform: uppercase;
  letter-spacing: 0.8px; font-weight: 800;
}}
.ticket-card h4.stable {{ color: var(--green); }}
.ticket-card h4.value {{ color: var(--blue); }}
.ticket-card h4.lottery {{ color: var(--purple); }}
.ticket-item {{
  display: flex; justify-content: space-between; align-items: center;
  font-size: 0.72rem; padding: 6px 0;
  border-bottom: 1px solid rgba(38,51,68,0.5);
}}
.ticket-item:last-child {{ border-bottom: none; }}
.ticket-item .ti-match {{ color: var(--text-secondary); }}
.ticket-item .ti-odds {{ color: var(--text); font-weight: 600; font-family: monospace; }}
.ticket-empty {{ font-size: 0.72rem; color: var(--dim); font-style: italic; padding: 8px 0; }}
.ticket-summary {{
  margin-top: 12px; display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px;
}}
.ts-chip {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 10px 12px; text-align: center;
}}
.ts-chip .ts-label {{ font-size: 0.6rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px; }}
.ts-chip .ts-val {{ font-size: 1rem; font-weight: 800; margin-top: 2px; }}

/* ===== SYSTEM PANEL ===== */
.sys-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
.sys-card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; transition: var(--transition);
}}
.sys-card:hover {{ border-color: var(--border-light); }}
.sys-card h4 {{
  font-size: 0.68rem; color: var(--dim); margin-bottom: 10px;
  text-transform: uppercase; letter-spacing: 1px; font-weight: 700;
}}
.sys-row {{
  display: flex; justify-content: space-between; align-items: center;
  font-size: 0.72rem; padding: 4px 0;
}}
.sys-row .k {{ color: var(--dim); }}
.sys-row .v {{ font-weight: 600; }}
.hash {{
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  font-size: 0.62rem; color: var(--purple); word-break: break-all;
  margin-top: 8px; padding: 8px; background: var(--surface2);
  border-radius: 6px; border: 1px solid var(--border);
}}
.tier-indicator {{
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: 4px; font-size: 0.68rem; font-weight: 700;
}}
.tier-indicator.safe {{ background: var(--green-dim); color: var(--green); }}
.tier-indicator.caution {{ background: var(--amber-dim); color: var(--amber); }}
.tier-indicator.danger {{ background: var(--red-dim); color: var(--red); }}

/* ===== RESULTS REVIEW ===== */
.results-summary {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px; margin-bottom: 14px;
}}
.results-table-wrap {{ overflow-x: auto; }}
.results-table {{
  width: 100%; border-collapse: collapse; font-size: 0.72rem;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); overflow: hidden;
}}
.results-table th {{
  text-align: left; padding: 8px 10px; color: var(--dim);
  font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border); background: var(--surface2);
}}
.results-table td {{
  padding: 8px 10px; border-bottom: 1px solid rgba(38,51,68,0.4);
  vertical-align: middle;
}}
.results-table tr:last-child td {{ border-bottom: none; }}
.results-table tr.hit td {{ background: rgba(34,197,94,0.04); }}
.results-table tr.miss td {{ background: rgba(239,68,68,0.04); }}
.result-icon {{
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 50%; font-size: 0.7rem; font-weight: 800;
}}
.result-icon.hit {{ background: var(--green-dim); color: var(--green); }}
.result-icon.miss {{ background: var(--red-dim); color: var(--red); }}

/* ===== FOOTER ===== */
.footer {{
  margin-top: 40px; padding-top: 18px; border-top: 1px solid var(--border);
  font-size: 0.7rem; color: var(--dim); line-height: 1.8;
}}
.footer .chain {{
  font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.65rem;
  color: var(--text-secondary); background: var(--surface);
  padding: 10px 14px; border-radius: var(--radius-sm);
  border: 1px solid var(--border); margin-bottom: 10px;
  overflow-x: auto; white-space: nowrap;
}}
.footer a {{ color: var(--blue); text-decoration: none; }}
.footer a:hover {{ text-decoration: underline; }}
.footer .disclaimer {{ margin-top: 8px; color: var(--dim); font-style: italic; }}

/* ===== RESPONSIVE ===== */
@media (max-width: 900px) {{
  .stats {{ grid-template-columns: repeat(3, 1fr); }}
  .ticket-grid {{ grid-template-columns: 1fr; }}
  .sys-grid {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 600px) {{
  .page {{ padding: 16px 12px 36px; }}
  .stats {{ grid-template-columns: repeat(2, 1fr); }}
  .teams {{ gap: 8px; }}
  .team {{ font-size: 0.92rem; min-width: 65px; }}
  .header-left h1 {{ font-size: 1.3rem; }}
  .odds-grid {{ grid-template-columns: 1fr 1fr; }}
  .scores-grid {{ grid-template-columns: repeat(auto-fill, minmax(60px, 1fr)); }}
  .match-header {{ padding: 12px 14px; }}
  .tab-content {{ padding: 12px 14px; }}
}}
</style>
</head>
<body>
<div class="page">
  <!-- HEADER -->
  <div class="header">
    <div class="header-left">
      <h1>竞彩分析引擎</h1>
      <div class="sub">{today} &middot; DC+MC &rarr; Shin去水 &rarr; 逆向赔率 &rarr; 四源融合 &rarr; LGBM &rarr; Isotonic校准 &rarr; Wilson信任</div>
    </div>
    <div class="header-right">
      {health_badge}
    </div>
  </div>

  <!-- 日期导航 -->
  <div class="date-nav">
    {''.join(f'<a href="{d}.html" class="date-btn {"active" if d == today else ""}">{d[5:]}</a>' for d in (all_dates or [today]))}
  </div>

  <!-- KPI STATS -->
  <div class="stats">
    <div class="stat"><div class="label">场次</div><div class="value">{total}</div></div>
    <div class="stat"><div class="label">价值注</div><div class="value green">{len(value_bets)}</div></div>
    <div class="stat"><div class="label">平均置信</div><div class="value blue">{avg_conf:.0%}</div></div>
    <div class="stat"><div class="label">总投入</div><div class="value amber">&yen;{total_stake:.0f}</div></div>
    <div class="stat"><div class="label">预期回报</div><div class="value {'green' if exp_roi > 1 else 'red'}">{exp_roi:.2f}x</div></div>
    <div class="stat"><div class="label">熔断器</div><div class="value {'green' if tier == 0 else 'red'}">T{tier} &middot; x{breaker_mult:.1f}</div></div>
  </div>

  <!-- LEAGUE MATRIX -->
  {league_matrix_html}

  <!-- MATCH PREDICTIONS -->
  <div class="section-title">比赛预测</div>
  {cards if cards else '<p style="color:var(--dim);padding:48px;text-align:center;font-size:0.85rem;">等待每日流水线运行...</p>'}

  <!-- BETTING PLAN -->
  {ticket_html}

  <!-- RESULTS REVIEW -->
  {results_html}

  <!-- SYSTEM STATUS -->
  {system_html}

  <!-- FOOTER -->
  <div class="footer">
    <div class="chain">DC(60%) + MC-50K(40%) &rarr; Shin去水 &rarr; 逆向赔率 &rarr; 同赔历史 &rarr; 融合(模型60% + 市场25% + DJYY15%) &rarr; LGBM(10%) &rarr; Isotonic校准 &rarr; Wilson信任</div>
    <p>数据源: 体彩 / 新浪 / 500万 / DJYY &middot; 零服务器 GitHub Actions &middot; <a href="https://github.com/wlrwx/football-engine">源代码</a></p>
    <p class="disclaimer">仅供研究学习，不构成任何投注建议。模型输出为概率估计，不保证准确性。</p>
  </div>
</div>

<script>
// ===== MATCH CARD EXPAND/COLLAPSE =====
document.querySelectorAll('.match-header').forEach(function(header) {{
  header.addEventListener('click', function() {{
    var card = this.closest('.match');
    card.classList.toggle('expanded');
  }});
}});

// ===== TAB SWITCHING =====
document.querySelectorAll('.tab-btn').forEach(function(btn) {{
  btn.addEventListener('click', function(e) {{
    e.stopPropagation();
    var panel = this.closest('.match-detail');
    var tabId = this.getAttribute('data-tab');
    // Deactivate all tabs in this panel
    panel.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    panel.querySelectorAll('.tab-content').forEach(function(c) {{ c.classList.remove('active'); }});
    // Activate clicked
    this.classList.add('active');
    panel.querySelector('#' + tabId).classList.add('active');
  }});
}});

// ===== LEAGUE FILTER =====
document.querySelectorAll('.league-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    var league = this.getAttribute('data-league');
    // Update active button
    document.querySelectorAll('.league-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    this.classList.add('active');
    // Show/hide sections
    document.querySelectorAll('.league-section').forEach(function(sec) {{
      if (league === 'all' || sec.getAttribute('data-league') === league) {{
        sec.classList.remove('hidden');
      }} else {{
        sec.classList.add('hidden');
      }}
    }});
  }});
}});
</script>
</body>
</html>"""


def _pred_pick(p):
    """生成明确的预测结论"""
    ph = p.get("home_win_prob") or 0
    pd = p.get("draw_prob") or 0
    pa = p.get("away_win_prob") or 0
    if ph >= pd and ph >= pa:
        return f'<span class="pick-label">预测</span> <span class="pick-val home">主胜 {ph:.0%}</span>'
    elif pd >= ph and pd >= pa:
        return f'<span class="pick-label">预测</span> <span class="pick-val draw">平局 {pd:.0%}</span>'
    else:
        return f'<span class="pick-label">预测</span> <span class="pick-val away">客胜 {pa:.0%}</span>'


def _pred_score(p):
    """预测比分（前3个最可能比分）"""
    top_scores = p.get("top_scores")
    if not top_scores or not isinstance(top_scores, list) or len(top_scores) == 0:
        return ""
    scores = []
    for item in top_scores[:3]:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            scores.append(f"{item[0]}-{item[1]}")
    if scores:
        return f' <span class="pred-score">比分 {" / ".join(scores)}</span>'
    return ""


def _load_league_matrix(path):
    """加载 DJYY 联赛矩阵数据"""
    import json
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {"leagues": []}


def _league_matrix_section(league_matrix, predictions):
    """联赛矩阵面板：展示联赛统计数据，高亮当天有预测的联赛"""
    if not league_matrix or not league_matrix.get("leagues"):
        return ""

    leagues = league_matrix["leagues"]
    predicted_leagues = set()
    # 联赛名称映射（竞彩名称 → DJYY 矩阵名称）
    name_map = {
        "K1联赛": "韩K联", "韩K联": "韩K联",
        "巴甲": "巴西甲", "巴西甲": "巴西甲",
        "K联赛": "韩K联",
    }
    for p in predictions:
        comp = p.get("competition", "")
        if comp:
            predicted_leagues.add(comp)
            mapped = name_map.get(comp, comp)
            if mapped != comp:
                predicted_leagues.add(mapped)

    sorted_leagues = sorted(leagues, key=lambda x: -x.get("avg_goals", 0))

    rows = ""
    for lg in sorted_leagues:
        name = lg["name_zh"]
        is_active = name in predicted_leagues
        row_cls = "active" if is_active else ""
        cat = lg.get("category", "other")
        cat_label = {"tier1": "顶级", "tier2": "次级", "world": "全球", "other": "其他"}.get(cat, cat)
        cat_cls = cat

        rows += '<tr class="lm-row ' + row_cls + '">'
        rows += '<td><span class="lm-cat lm-cat-' + cat_cls + '">' + cat_label + '</span></td>'
        rows += '<td class="lm-name">' + name + '</td>'
        rows += '<td class="lm-num">' + str(lg.get("matches", 0)) + '</td>'
        rows += '<td class="lm-num">' + f'{lg.get("avg_goals", 0):.1f}' + '</td>'
        rows += '<td class="lm-num">' + f'{lg.get("avg_xg", 0):.2f}' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("btts_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("home_win_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("draw_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("away_win_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("clean_sheet_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-num">' + f'{lg.get("avg_corners", 0):.1f}' + '</td>'
        rows += '<td class="lm-num">' + f'{lg.get("avg_yellow", 0):.1f}' + '</td>'
        rows += '</tr>'

    # 补充 DJYY 矩阵中缺失的联赛（杯赛/国际赛事等）
    matrix_names = {lg["name_zh"] for lg in sorted_leagues}
    missing_comps = set()
    for p in predictions:
        comp = p.get("competition", "")
        if comp and comp not in matrix_names and name_map.get(comp, comp) not in matrix_names:
            missing_comps.add(comp)

    if missing_comps:
        for comp in sorted(missing_comps):
            rows += '<tr class="lm-row active">'
            rows += '<td><span class="lm-cat lm-cat-cup">杯赛</span></td>'
            rows += '<td class="lm-name">' + comp + '</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '</tr>'

    gen_time = league_matrix.get("generated_at", "")[:10]
    active_count = sum(1 for lg in sorted_leagues if lg["name_zh"] in predicted_leagues)
    active_count += len(missing_comps)

    # 今日赛事提醒横幅
    alert_html = ""
    if active_count > 0:
        alert_html = (
            '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;margin:0 0 2px;'
            'background:linear-gradient(135deg,rgba(34,197,94,0.12),rgba(6,182,212,0.08));'
            'border:1px solid rgba(34,197,94,0.25);border-radius:var(--radius-sm);font-size:0.78rem">'
            '<span style="display:inline-flex;align-items:center;gap:4px;font-weight:800;color:var(--green)">'
            '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);'
            'animation:pulse 2s infinite"></span>'
            '今日赛事</span>'
            '<span style="color:var(--text-secondary)">' + str(active_count) + ' 个联赛有比赛</span>'
            '<span style="font-size:0.62rem;color:var(--dim);margin-left:auto">数据源: DJYY 每日更新</span>'
            '</div>'
        )

    return (
        '<div class="section-title" onclick="document.getElementById(\'league-matrix\').classList.toggle(\'collapsed\')" style="cursor:pointer">'
        + '联赛矩阵 &middot; ' + str(len(leagues) + len(missing_comps)) + ' 联赛/杯赛 &middot; ' + gen_time
        + ' <span style="font-size:0.65rem;color:var(--dim)">&#9660; 点击折叠</span></div>'
        + alert_html
        + '<div id="league-matrix"><div class="lm-wrap"><table class="lm-table">'
        + '<thead><tr><th>级别</th><th>联赛</th><th>场次</th><th>场均进球</th><th>场均xG</th>'
        + '<th>BTTS</th><th>主胜</th><th>平局</th><th>客胜</th><th>零封</th>'
        + '<th>角球</th><th>黄牌</th></tr></thead>'
        + '<tbody>' + rows + '</tbody></table></div>'
        + '<div style="padding:8px 12px;font-size:0.62rem;color:var(--dim);display:flex;gap:12px;flex-wrap:wrap">'
        + '<span>数据来源: <a href="https://djyylive.com" style="color:var(--blue)">DJYY</a></span>'
        + '<span>&#x25cf; <span style="color:var(--green)">高亮行</span> = 当天有预测的联赛</span>'
        + '<span>BTTS = 双方进球率</span>'
        + '<span>⚡ 数据更新于 DJYY 每日流水线</span></div></div>'
    )


def _league_header_enriched(lg_name, count, league_matrix):
    """生成带联赛统计的增强版联赛头部"""
    if not league_matrix or not league_matrix.get("leagues"):
        return '<div class="league-header">' + lg_name + ' <span class="league-count">' + str(count) + ' 场</span></div>'

    lg_data = None
    name_map = {"K1联赛": "韩K联", "巴甲": "巴西甲", "K联赛": "韩K联"}
    search_name = name_map.get(lg_name, lg_name)
    for lg in league_matrix["leagues"]:
        if lg["name_zh"] == search_name or lg["name_zh"] == lg_name or lg.get("short_zh", "") == lg_name:
            lg_data = lg
            break

    if not lg_data:
        return '<div class="league-header">' + lg_name + ' <span class="league-count">' + str(count) + ' 场</span></div>'

    goals = lg_data.get("avg_goals", 0)
    btts = lg_data.get("btts_pct", 0)
    home = lg_data.get("home_win_pct", 0)
    draw = lg_data.get("draw_pct", 0)
    away = lg_data.get("away_win_pct", 0)
    xg = lg_data.get("avg_xg", 0)
    matches = lg_data.get("matches", 0)

    return (
        '<div class="league-header">'
        + '<span>' + lg_name + '</span>'
        + '<span class="league-count">' + str(count) + ' 场 &middot; 赛季 ' + str(matches) + '场</span>'
        + '<div class="lg-stats-bar">'
        + '<span class="lg-stat" title="场均进球">&#x26BD; ' + f'{goals:.1f}' + '</span>'
        + '<span class="lg-stat" title="场均xG">xG ' + f'{xg:.2f}' + '</span>'
        + '<span class="lg-stat" title="双方进球率">BTTS ' + f'{btts:.0f}%' + '</span>'
        + '<span class="lg-stat h" title="主胜率">主 ' + f'{home:.0f}%' + '</span>'
        + '<span class="lg-stat d" title="平局率">平 ' + f'{draw:.0f}%' + '</span>'
        + '<span class="lg-stat a" title="客胜率">客 ' + f'{away:.0f}%' + '</span>'
        + '</div></div>'
    )



def _match_card(p, value_matches, idx, results_map=None):
    """Render a single match card with expandable detail tabs."""
    hp = p.get("home_win_prob", 0) * 100
    dp = p.get("draw_prob", 0) * 100
    ap = p.get("away_win_prob", 0) * 100
    conf = p.get("confidence", 0)
    conf_cls = "high" if conf > 0.6 else "med" if conf > 0.4 else "low"
    is_val = _is_value(p, value_matches)
    match_id = p.get("match_id", "")
    uid = f"m{idx}"

    # Basic info
    odds_h = p.get("home_odds") or 0
    odds_d = p.get("draw_odds") or 0
    odds_a = p.get("away_odds") or 0
    xg_h = p.get("home_xg", 0)
    xg_a = p.get("away_xg", 0)

    # 实际赛果对比
    result_html = ""
    if results_map:
        r = results_map.get(match_id)
        if not r:
            # 用场次号匹配（跨日期）
            fixture = _extract_fixture(match_id)
            if fixture:
                r = results_map.get(fixture)
            if not r:
                # fallback: 旧格式匹配
                fixture2 = match_id.split("_", 1)[-1] if "_" in match_id else match_id
                r = results_map.get(fixture2)
            if not r:
                # fallback: 队名匹配（最可靠）
                home_team = p.get("home_team", "")
                away_team = p.get("away_team", "")
                if home_team and away_team:
                    team_key = f"{home_team}_vs_{away_team}"
                    r = results_map.get(team_key)
        if r and r.get("home_score") is not None:
            hs, as_ = r["home_score"], r["away_score"]
            if hs > as_:
                actual_label, actual_cls = "主胜", "home"
            elif hs == as_:
                actual_label, actual_cls = "平局", "draw"
            else:
                actual_label, actual_cls = "客胜", "away"
            # 判断命中
            ph = p.get("home_win_prob") or 0
            pd_ = p.get("draw_prob") or 0
            pa = p.get("away_win_prob") or 0
            if ph >= pd_ and ph >= pa:
                pred_outcome = "home"
            elif pd_ >= ph and pd_ >= pa:
                pred_outcome = "draw"
            else:
                pred_outcome = "away"
            hit = pred_outcome == ("home" if hs > as_ else "draw" if hs == as_ else "away")
            hit_icon = "✓" if hit else "✗"
            hit_cls = "hit" if hit else "miss"
            # 比分命中检查
            score_hit_html = ""
            top_scores = p.get("top_scores") or []
            for item in top_scores:
                if isinstance(item, (list, tuple)) and len(item) >= 2 and int(item[0]) == hs and int(item[1]) == as_:
                    score_hit_html = ' <span class="ar-hit hit" title="比分命中">比✓</span>'
                    break
            result_html = f'<div class="actual-result"><span class="ar-label">实际</span> <span class="ar-score">{hs}-{as_}</span> <span class="ar-outcome {actual_cls}">{actual_label}</span> <span class="ar-hit {hit_cls}">{hit_icon}</span>{score_hit_html}</div>'

    # Build detail tabs
    model_tab = _tab_model(p, uid)
    odds_tab = _tab_odds(p, uid)
    dist_tab = _tab_distribution(p, uid)

    return f"""
  <div class="match {'value-pick' if is_val else ''}">
    <div class="match-header">
      <div class="match-top">
        <span class="league-tag">{p.get('competition', '')}</span>
        <div class="match-meta">
          {'<span class="value-badge">价值精选</span>' if is_val else ''}
          <span class="match-id">{match_id.split('_', 1)[-1] if '_' in match_id else match_id}</span>
          <span class="expand-icon">详情 &#9660;</span>
        </div>
      </div>
      <div class="teams">
        <span class="team home">{p.get('home_team', '')}</span>
        <span class="vs">VS</span>
        <span class="team away">{p.get('away_team', '')}</span>
      </div>
      <div class="prob-row">
        <div class="prob-seg h" style="width:{hp:.1f}%">H {hp:.0f}%</div>
        <div class="prob-seg d" style="width:{dp:.1f}%">D {dp:.0f}%</div>
        <div class="prob-seg a" style="width:{ap:.1f}%">A {ap:.0f}%</div>
      </div>
      <div class="pred-pick">{_pred_pick(p)}{_pred_score(p)}</div>
      {result_html}
      <div class="match-info-row">
        <span class="conf-meter"><span class="conf-dot {conf_cls}"></span><b>{conf:.0%}</b></span>
        <span class="info-chip">xG <b>{xg_h:.2f} - {xg_a:.2f}</b></span>
        <span class="info-chip">Odds <b>{odds_h}/{odds_d}/{odds_a}</b></span>
      </div>
    </div>
    <div class="match-detail">
      <div class="tab-bar">
        <button class="tab-btn active" data-tab="{uid}-model">模型</button>
        <button class="tab-btn" data-tab="{uid}-odds">赔率</button>
        <button class="tab-btn" data-tab="{uid}-dist">分布</button>
      </div>
      <div class="tab-content active" id="{uid}-model">{model_tab}</div>
      <div class="tab-content" id="{uid}-odds">{odds_tab}</div>
      <div class="tab-content" id="{uid}-dist">{dist_tab}</div>
    </div>
  </div>"""


def _tab_model(p, uid):
    """模型 tab: model_raw vs market_fair vs djyy vs final, Elo, Wilson, + DJYY xG/ injuries."""
    model_raw = p.get("model_raw") or {}
    market_fair = p.get("market_fair")
    djyy = p.get("djyy_model_prob")
    final_h = p.get("home_win_prob") or 0
    final_d = p.get("draw_prob") or 0
    final_a = p.get("away_win_prob") or 0
    elo_home = p.get("elo_home")
    elo_away = p.get("elo_away")
    wilson = p.get("wilson_trust") or 0
    
    # ===== DJYY 增强数据 =====
    # xG 预期进球
    xg_home = 0
    xg_away = 0
    xg_html = ""
    djyy_xg = p.get("djyy_xg") or {}
    if djyy_xg:
        xg_home = float(djyy_xg.get("home_avg") or 0)
        xg_away = float(djyy_xg.get("away_avg") or 0)
        max_xg = max(2.0, xg_home, xg_away)
        h_pct = (xg_home / max_xg) * 100 if max_xg > 0 else 0
        a_pct = (xg_away / max_xg) * 100 if max_xg > 0 else 0
        xg_html = f"""
      <div class="xg-compare">
        <h5>xG 预期进球 (近5场平均)</h5>
        <div class="xg-bar-row">
          <span class="xg-bar-label">主队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill home" style="width:{h_pct}%">{xg_home:.2f}</div></div>
        </div>
        <div class="xg-bar-row">
          <span class="xg-bar-label">客队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill away" style="width:{a_pct}%">{xg_away:.2f}</div></div>
        </div>
      </div>"""
    
    # 赛程密度/休息天数
    rest_html = ""
    rest_days = p.get("rest_days") or {}
    if rest_days:
        rh = rest_days.get("home") or 7
        ra = rest_days.get("away") or 7
        # 颜色: <=2天=红色(疲劳), 3-4天=橙色, >=5天=绿色(充足)
        h_color = "var(--red)" if rh <= 2 else "var(--amber)" if rh <= 4 else "var(--green)"
        a_color = "var(--red)" if ra <= 2 else "var(--amber)" if ra <= 4 else "var(--green)"
        rest_html = f"""
      <div class="xg-compare">
        <h5>赛程密度 (距上场天数)</h5>
        <div class="xg-bar-row">
          <span class="xg-bar-label">主队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill" style="width:{min(rh/7*100, 100)}%; background:{h_color}">{rh}天</div></div>
        </div>
        <div class="xg-bar-row">
          <span class="xg-bar-label">客队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill" style="width:{min(ra/7*100, 100)}%; background:{a_color}">{ra}天</div></div>
        </div>
      </div>"""
    
    # 伤停预警
    inj_html = ""
    injuries = p.get("injuries") or {}
    if injuries:
        h_cnt = injuries.get("home_count", 0)
        h_att = injuries.get("home_attackers", 0)
        a_cnt = injuries.get("away_count", 0)
        a_att = injuries.get("away_attackers", 0)
        # 前锋/中场缺阵 = 高亮预警
        h_warn = "⚠️" if h_att >= 1 else ""
        a_warn = "⚠️" if a_att >= 1 else ""
        inj_html = f"""
      <div class="reverse-box">
        <h5>伤停预警</h5>
        <div class="reverse-row"><span class="rk">主队</span><span>伤停{h_cnt}人{h_warn} (含攻击线{h_att}人)</span></div>
        <div class="reverse-row"><span class="rk">客队</span><span>伤停{a_cnt}人{a_warn} (含攻击线{a_att}人)</span></div>
      </div>"""

    rows = ""
    # Model Raw (DC+MC)
    mr_h = model_raw.get("home", 0) * 100 if model_raw else 0
    mr_d = model_raw.get("draw", 0) * 100 if model_raw else 0
    mr_a = model_raw.get("away", 0) * 100 if model_raw else 0
    rows += _model_row("DC+MC 原始", mr_h, mr_d, mr_a)

    # Market Fair (Shin)
    if market_fair and len(market_fair) >= 3:
        mf_h = market_fair[0] * 100
        mf_d = market_fair[1] * 100
        mf_a = market_fair[2] * 100
        rows += _model_row("Shin公平", mf_h, mf_d, mf_a)
    else:
        rows += _model_row_empty("Shin公平")

    # DJYY
    if djyy and djyy.get("home"):
        dj_h = djyy.get("home", 0) * 100
        dj_d = djyy.get("draw", 0) * 100
        dj_a = djyy.get("away", 0) * 100
        rows += _model_row("DJYY模型", dj_h, dj_d, dj_a)
    else:
        rows += _model_row_empty("DJYY模型")

    # Final Fused
    rows += _model_row("最终融合", final_h * 100, final_d * 100, final_a * 100)

    # Elo section
    elo_html = ""
    if elo_home is not None and elo_away is not None:
        elo_diff = (elo_home or 0) - (elo_away or 0)
        elo_html = f"""
      <div class="elo-row">
        <div class="elo-chip"><div class="elo-label">主队Elo</div><div class="elo-val" style="color:var(--blue)">{elo_home:.0f}</div></div>
        <div class="elo-chip"><div class="elo-label">客队Elo</div><div class="elo-val" style="color:var(--red)">{elo_away:.0f}</div></div>
        <div class="elo-chip"><div class="elo-label">Elo差值</div><div class="elo-val" style="color:{'var(--green)' if elo_diff > 0 else 'var(--red)'}">{elo_diff:+.0f}</div></div>
      </div>"""

    # Wilson trust
    wilson_pct = (wilson or 0) * 100
    wilson_html = f"""
      <div class="wilson-meter">
        <div class="wilson-label">Wilson信任分</div>
        <div class="wilson-track"><div class="wilson-fill" style="width:{wilson_pct:.0f}%"></div></div>
        <div class="wilson-val">{wilson_pct:.1f}% 置信权重</div>
      </div>"""

    return f"""
      <table class="model-table">
        <tr><th>信号源</th><th class="prob-bar-cell">主 / 平 / 客 概率分布</th></tr>
        {rows}
      </table>
      {xg_html}
      {rest_html}
      {inj_html}
      {elo_html}
      {wilson_html}"""


def _model_row(label, h, d, a):
    return f"""
        <tr>
          <td class="src-label">{label}</td>
          <td class="prob-bar-cell">
            <div class="mini-bar-wrap">
              <div class="mini-bar h" style="width:{h:.1f}%">{h:.0f}</div>
              <div class="mini-bar d" style="width:{d:.1f}%">{d:.0f}</div>
              <div class="mini-bar a" style="width:{a:.1f}%">{a:.0f}</div>
            </div>
          </td>
        </tr>"""


def _model_row_empty(label):
    return f"""
        <tr>
          <td class="src-label">{label}</td>
          <td class="prob-bar-cell"><span style="font-size:0.65rem;color:var(--dim);">暂无</span></td>
        </tr>"""


def _tab_odds(p, uid):
    """赔率 tab: market odds, shin fair, implied probs, edge, reverse, same-odds, + 四庄家对比."""
    odds_h = p.get("home_odds") or 0
    odds_d = p.get("draw_odds") or 0
    odds_a = p.get("away_odds") or 0
    handicap = p.get("handicap", "")
    market_fair = p.get("market_fair")
    final_h = p.get("home_win_prob", 0)
    final_d = p.get("draw_prob", 0)
    final_a = p.get("away_win_prob", 0)
    
    # 四庄家赔率对比
    # 1. 体彩官方
    sporttery_odds = p.get("sporttery_odds") or {}
    st_h = sporttery_odds.get("home") or odds_h
    st_d = sporttery_odds.get("draw") or odds_d
    st_a = sporttery_odds.get("away") or odds_a
    
    # 2. Bet365
    bet365_odds = p.get("bet365_odds") or {}
    b365_h = bet365_odds.get("home") or odds_h
    b365_d = bet365_odds.get("draw") or odds_d
    b365_a = bet365_odds.get("away") or odds_a
    
    # 3. Pinnacle
    pinn_odds = p.get("pinnacle_odds") or {}
    pinn_h = pinn_odds.get("home") or odds_h
    pinn_d = pinn_odds.get("draw") or odds_d
    pinn_a = pinn_odds.get("away") or odds_a
    
    # 4. DJYY 模型赔率（反向计算）
    djyy_odds = p.get("djyy_model_prob") or {}
    djyy_h = 1 / djyy_odds.get("home") if djyy_odds.get("home") and djyy_odds.get("home") > 0 else None
    djyy_d = 1 / djyy_odds.get("draw") if djyy_odds.get("draw") and djyy_odds.get("draw") > 0 else None
    djyy_a = 1 / djyy_odds.get("away") if djyy_odds.get("away") and djyy_odds.get("away") > 0 else None
    
    # 赔率对比表格
    def _o(v):
        return f"{v:.2f}" if v and v > 0 else "-"
    
    bookies_html = f"""
      <table class="edge-table" style="margin-top: 16px;">
        <tr><th>庄家</th><th>主胜</th><th>平局</th><th>客胜</th></tr>
        <tr><td class="src-label">体彩官方</td><td>{_o(st_h)}</td><td>{_o(st_d)}</td><td>{_o(st_a)}</td></tr>
        <tr><td class="src-label">Bet365</td><td>{_o(b365_h)}</td><td>{_o(b365_d)}</td><td>{_o(b365_a)}</td></tr>
        <tr><td class="src-label">Pinnacle</td><td>{_o(pinn_h)}</td><td>{_o(pinn_d)}</td><td>{_o(pinn_a)}</td></tr>
        <tr><td class="src-label" style="color:var(--purple);">DJYY模型</td><td>{_o(djyy_h)}</td><td>{_o(djyy_d)}</td><td>{_o(djyy_a)}</td></tr>
      </table>"""

    # Implied probs from raw odds
    imp_h = (1 / odds_h * 100) if odds_h else 0
    imp_d = (1 / odds_d * 100) if odds_d else 0
    imp_a = (1 / odds_a * 100) if odds_a else 0

    # Shin fair probs
    sf_h = market_fair[0] * 100 if market_fair and len(market_fair) >= 3 else 0
    sf_d = market_fair[1] * 100 if market_fair and len(market_fair) >= 3 else 0
    sf_a = market_fair[2] * 100 if market_fair and len(market_fair) >= 3 else 0

    # Edge = model - implied
    edge_h = final_h * 100 - imp_h
    edge_d = final_d * 100 - imp_d
    edge_a = final_a * 100 - imp_a

    def _edge_cls(v):
        return "edge-pos" if v > 2 else "edge-neg" if v < -2 else ""

    # Reverse odds analysis
    upset = p.get("reverse_upset_risk") or 0
    direction = p.get("reverse_direction") or ""
    compression = p.get("reverse_compression") or 0

    # Same odds
    same_matched = p.get("same_odds_matched") or 0
    same_conf = p.get("same_odds_confidence") or 0
    same_bias = p.get("same_odds_bias") or ""
    combo_boost = p.get("combo_boost") or 0

    return f"""
      <div class="odds-grid">
        <div class="odds-box"><div class="ob-label">主胜</div><div class="ob-val">{odds_h:.2f}</div></div>
        <div class="odds-box"><div class="ob-label">平局</div><div class="ob-val">{odds_d:.2f}</div></div>
        <div class="odds-box"><div class="ob-label">客胜</div><div class="ob-val">{odds_a:.2f}</div></div>
        <div class="odds-box"><div class="ob-label">让球</div><div class="ob-val">{handicap if handicap else '暂无'}</div></div>
      </div>
      <h5 style="margin: 16px 0 8px 0; color: var(--text-secondary); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.5px;">四庄家赔率对比</h5>
      {bookies_html}
      <table class="edge-table">
        <tr><th>结果</th><th>隐含概率</th><th>Shin公平</th><th>模型</th><th>边际</th></tr>
        <tr>
          <td>主胜</td><td>{imp_h:.1f}%</td><td>{sf_h:.1f}%</td><td>{final_h*100:.1f}%</td>
          <td class="{_edge_cls(edge_h)}">{edge_h:+.1f}%</td>
        </tr>
        <tr>
          <td>平局</td><td>{imp_d:.1f}%</td><td>{sf_d:.1f}%</td><td>{final_d*100:.1f}%</td>
          <td class="{_edge_cls(edge_d)}">{edge_d:+.1f}%</td>
        </tr>
        <tr>
          <td>客胜</td><td>{imp_a:.1f}%</td><td>{sf_a:.1f}%</td><td>{final_a*100:.1f}%</td>
          <td class="{_edge_cls(edge_a)}">{edge_a:+.1f}%</td>
        </tr>
      </table>
      <div class="reverse-box">
        <h5>逆向赔率分析</h5>
        <div class="reverse-row"><span class="rk">冷门风险</span><span style="color:{'var(--red)' if upset > 40 else 'var(--text)'}; font-weight:700;">{upset:.0f}%</span></div>
        <div class="reverse-row"><span class="rk">方向</span><span>{direction if direction else '暂无'}</span></div>
        <div class="reverse-row"><span class="rk">压缩比</span><span>{compression:.2f}</span></div>
      </div>
      <div class="same-odds-box">
        <h5>同赔历史</h5>
        <div class="reverse-row"><span class="rk">匹配场次</span><span style="font-weight:700;">{same_matched}</span></div>
        <div class="reverse-row"><span class="rk">历史置信</span><span>{same_conf:.0%}</span></div>
        <div class="reverse-row"><span class="rk">偏差</span><span>{same_bias if same_bias else '中性'}</span></div>
        <div class="reverse-row"><span class="rk">组合加成</span><span style="color:{'var(--green)' if combo_boost > 0 else 'var(--dim)'}">{combo_boost:+.2f}</span></div>
      </div>"""


def _tab_distribution(p, uid):
    """分布 tab: top_scores grid, total_goals bars, xG comparison."""
    top_scores = p.get("top_scores")
    total_goals = p.get("total_goals")
    xg_h = p.get("home_xg", 0)
    xg_a = p.get("away_xg", 0)

    # Top scores grid
    scores_html = ""
    if top_scores and isinstance(top_scores, list) and len(top_scores) > 0:
        cells = ""
        for item in top_scores[:12]:
            if isinstance(item, dict):
                score = item.get("score", "")
                prob = item.get("prob", 0)
                cells += f'<div class="score-cell"><div class="sc-score">{score}</div><div class="sc-prob">{prob*100:.1f}%</div></div>'
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                cells += f'<div class="score-cell"><div class="sc-score">{item[0]}-{item[1]}</div><div class="sc-prob">{item[2]*100:.1f}%</div></div>'
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                cells += f'<div class="score-cell"><div class="sc-score">{item[0]}</div><div class="sc-prob">{item[1]*100:.1f}%</div></div>'
        if cells:
            scores_html = f'<div style="margin-bottom:6px;font-size:0.68rem;color:var(--dim);text-transform:uppercase;letter-spacing:0.5px;">最可能比分</div><div class="scores-grid">{cells}</div>'
    else:
        scores_html = '<div style="font-size:0.72rem;color:var(--dim);font-style:italic;margin-bottom:12px;">比分分布暂无数据</div>'

    # Total goals bars
    goals_html = ""
    # 兼容 dict 格式（旧数据：{'1.5': [under_prob, over_prob]})
    if isinstance(total_goals, dict):
        total_goals = [[int(float(k)), v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else (v if isinstance(v, (int, float)) else 0)]
                       for k, v in total_goals.items()]
    if total_goals and isinstance(total_goals, list) and len(total_goals) > 0:
        max_prob = max((item.get("prob", 0) if isinstance(item, dict) else (item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else 0) for item in total_goals), default=1) or 1
        bars = ""
        for item in total_goals:
            if isinstance(item, dict):
                label = item.get("goals", item.get("label", ""))
                prob = item.get("prob", 0)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                label = item[0]
                prob = item[1]
            else:
                continue
            pct = (prob / max_prob) * 100
            bars += f"""
          <div class="goal-bar-row">
            <span class="goal-bar-label">{label}</span>
            <div class="goal-bar-track"><div class="goal-bar-fill" style="width:{pct:.0f}%">{prob*100:.1f}%</div></div>
          </div>"""
        goals_html = f'<div style="margin:14px 0 6px;font-size:0.68rem;color:var(--dim);text-transform:uppercase;letter-spacing:0.5px;">总进球分布</div><div class="goals-bars">{bars}</div>'
    else:
        goals_html = '<div style="font-size:0.72rem;color:var(--dim);font-style:italic;margin:12px 0;">总进球分布暂无数据</div>'

    # xG comparison
    max_xg = max(xg_h, xg_a, 0.1)
    xg_h_pct = (xg_h / (max_xg * 1.3)) * 100
    xg_a_pct = (xg_a / (max_xg * 1.3)) * 100
    xg_html = f"""
      <div class="xg-compare">
        <h5>预期进球 (xG)</h5>
        <div class="xg-bar-row">
          <span class="xg-bar-label">{p.get('home_team', 'Home')[:8]}</span>
          <div class="xg-bar-track"><div class="xg-bar-fill home" style="width:{xg_h_pct:.0f}%">{xg_h:.2f}</div></div>
        </div>
        <div class="xg-bar-row">
          <span class="xg-bar-label">{p.get('away_team', 'Away')[:8]}</span>
          <div class="xg-bar-track"><div class="xg-bar-fill away" style="width:{xg_a_pct:.0f}%">{xg_a:.2f}</div></div>
        </div>
      </div>"""

    return f"{scores_html}{goals_html}{xg_html}"


def _ticket_section(ticket, predictions):
    if not ticket:
        return ""
    stable = ticket.get("stable", [])
    value = ticket.get("value", [])
    lottery = ticket.get("lottery", [])
    total_stake = ticket.get("total_stake", 0)
    exp_roi = ticket.get("expected_roi", 0)
    bankroll = ticket.get("bankroll", 0)
    breaker_mult = ticket.get("breaker_multiplier", 1.0)

    def _items(items):
        if not items:
            return '<div class="ticket-empty">暂无选择</div>'
        html = ""
        for it in items:
            match_id = it.get("match", "")
            teams = match_id.split("_", 1)[-1] if "_" in match_id else match_id
            for p in predictions:
                if p.get("match_id") == match_id:
                    teams = f'{p["home_team"]} vs {p["away_team"]}'
                    break
            sel_map = {"home": "主胜", "draw": "平局", "away": "客胜"}
            sel = sel_map.get(it.get("sel", ""), it.get("sel", ""))
            html += f'<div class="ticket-item"><span class="ti-match">{teams} [{sel}]</span><span class="ti-odds">@{it.get("odds", 0):.2f} / &yen;{it.get("stake", 0):.0f}</span></div>'
        return html

    return f"""
  <div class="section-title">投注方案（三票制 60/30/10）</div>
  <div class="ticket-grid">
    <div class="ticket-card"><h4 class="stable">稳胆（60%）</h4>{_items(stable)}</div>
    <div class="ticket-card"><h4 class="value">搏冷（30%）</h4>{_items(value)}</div>
    <div class="ticket-card"><h4 class="lottery">彩票（10%）</h4>{_items(lottery)}</div>
  </div>
  <div class="ticket-summary">
    <div class="ts-chip"><div class="ts-label">总投入</div><div class="ts-val" style="color:var(--amber)">&yen;{total_stake:.0f}</div></div>
    <div class="ts-chip"><div class="ts-label">预期回报</div><div class="ts-val" style="color:{'var(--green)' if exp_roi > 1 else 'var(--red)'}">{exp_roi:.2f}x</div></div>
    <div class="ts-chip"><div class="ts-label">资金池</div><div class="ts-val">&yen;{bankroll:.0f}</div></div>
    <div class="ts-chip"><div class="ts-label">熔断系数</div><div class="ts-val" style="color:{'var(--green)' if breaker_mult >= 1 else 'var(--red)'}">x{breaker_mult:.2f}</div></div>
  </div>"""


def _results_section(results, predictions, review_ledger=None):
    """赛果复盘: 预测 vs 实际结果对比（优先 results.json，fallback review_ledger）"""
    if not results and not review_ledger:
        return ""

    # 建立 match_id → prediction 多层索引（精确 + 场次号 + 队名）
    pred_map = {p.get("match_id", ""): p for p in predictions}
    pred_fixture_map = {}
    for p in predictions:
        mid = p.get("match_id", "")
        fixture = _extract_fixture(mid)
        if fixture:
            pred_fixture_map[fixture] = p
        # 队名索引（最可靠，跨数据源通用）
        hm = p.get("home_team", "")
        aw = p.get("away_team", "")
        if hm and aw:
            pred_map[f"{hm}_vs_{aw}"] = p

    # 如果没有 results.json，从 review_ledger 构建结果
    if not results and review_ledger:
        results = []
        for rl in review_ledger:
            goals = rl.get("total_goals_actual", 0)
            idx = rl.get("actual_idx", -1)
            if idx == 0:
                hs, as_ = (goals, 0) if goals > 0 else (1, 0)
            elif idx == 1:
                half = max(1, goals // 2)
                hs, as_ = (half, goals - half)
            elif idx == 2:
                hs, as_ = (0, goals) if goals > 0 else (0, 1)
            else:
                hs, as_ = (0, 0)
            results.append({
                "match_id": rl.get("match_id", ""),
                "home_score": hs,
                "away_score": as_,
                "pnl": rl.get("pnl", 0),
            })

    rows = ""
    hits = 0
    total_brier = 0.0
    total_pnl = 0.0
    matched = 0

    for r in results:
        mid = r.get("match_id", "")
        home_score = r.get("home_score")
        away_score = r.get("away_score")
        if home_score is None or away_score is None:
            continue

        pred = pred_map.get(mid)
        if not pred:
            # 模糊匹配：用场次号
            pred = pred_fixture_map.get(_extract_fixture(mid))
        if not pred:
            # 队名匹配（最可靠）
            hm = r.get("home_team", "")
            aw = r.get("away_team", "")
            if hm and aw:
                pred = pred_map.get(f"{hm}_vs_{aw}")
        if not pred:
            continue

        matched += 1
        # 实际结果
        if home_score > away_score:
            actual = "home"
            actual_label = "主胜"
        elif home_score == away_score:
            actual = "draw"
            actual_label = "平局"
        else:
            actual = "away"
            actual_label = "客胜"

        # 预测结果
        ph = pred.get("home_win_prob", 0)
        pd = pred.get("draw_prob", 0)
        pa = pred.get("away_win_prob", 0)
        if ph >= pd and ph >= pa:
            predicted = "home"
            pred_label = "主胜"
        elif pd >= ph and pd >= pa:
            predicted = "draw"
            pred_label = "平局"
        else:
            predicted = "away"
            pred_label = "客胜"

        hit = predicted == actual
        if hit:
            hits += 1

        # Brier score: sum of (prob - indicator)^2
        ind_h = 1.0 if actual == "home" else 0.0
        ind_d = 1.0 if actual == "draw" else 0.0
        ind_a = 1.0 if actual == "away" else 0.0
        brier = (ph - ind_h)**2 + (pd - ind_d)**2 + (pa - ind_a)**2
        total_brier += brier

        # 投注盈亏（如果在三票方案中）
        pnl = r.get("pnl", 0)
        total_pnl += pnl

        hit_cls = "hit" if hit else "miss"
        hit_icon = "✓" if hit else "✗"
        pnl_color = "var(--green)" if pnl > 0 else "var(--red)" if pnl < 0 else "var(--dim)"

        rows += f"""
        <tr class="{hit_cls}">
          <td>{pred.get('home_team', '')} vs {pred.get('away_team', '')}</td>
          <td style="font-weight:800;text-align:center;">{home_score}-{away_score}</td>
          <td>{actual_label}</td>
          <td>{pred_label} ({max(ph, pd, pa):.0%})</td>
          <td style="text-align:center;"><span class="result-icon {hit_cls}">{hit_icon}</span></td>
          <td style="font-family:monospace;font-size:0.68rem;">{brier:.3f}</td>
          <td style="color:{pnl_color};font-weight:600;">{'+' if pnl > 0 else ''}{pnl:.0f}</td>
        </tr>"""

    if matched == 0:
        return ""

    hit_rate = hits / matched
    avg_brier = total_brier / matched

    return f"""
  <div class="section-title">赛果复盘</div>
  <div class="results-summary">
    <div class="ts-chip"><div class="ts-label">命中率</div><div class="ts-val" style="color:{'var(--green)' if hit_rate >= 0.5 else 'var(--red)'}">{hit_rate:.0%} ({hits}/{matched})</div></div>
    <div class="ts-chip"><div class="ts-label">平均Brier</div><div class="ts-val" style="color:{'var(--green)' if avg_brier < 0.5 else 'var(--amber)'}">{avg_brier:.3f}</div></div>
    <div class="ts-chip"><div class="ts-label">总盈亏</div><div class="ts-val" style="color:{'var(--green)' if total_pnl >= 0 else 'var(--red)'}">&yen;{total_pnl:+.0f}</div></div>
  </div>
  <div class="results-table-wrap">
    <table class="results-table">
      <tr><th>比赛</th><th>比分</th><th>实际</th><th>预测</th><th>命中</th><th>Brier</th><th>盈亏</th></tr>
      {rows}
    </table>
  </div>"""


def _system_panel(breaker, bundle, tier, mult, tier_reason=""):
    streak = breaker.get("current_streak", 0)
    wins = breaker.get("total_wins", 0)
    losses = breaker.get("total_losses", 0)
    wr = wins / max(1, wins + losses)
    daily_pnl = breaker.get("daily_pnl", 0)
    weekly_pnl = breaker.get("weekly_pnl", 0)
    halted = breaker.get("halted", False)
    sha = bundle.get("bundle_sha256", "暂无")
    created = bundle.get("created_at", "")

    tier_cls = "safe" if tier <= 1 else "caution" if tier <= 2 else "danger"
    tier_label = f"T{tier}" + (" · " + tier_reason if tier_reason else "")

    return f"""
  <div class="section-title">系统状态</div>
  <div class="sys-grid">
    <div class="sys-card">
      <h4>熔断器</h4>
      <div class="sys-row"><span class="k">状态</span><span class="tier-indicator {tier_cls}">{tier_label} &middot; x{mult:.2f}</span></div>
      <div class="sys-row"><span class="k">连续</span><span class="v" style="color:{'var(--green)' if streak >= 0 else 'var(--red)'}">{streak:+d}</span></div>
      <div class="sys-row"><span class="k">胜率</span><span class="v">{wr:.1%} ({wins}胜 / {losses}负)</span></div>
      <div class="sys-row"><span class="k">日盈亏</span><span class="v" style="color:{'var(--green)' if daily_pnl >= 0 else 'var(--red)'}">&yen;{daily_pnl:+.0f}</span></div>
      <div class="sys-row"><span class="k">周盈亏</span><span class="v" style="color:{'var(--green)' if weekly_pnl >= 0 else 'var(--red)'}">&yen;{weekly_pnl:+.0f}</span></div>
    </div>
    <div class="sys-card">
      <h4>决策完整性</h4>
      <div class="sys-row"><span class="k">创建时间</span><span class="v">{created[:19] if created else '暂无'}</span></div>
      <div class="sys-row"><span class="k">版本</span><span class="v">{bundle.get('version', 'v1')}</span></div>
      <div class="sys-row"><span class="k">算法</span><span class="v">SHA-256</span></div>
      <div class="hash">{sha}</div>
    </div>
    <div class="sys-card">
      <h4>模型配置</h4>
      <div class="sys-row"><span class="k">集成</span><span class="v">DC 60% + MC 40%</span></div>
      <div class="sys-row"><span class="k">融合</span><span class="v">模型60 / 市场25 / DJYY15</span></div>
      <div class="sys-row"><span class="k">元学习器</span><span class="v">LGBM (10%)</span></div>
      <div class="sys-row"><span class="k">校准</span><span class="v">Isotonic</span></div>
      <div class="sys-row"><span class="k">MC模拟</span><span class="v">50,000次</span></div>
      <div class="sys-row"><span class="k">信任区间</span><span class="v">Wilson</span></div>
    </div>
  </div>"""


def _is_value(p, value_matches=None):
    """判断是否为价值投注: 仅当被三票方案选中（稳胆/搏冷）"""
    if value_matches and p.get("match_id") in value_matches:
        return True
    return False


def _breaker_tier(breaker):
    """返回熔断级别，区分真实 tier 和 halted 原因"""
    streak = abs(min(breaker.get("current_streak", 0), 0))
    actual_tier = max(0, breaker.get("tier", 0))
    if streak >= 15:
        return 4, "连败≥15 停注"
    if streak >= 12:
        return 3, ""
    if streak >= 6:
        return 2, ""
    if streak >= 3:
        return 1, ""
    if breaker.get("halted"):
        # 非连败触发的停注（日/周止损），显示实际tier
        return actual_tier, "周/日止损停注"
    return actual_tier, ""


if __name__ == "__main__":
    build_site()
