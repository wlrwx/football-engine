from __future__ import annotations
"""静态报告生成器 - 专业体育分析仪表盘

展示: 预测概率/赔率对比/价值检测/xG/置信度/冷门风险/三票方案/熔断状态/决策链完整性
交互式: 点击展开比赛详情, 多Tab分析面板, 响应式布局
"""
import os
import json
import sys
from datetime import datetime
from html import escape as _esc
from pathlib import Path

ROOT = Path(__file__).parent.parent
# 以脚本方式运行时 sys.path[0] 是 engine/ 目录，补上仓库根，保证 import engine.* 可用
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.beijing_time import beijing_now, beijing_today


def _pipeline_desc():
    """流水线一句话描述——header/footer/系统面板共用同一数据源（fusion_weights 实时值），
    杜绝"页首页脚各说各话"（旧版 header 写 LGBM/Isotonic、footer 写死 模型10%+市场60%）。"""
    w = "模型? / 市场? / DJYY?"
    try:
        _fw = json.loads((ROOT / "data" / "state" / "fusion_weights.json").read_text(encoding="utf-8"))
        _ch = _fw.get("champion", {})
        if _ch:
            w = f"模型{_ch.get('model', 0)*100:.0f}% / 市场{_ch.get('market', 0)*100:.0f}% / DJYY{_ch.get('djyy', 0)*100:.0f}%"
    except Exception:
        pass
    return f"DC+MC &rarr; Shin去水 &rarr; 四源融合({w}) &rarr; 后处理链(每步可开关)"


def _health_badge():
    """诚实徽章：由 selfcheck_report 的 critical 告警驱动；无文件显示"未监控"，
    不再永远显示"系统正常"（旧版读一个没人写的 health-status.json）。"""
    try:
        _sc = json.loads((ROOT / "data" / "state" / "selfcheck_report.json").read_text(encoding="utf-8"))
        _alerts = _sc.get("alerts", [])
        _severe = [a for a in _alerts if any(k in a for k in ("全缺", "crs_odds", "ttg_odds", "hafu_odds"))]
        if _severe:
            return f'<span class="badge warn" title="{"; ".join(_severe[:2])}">数据告警 ×{len(_severe)}</span>'
        if _alerts:
            return f'<span class="badge ok" title="{"; ".join(_alerts[:2])}">正常（{len(_alerts)}条提示）</span>'
        return '<span class="badge ok">正常</span>'
    except Exception:
        return '<span class="badge" style="background:var(--surface3);color:var(--dim)">未监控</span>'


def _evo_state():
    """加载四个进化状态文件（upgrade_tracker / edge_calibration / calibration_status / lgbm_status）"""
    out = {}
    for key, rel in (("tracker", "data/state/upgrade_tracker.json"),
                     ("edge", "data/state/edge_calibration.json"),
                     ("calib", "data/state/calibration_status.json"),
                     ("lgbm", "data/state/lgbm_status.json")):
        try:
            _p = ROOT / rel
            out[key] = json.loads(_p.read_text(encoding="utf-8")) if _p.exists() else None
        except Exception:
            out[key] = None
    return out


def build_site():
    """生成静态 HTML 报告（多日期）"""
    web_dir = ROOT / "web"
    web_dir.mkdir(parents=True, exist_ok=True)

    daily_root = ROOT / "data" / "daily"
    today = beijing_today()

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

    # 缓存 league_matrix 到本地（从 DJYY 获取）。加 TTL：超过 48 小时强制刷新，
    # 避免页面长期显示旧数据（曾出现 8/9 的数据挂在 8/14 页面上）。
    league_matrix_path = ROOT / "data" / "league_matrix.json"
    _need_refresh = not league_matrix_path.exists()
    if not _need_refresh:
        try:
            from datetime import datetime as _dtm
            _age_hours = (_dtm.now() - _dtm.fromtimestamp(league_matrix_path.stat().st_mtime)).total_seconds() / 3600
            if _age_hours > 48:
                _need_refresh = True
        except Exception:
            pass
    if _need_refresh:
        try:
            import urllib.request
            # 空串（CI 未配 secret 时展开为空）必须当未配置，否则拼出
            # "/data/league-matrix.json" 这种无 scheme 的 URL → ValueError，
            # 被 except Exception 吞掉，联赛矩阵静默不刷新。
            # 口径同 engine/sources/djyy.py:_resolve_djyy_base。
            _djyy_base = (os.environ.get("DJYY_PROXY") or "").strip()
            if not _djyy_base or "://" not in _djyy_base:
                _djyy_base = "https://djyylive.com"
            req = urllib.request.Request(
                _djyy_base + "/data/league-matrix.json",
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
        # 如果当日 results.json 为空，用全局索引匹配（仅对"已过去"的日期）。
        # 未来/当天未结算的日期绝不 fallback，否则会拿历史赛果伪造"实际比分"
        # （2026-08-14 页面事故根因）。用北京时间判断"过去"。
        if not results and predictions:
            try:
                if target_date < beijing_today():
                    results = _match_results_to_predictions(predictions, all_results)
            except Exception:
                pass
        review_ledger = _load_ledger(ROOT / "data" / "state" / "review_ledger.jsonl", target_date)
        results_html_preds = predictions

        html = _render_html(target_date, predictions, bundle, ticket, breaker, health, results, results_html_preds, all_dates, review_ledger)

        # 最新日期写index.html, 所有日期写dated页面
        if target_date == all_dates[0]:
            (web_dir / "index.html").write_text(html, encoding="utf-8")
        (web_dir / f"{target_date}.html").write_text(html, encoding="utf-8")

    # 全局清理旧版决策包（所有日期保留最新3个，历史日期也清理）
    try:
        from engine.integrity.decision_bundle import DecisionBundle
        _pruned = DecisionBundle.prune_all_dates(daily_root, keep=3)
        if _pruned:
            print(f"[build_site] 🧹 已清理 {_pruned} 个旧决策包版本")
    except Exception as _e:
        print(f"[build_site] ⚠ 决策包清理跳过: {_e}")

    status = {
        "date": all_dates[0] if all_dates else today,
        "generated_at": beijing_now().isoformat(),
        "prediction_count": len(_load_json(daily_root / all_dates[0] / "predictions.json", [])) if all_dates else 0,
        "available_dates": all_dates,
        "healthy": True,
    }
    (web_dir / "report-status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    # EV 价值区报告（全量复盘分层 ROI，供页面与 Kelly 参考）
    try:
        from engine.review.ev_report import build_report
        build_report(daily_root, ROOT / "data" / "state" / "ev_report.json")
    except Exception as e:
        print(f"[build_site] ⚠ EV 报告生成跳过: {e}")
    # 比分/总进球经验先验（2026-08-14：校准模型比分矩阵的低比分偏差，
    # 供 multi_play_ev / score_parlay 的波胆/总进球 EV 使用）
    try:
        from engine.learning.score_calibration import build_score_prior
        _sp = build_score_prior(daily_root, ROOT / "data" / "state" / "score_prior.json")
        print(f"[build_site] 📊 比分经验先验已重建: {_sp.get('n_matches', 0)} 场")
    except Exception as e:
        print(f"[build_site] ⚠ 比分经验先验重建跳过: {e}")
    # 让球玩法回测报告（验证让球 EV 历史表现，从有让球赔率的场次积累）
    try:
        from engine.strategy.handicap_ev import build_handicap_report
        build_handicap_report(daily_root, ROOT / "data" / "state" / "handicap_report.json")
    except Exception as e:
        print(f"[build_site] ⚠ 让球回测报告生成跳过: {e}")
    # 多玩法回测报告（总进球/波胆/半全场 ROI 积累）
    try:
        from engine.strategy.multi_play_ev import build_plays_report
        build_plays_report(daily_root, ROOT / "data" / "state" / "plays_report.json")
    except Exception as e:
        print(f"[build_site] ⚠ 多玩法回测报告生成跳过: {e}")
    # 联赛分层价值报告（送钱区禁投依据）
    league_report = {}
    try:
        from engine.review.league_report import build_league_report
        # 老系统联赛复盘样本（世界杯积累的另一预测域不能搬参数，但同域联赛样本可合并）
        _legacy = []
        _legacy_path = ROOT / "data" / "state" / "legacy_league_samples.json"
        if _legacy_path.exists():
            _legacy = json.loads(_legacy_path.read_text(encoding="utf-8"))
        league_report = build_league_report(daily_root, ROOT / "data" / "state" / "league_report.json", _legacy)
        if _legacy:
            print(f"[build_site] 🧬 联赛分层已合并 {len(_legacy)} 场老系统历史复盘")
    except Exception as e:
        print(f"[build_site] ⚠ 联赛分层报告生成跳过: {e}")
    # 串关回测报告（2串1 能不能玩的实证）
    parlay_report = {}
    try:
        from engine.review.parlay_report import build_parlay_report
        parlay_report = build_parlay_report(daily_root, ROOT / "data" / "state" / "parlay_report.json")
    except Exception as e:
        print(f"[build_site] ⚠ 串关回测报告生成跳过: {e}")
    # 串关/波胆真实复盘（2026-08-10 新增：真实出票的结算，非回测）
    try:
        from engine.review.settle_parlays import build_settle_report
        build_settle_report(daily_root, ROOT / "data" / "state" / "parlay_settle.json")
    except Exception as e:
        print(f"[build_site] ⚠ 串关真实复盘生成跳过: {e}")
    # 准确率趋势报告（回答"是否每天在提升"）
    try:
        from engine.review.accuracy_trend import build_accuracy_trend
        trend_report = build_accuracy_trend(ROOT / "data" / "state" / "review_ledger.jsonl", ROOT / "data" / "state" / "accuracy_trend.json")
    except Exception as e:
        print(f"[build_site] ⚠ 准确率趋势报告生成跳过: {e}")
        trend_report = {}
    # 每日简报（最新日期）
    try:
        if all_dates:
            _latest = all_dates[0]
            _lp = _load_json(daily_root / _latest / "predictions.json", [])
            _tp = _load_json(daily_root / _latest / "ticket_plan.json", {})
            _rv = _load_json(daily_root / _latest / "review.json", None)
            _brief = _build_daily_brief(web_dir, _latest, _lp, _tp, _rv, league_report, parlay_report, trend_report)
            print(f"[build_site] 📋 每日简报已生成: {_brief.name}")
    except Exception as e:
        print(f"[build_site] ⚠ 每日简报生成跳过: {e}")
    print(f"[build_site] 仪表盘已生成: {len(all_dates)} 个日期页面")

    # 公开 Track Record 页（2026-08-12 P2：历史表现透明可查，随构建自动更新）
    try:
        _tr = _render_track_record(ROOT / "data" / "state", web_dir)
        print(f"[build_site] 📈 Track Record 页已生成: {_tr}")
    except Exception as e:
        print(f"[build_site] ⚠ Track Record 生成跳过: {e}")


def _league_layers_section(state_dir: Path, fallback_rows: str = "") -> str:
    """联赛分层表格（送钱区禁投 · 回暖自动解禁 · 判平强度自适应）"""
    try:
        _lrep_path = state_dir / "league_report.json"
        _lrep = _load_json(_lrep_path, {})
        _rows = _lrep.get("leagues", [])[:15]
        _lp = _load_json(state_dir / "league_params.json", {})
        if _rows:
            def _draw_cell(league):
                p = _lp.get(league, {})
                dp = p.get("draw_predictions", 0)
                dh = p.get("draw_hits", 0)
                if dp == 0:
                    return '<td style="color:var(--dim)">—</td>'
                prec = dh / dp
                st = 0.85 if (dp >= 4 and dh >= 3) else (0.35 if dp >= 2 else 0.40)
                _c = "var(--green)" if prec >= 0.5 else ("var(--amber)" if prec > 0 else "var(--red)")
                return f'<td style="color:{_c}">{dh}/{dp} ({prec*100:.0f}%)<br><span style="font-size:0.62rem;color:var(--dim)">强度 {st:.2f}</span></td>'

            _rows_html = "".join(
                f'<tr><td>{r["league"]}</td><td>{r["n"]}</td>'
                f'<td>{r["hit_rate"]*100:.0f}%</td><td>{r["avg_odds"]:.2f}</td>'
                f'<td style="color:{"var(--green)" if r["roi"] > 0 else "var(--red)"};font-weight:700">{r["roi"]*100:+.1f}%</td>'
                f'{_verdict_cell(r)}'
                f'{_recent_cell(r)}'
                f'{_recent10_cell(r)}'
                f'{_draw_cell(r["league"])}</tr>'
                for r in _rows if r.get("n", 0) >= 3
            )
            if _rows_html:
                return f'''
  <details class="deep-fold" open>
    <summary>
      <span class="deep-fold-title">联赛分层（送钱区禁投 · 回暖自动解禁 · 判平强度自适应）</span>
      <span class="deep-fold-meta">三窗口对照：全部累计 · 近10场 · 近5场</span>
      <span class="deep-fold-hint">点击折叠 ▾</span>
    </summary>
    <div class="deep-fold-body" style="overflow-x:auto">
      <table class="edge-table">
        <tr><th>联赛</th><th>场数</th><th>命中率</th><th>均赔</th><th>ROI</th><th>判断</th><th>近5场</th><th>近10场</th><th>判平(命中/次数·强度)</th></tr>
        {_rows_html}
      </table>
      <div class="note">回暖解禁 = 累计口径送钱区，但近5场命中≥60% <b>且</b> 近10场≥50% → 解除禁投观察；再拉胯自动打回送钱区。</div>
    </div>
  </details>'''
    except Exception:
        pass
    if fallback_rows:
        return f'''
  <details class="deep-fold" open>
    <summary>
      <span class="deep-fold-title">联赛表现（样本≥5）</span>
      <span class="deep-fold-meta">按联赛分桶的命中率与平局率</span>
      <span class="deep-fold-hint">点击折叠 ▾</span>
    </summary>
    <div class="deep-fold-body" style="overflow-x:auto"><table>
      <tr><th>联赛</th><th>场次</th><th>命中率</th><th>实际平局率</th></tr>
      {fallback_rows}
    </table></div>
  </details>'''
    return ""


def _hcr_section(state_dir: Path) -> str:
    """高置信反向样本库（独立归档复核）"""
    _hcr_path = state_dir / "high_conf_reversals.jsonl"
    if not _hcr_path.exists():
        return ""
    _hcr_items = []
    try:
        for _line in _hcr_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line:
                continue
            try:
                _hcr_items.append(json.loads(_line))
            except Exception:
                continue
    except Exception:
        return ""
    if not _hcr_items:
        return ""
    _hcr_rows = "".join(
        f'<tr><td>{_s.get("date","")}</td><td>{_s.get("league","")}</td>'
        f'<td>{_s.get("teams","")}</td>'
        f'<td>预测{_s.get("direction","")} <b>{_s.get("conf",0)*100:.0f}%</b></td>'
        f'<td style="color:var(--red)">实际{_s.get("actual_dir","")}</td></tr>'
        for _s in _hcr_items[-10:][::-1]
    )
    return f'''
  <details class="deep-fold">
    <summary>
      <span class="deep-fold-title">高置信反向样本库（{len(_hcr_items)} 场 · 预测≥60% + 市场同向却翻车）</span>
      <span class="deep-fold-meta">最近 {min(10, len(_hcr_items))} 条反向案例归档</span>
      <span class="deep-fold-hint">点击展开 ▾</span>
    </summary>
    <div class="deep-fold-body" style="overflow-x:auto">
      <table class="edge-table">
        <tr><th>日期</th><th>联赛</th><th>对阵</th><th>预测</th><th>实际</th></tr>
        {_hcr_rows}
      </table>
      <div class="note">独立归档复核（借鉴 MBS AIK 案例），不归因于模型-市场分歧；与 50-60% 段降档互补：60%+ 段虽整体命中最好，但反向样本单独跟踪。</div>
    </div>
  </details>'''


def _accuracy_trend_section(at: dict) -> str:
    """准确率趋势（近10日滚动表现 vs 前期）"""
    _daily = (at or {}).get("daily", [])[-10:]
    if not _daily:
        return ""
    _v = at.get("verdict", "样本不足")
    _v_color = "var(--green)" if "提升" in _v else ("var(--red)" if "下降" in _v else "var(--amber)")
    _rows_html = "".join(
        f'<tr><td>{d["date"]}</td><td>{d["n"]}</td><td>{d["hits"]}/{d["n"]}</td>'
        f'<td style="font-weight:700">{d["hit_rate"]*100:.0f}%</td>'
        f'<td>{d["brier_final"]:.2f}</td><td>{d["cum_hit_rate"]*100:.0f}%</td>'
        f'<td style="color:{"var(--green)" if d["pnl"] > 0 else ("var(--red)" if d["pnl"] < 0 else "var(--dim)")}">{d["pnl"]:+.0f}</td></tr>'
        for d in reversed(_daily)
    )
    return f'''
  <div class="section-title">准确率趋势（最近7天 vs 前7天: <span style="color:{_v_color}">{_v}</span>）</div>
  <div class="card" style="overflow-x:auto">
    <table class="edge-table">
      <tr><th>日期</th><th>场次</th><th>命中</th><th>命中率</th><th>Brier</th><th>累计命中率</th><th>盈亏</th></tr>
      {_rows_html}
    </table>
    <div class="note">⚠ 置信度不预示盈亏：高置信 ≈ 低赔热门 ≈ 抽水最大。收益依赖<b>正 EV 门槛</b>与校准质量，实时状态见上方「🧬 进化仪表盘」。</div>
  </div>'''


def _score_and_signal_section(ledger: list, ref_date: str) -> str:
    """比分命中率分层 · 双源候选对比 · 盘口压缩信号验证"""
    if not ledger:
        return ""
    _n = len(ledger)
    def _rate(cond):
        return sum(1 for r in ledger if cond(r)) / _n
    _cutoff = ref_date
    try:
        from datetime import datetime as _dtm
        from datetime import timedelta as _td
        if ref_date:
            _cutoff = (_dtm.strptime(ref_date[:10], "%Y-%m-%d") - _td(days=7)).strftime("%Y-%m-%d")
    except Exception:
        _cutoff = ref_date
    _recent = [r for r in ledger if r.get("date", "") >= _cutoff] if _cutoff else ledger
    _rn = len(_recent)
    def _rate7(cond):
        return sum(1 for r in _recent if cond(r)) / _rn if _rn else 0

    _parts = [f'''
  <div class="section-title" style="margin-top:8px">比分命中率（全量 {_n} 场 · 近7天 {_rn} 场）</div>
  <div style="overflow-x:auto">
    <table class="edge-table">
      <tr><th>推荐档</th><th>全量命中</th><th>全量命中率</th><th>近7天命中</th><th>近7天命中率</th><th>说明</th></tr>
      <tr><td>主推 top1</td><td>{sum(1 for r in ledger if r.get('score_rank')==1)}</td><td style="font-weight:700">{_rate(lambda r: r.get('score_rank')==1)*100:.0f}%</td><td>{sum(1 for r in _recent if r.get('score_rank')==1)}</td><td>{_rate7(lambda r: r.get('score_rank')==1)*100:.0f}%</td><td style="color:var(--dim)">只押最可能比分</td></tr>
      <tr><td>主推 top3</td><td>{sum(1 for r in ledger if r.get('score_top3_hit'))}</td><td style="font-weight:700">{_rate(lambda r: r.get('score_top3_hit'))*100:.0f}%</td><td>{sum(1 for r in _recent if r.get('score_top3_hit'))}</td><td>{_rate7(lambda r: r.get('score_top3_hit'))*100:.0f}%</td><td style="color:var(--dim)">原主推 3 个</td></tr>
      <tr><td>主推 top5</td><td>{sum(1 for r in ledger if r.get('score_top5_hit'))}</td><td style="font-weight:700;color:var(--green)">{_rate(lambda r: r.get('score_top5_hit'))*100:.0f}%</td><td>{sum(1 for r in _recent if r.get('score_top5_hit'))}</td><td style="color:var(--green)">{_rate7(lambda r: r.get('score_top5_hit'))*100:.0f}%</td><td style="color:var(--dim)">当前主推 5 个</td></tr>
      <tr><td>候选 top8</td><td>{sum(1 for r in ledger if r.get('score_top8_hit'))}</td><td style="font-weight:700">{_rate(lambda r: r.get('score_top8_hit'))*100:.0f}%</td><td>{sum(1 for r in _recent if r.get('score_top8_hit'))}</td><td>{_rate7(lambda r: r.get('score_top8_hit'))*100:.0f}%</td><td style="color:var(--dim)">DJYY 完整候选列表</td></tr>
    </table>
  </div>''']

    _dj_items = [r for r in ledger if r.get('score_djyy_rank', -1) >= 0]
    _mc_items = [r for r in ledger if r.get('score_mc_rank', -1) >= 0]
    _src_rows = ""
    if _dj_items:
        _dh = sum(1 for r in _dj_items if 1 <= r.get('score_djyy_rank', 0) <= 5)
        _src_rows += f'<tr><td>DJYY 分析源</td><td>{_dh}/{len(_dj_items)}</td><td style="font-weight:700">{_dh/len(_dj_items)*100:.0f}%</td><td style="color:var(--dim)">分析文本提取的比分候选</td></tr>'
    if _mc_items:
        _mh = sum(1 for r in _mc_items if 1 <= r.get('score_mc_rank', 0) <= 5)
        _src_rows += f'<tr><td>MC 模拟源</td><td>{_mh}/{len(_mc_items)}</td><td style="font-weight:700">{_mh/len(_mc_items)*100:.0f}%</td><td style="color:var(--dim)">泊松蒙特卡洛模拟</td></tr>'
    if _src_rows:
        _parts.append(f'''
  <div class="section-title">比分候选来源对比（DJYY vs MC · 主推前5命中率）</div>
  <div style="overflow-x:auto">
    <table class="edge-table">
      <tr><th>来源</th><th>命中/场次</th><th>命中率</th><th>说明</th></tr>
      {_src_rows}
    </table>
  </div>''')

    _ms_items = [r for r in ledger if r.get('market_signal_hit') is not None]
    if _ms_items:
        _msh = sum(1 for r in _ms_items if r.get('market_signal_hit'))
        _msr = _msh / len(_ms_items)
        _ms_color = "var(--green)" if _msr >= 0.45 else ("var(--red)" if _msr <= 0.35 else "var(--amber)")
        _ms_verdict = ("≥基线(44%)，信号有预测力" if _msr >= 0.45
                       else ("显著低于基线，信号待观察" if _msr <= 0.35 else "与基线接近，继续累积样本"))
        _parts.append(f'''
  <div class="section-title">盘口信号命中率（压缩比资金流向）</div>
  <div style="overflow-x:auto">
    <table class="edge-table">
      <tr><th>信号</th><th>命中/场次</th><th>命中率</th><th>模型方向基线</th><th>判断</th></tr>
      <tr><td>欧赔压缩方向</td><td>{_msh}/{len(_ms_items)}</td><td style="font-weight:700;color:{_ms_color}">{_msr*100:.0f}%</td><td>{sum(1 for r in ledger if r.get('hit'))}/{_n} ({sum(1 for r in ledger if r.get('hit'))/_n*100:.0f}%)</td><td style="color:{_ms_color}">{_ms_verdict}</td></tr>
    </table>
  </div>''')

    return f'''
  <details class="deep-fold">
    <summary>
      <span class="deep-fold-title">比分命中率 · 候选来源 · 盘口信号</span>
      <span class="deep-fold-meta">全量累计 vs 近7天 · DJYY/MC 双源 · 资金流向信号验证</span>
      <span class="deep-fold-hint">点击展开 ▾</span>
    </summary>
    <div class="deep-fold-body">{"".join(_parts)}</div>
  </details>'''


def _weekly_backtest_section(wb: dict) -> str:
    """预测时点分桶 + Dixon-Coles 分层收缩挑战者对照"""
    _steps = (wb or {}).get("steps") or {}
    _parts = []
    _hor = _steps.get("horizon_eval") or {}
    _buckets = _hor.get("buckets") or []
    if _buckets:
        _bh = _hor.get("best_horizon") or ""
        _rows = ""
        for _b in _buckets:
            _n = _b.get("n", 0)
            _brier = _b.get("brier") or 0
            _hit = _b.get("hit_rate") or 0
            _ev = _b.get("total_ev") or 0
            _ev_c = "var(--green)" if _ev > 0 else ("var(--red)" if _ev < 0 else "var(--dim)")
            _rows += (f'<tr><td>{_b.get("label", _b.get("horizon", ""))}</td>'
                      f'<td>{_n}</td><td>{_brier:.4f}</td><td>{_hit*100:.1f}%</td>'
                      f'<td style="color:{_ev_c};font-weight:700">{_ev:+.2f}u</td></tr>')
        _parts.append(f'''
  <div class="section-title" style="margin-top:8px">预测时点分桶（提前多久预测最准）</div>
  <div class="note" style="margin-bottom:6px">按预测截点距开赛时长分桶评估。最佳时点: <b>{_bh or "待积累"}</b>。</div>
  <div style="overflow-x:auto"><table class="edge-table">
    <tr><th>时点</th><th>场次</th><th>Brier</th><th>命中率</th><th>总EV</th></tr>
    {_rows}
  </table></div>''')

    _sd = _steps.get("shrinkage_dc") or {}
    _n_ev = _sd.get("n_evaluated", 0)
    if _n_ev:
        _verdict = _sd.get("verdict", "hold")
        _v_color = "var(--amber)" if _verdict == "hold" else ("var(--green)" if _verdict == "promote" else "var(--red)")
        def _brier_row(_label, _key):
            _v = _sd.get(_key)
            if _v is None:
                return ""
            return f'<tr><td>{_label}</td><td style="font-weight:700">{_v:.4f}</td></tr>'
        _parts.append(f'''
  <div class="section-title">Dixon-Coles 分层收缩挑战者（vs 生产模型）</div>
  <div class="note" style="margin-bottom:6px">滚动拟合（只用评估日之前历史，防泄漏）对比 Brier，n={_n_ev} 场。Verdict: <b style="color:{_v_color}">{_verdict}</b>。</div>
  <div style="overflow-x:auto"><table class="edge-table">
    <tr><th>模型</th><th>Brier</th></tr>
    {_brier_row("shrinkage_dc（挑战者）", "shrinkage_dc_brier")}
    {_brier_row("生产 final（现役）", "final_brier")}
    {_brier_row("模型原始（未融合）", "model_raw_brier")}
    {_brier_row("市场基准（去水）", "market_brier")}
  </table></div>''')

    if not _parts:
        return ""
    return f'''
  <details class="deep-fold">
    <summary>
      <span class="deep-fold-title">预测时点分桶 · Dixon-Coles 挑战者</span>
      <span class="deep-fold-meta">提前多久预测最准 · 分层收缩模型 vs 生产模型</span>
      <span class="deep-fold-hint">点击展开 ▾</span>
    </summary>
    <div class="deep-fold-body">{"".join(_parts)}</div>
  </details>'''


def _render_track_record(state_dir: Path, web_dir: Path) -> str:
    """生成公开 Track Record 页（v2洁净口径优先 · 托管全部全量统计）"""
    ledger = []
    lp = state_dir / "review_ledger.jsonl"
    if lp.exists():
        for _line in lp.read_text(encoding="utf-8").strip().split("\n"):
            if _line.strip():
                try:
                    ledger.append(json.loads(_line))
                except Exception:
                    continue
    at = _load_json(state_dir / "accuracy_trend.json", {})
    wb = _load_json(state_dir / "weekly_backtest.json", {})
    ps = _load_json(state_dir / "parlay_settle.json", {})
    ut = _load_json(state_dir / "upgrade_tracker.json", {})

    n = len(ledger)
    hits = sum(1 for r in ledger if r.get("hit"))
    hit_rate = hits / n if n else 0
    draws_actual = sum(1 for r in ledger if r.get("actual_idx") == 1)
    draws_hit = sum(1 for r in ledger if r.get("hit") and r.get("actual_idx") == 1)

    from collections import defaultdict
    by_day = defaultdict(lambda: [0, 0])
    for r in ledger:
        by_day[r.get("date", "")][1] += 1
        if r.get("hit"):
            by_day[r.get("date", "")][0] += 1
    day_rows = ""
    for d in sorted(by_day.keys())[-14:]:
        h, t = by_day[d]
        _r = h / t if t else 0
        _c = "var(--green)" if _r >= 0.5 else ("var(--amber)" if _r >= 0.4 else "var(--red)")
        day_rows += f'<tr><td>{d}</td><td>{t}</td><td>{h}</td><td style="color:{_c};font-weight:700">{_r*100:.0f}%</td></tr>'

    by_league = defaultdict(lambda: [0, 0, 0])
    for r in ledger:
        lg = r.get("league") or "未知"
        by_league[lg][0] += 1
        if r.get("hit"):
            by_league[lg][1] += 1
        if r.get("actual_idx") == 1:
            by_league[lg][2] += 1
    lg_rows = ""
    for lg, (t, h, d) in sorted(by_league.items(), key=lambda x: -x[1][0]):
        if t < 5:
            continue
        _r = h / t
        _dr = d / t
        _c = "var(--green)" if _r >= 0.5 else ("var(--amber)" if _r >= 0.4 else "var(--red)")
        lg_rows += (f'<tr><td>{lg}</td><td>{t}</td>'
                    f'<td style="color:{_c};font-weight:700">{_r*100:.0f}%</td>'
                    f'<td>{_dr*100:.0f}%</td></tr>')

    _sd = wb.get("steps", {}).get("shrinkage_dc", {})
    _ov = at.get("overall", {})

    def _brier_cell(v):
        return f'<td style="font-weight:700">{v:.4f}</td>' if v is not None else "<td>—</td>"

    _ov_b = _ov.get("brier")
    _ov_m = _ov.get("brier_market")
    _ov_mdl = _ov.get("brier_model")
    _sd_b = _sd.get("shrinkage_dc_brier")
    _sd_f = _sd.get("final_brier")

    # v2-first KPI 口径
    _v2 = ut.get("v2_clean") or {}
    _v2_n = _v2.get("n") or 0
    _v2_hr = _v2.get("hit_rate")
    _v2_brier = _v2.get("brier")
    _pvm = ut.get("paired_vs_market") or {}
    _pvm_n = _pvm.get("n") or 0
    _pvm_v = _pvm.get("verdict") or ""
    _pvm_map = {"BEATING": ("优于市场", "green"), "LOSING": ("劣于市场", "red"),
                "TIED": ("与市场持平", "amber"), "WARMING_UP": ("样本预热", "")}
    _pvm_lbl, _pvm_cls = _pvm_map.get(_pvm_v, (_pvm_v or "待样本", ""))
    _r7 = at.get("rolling7") or {}
    _r7_hr = _r7.get("hit_rate", 0)
    _r7_n = _r7.get("n", 0)

    # 串关/波胆真实复盘（2026-08-13：Track Record 页补齐串关战绩）
    def _ps_stat_card(title, s):
        if not s or not s.get("n_tickets"):
            return ""
        hr = f"{s.get('hit_rate', 0):.0%}" if s.get("hit_rate") is not None else "—"
        roi = s.get("roi") or 0
        roi_c = "var(--green)" if roi > 0 else "var(--red)"
        by_type = ""
        if s.get("by_type"):
            by_type = " · ".join(
                f"{k} {v.get('n',0)}张中{v.get('won',0)} ROI {(v.get('roi') or 0):+.0%}"
                for k, v in s["by_type"].items()
            )
        extra_leg = ""
        if s.get("leg_hit_rate") is not None:
            extra_leg = f'<tr><td>单腿命中</td><td>{s["leg_hit_rate"]:.0%}（{s.get("n_legs_settled",0)} 腿）</td></tr>'
        return f"""
        <div class="card">
          <div class="section-title" style="margin-top:0">{title}</div>
          <table>
            <tr><th>指标</th><th>值</th></tr>
            <tr><td>出票 / 已结算</td><td>{s['n_tickets']} / {s.get('n_settled', s['n_tickets'])}</td></tr>
            <tr><td>命中</td><td>{s.get('n_won',0)} 张（命中率 {hr}）</td></tr>
            <tr><td>已出票投入</td><td>¥{s.get('stake_committed', s.get('stake',0)):.2f}</td></tr>
            <tr><td>已结算投入 / 回报</td><td>¥{s.get('stake',0):.2f} / ¥{s.get('return',0):.2f}</td></tr>
            <tr><td>ROI</td><td style="color:{roi_c};font-weight:700">{roi:+.1%}</td></tr>
            {extra_leg}
          </table>
          {f'<div class="note" style="margin-top:6px">{by_type}</div>' if by_type else ''}
        </div>"""

    _parlay_html = ""
    if ps:
        _pl = ps.get("parlay") or {}
        _sp = ps.get("score_parlay") or {}
        _cards = "".join(x for x in (_ps_stat_card("📋 胜平负串", _pl), _ps_stat_card("🎰 比分串（波胆）", _sp)) if x)
        if _cards:
            _verdict = ps.get("verdict", "")
            _verdict_html = f'<div class="note" style="margin-top:8px">{_verdict}</div>' if _verdict else ""
            _note_parts = []
            if _pl and _pl.get("n_tickets"):
                _pl_roi = _pl.get("roi")
                _pl_roi_txt = "—" if _pl_roi is None else f"{_pl_roi:+.0%}"
                _note_parts.append(
                    f"胜平负串 {_pl['n_tickets']}张/已结算{_pl.get('n_settled', 0)} · ROI {_pl_roi_txt}"
                )
            if _sp and _sp.get("n_tickets"):
                _sp_roi = _sp.get("roi")
                _sp_roi_txt = "—" if _sp_roi is None else f"{_sp_roi:+.0%}"
                _note_parts.append(
                    f"比分串 {_sp['n_tickets']}张/已结算{_sp.get('n_settled', 0)} · ROI {_sp_roi_txt}"
                )
            _settle_note = "历史真实出票（非回测）：" + "；".join(_note_parts) + "。2026-08-13 起比分串改为仅官方赔率正 EV 出票。"
            _parlay_html = f'''
  <div class="section-title">串关 / 波胆真实复盘（历史累计 · 非回测）</div>
  <div style="padding:6px 2px 10px;font-size:0.68rem;color:var(--dim)">{_settle_note}</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">
    {_cards}
  </div>
  {_verdict_html}'''

    # 校准可靠性（2026-08-16 新增：把“说 70% 就真的 70%”可视化）
    _rel_html = ""
    try:
        import numpy as np
        from engine.prediction.reliability_curve import compute_outcome_reliability
        _rel_recs = [r for r in ledger if r.get("final_prob") and r.get("actual_idx") is not None]
        if _rel_recs:
            _probs = np.array([r["final_prob"] for r in _rel_recs])
            _actuals = np.array([r["actual_idx"] for r in _rel_recs], dtype=int)
            _reports = compute_outcome_reliability(_probs, _actuals, n_bins=10)
            _rel_rows = ""
            for _label, _rep in _reports.items():
                if not _rep.bins:
                    continue
                _rel_rows += (
                    f"<tr><td>{_label}</td><td>{_rep.ece:.4f}</td><td>{_rep.mce:.4f}</td>"
                    f"<td>{_rep.n_samples}</td><td>{_rep.well_calibrated_ratio:.0%}</td></tr>"
                )
            if _rel_rows:
                _rel_html = f'''
  <div class="section-title">校准可靠性（ECE / MCE，越低越好）</div>
  <div class="card"><table>
    <tr><th>结果类</th><th>ECE</th><th>MCE</th><th>样本</th><th>良好校准箱占比</th></tr>
    {_rel_rows}
  </table></div>'''
    except Exception:
        _rel_html = ""

    # 数据覆盖率（全量预测关键字段，2026-08-16 新增）
    _coverage_html = ""
    try:
        _daily_root = state_dir.parent / "daily"
        _fields = ("crs_odds", "ttg_odds", "hafu_odds", "handicap", "market_fair", "sina_odds")
        _total = 0
        _covered = {f: 0 for f in _fields}
        for _pf in sorted(_daily_root.glob("*/predictions.json")):
            try:
                _preds = json.loads(_pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(_preds, dict):
                _preds = _preds.get("predictions", [])
            for _pred in _preds or []:
                _total += 1
                for _f in _fields:
                    _v = _pred.get(_f)
                    if _f == "handicap":
                        _ok = _v is not None
                    elif _f == "market_fair":
                        _ok = _v not in (None, [])
                    else:
                        _ok = _v not in (None, {})
                    if _ok:
                        _covered[_f] += 1
        if _total:
            _labels = {
                "crs_odds": "波胆赔率", "ttg_odds": "总进球赔率", "hafu_odds": "半全场赔率",
                "handicap": "让球", "market_fair": "市场去水", "sina_odds": "新浪赔率",
            }
            _cov_rows = "".join(
                f"<tr><td>{_labels[_f]}</td><td>{_covered[_f]}/{_total}</td>"
                f"<td>{_covered[_f] / _total:.0%}</td></tr>"
                for _f in _fields
            )
            _coverage_html = f'''
  <div class="section-title">数据覆盖率（全量预测）</div>
  <div class="card"><table>
    <tr><th>字段</th><th>覆盖</th><th>覆盖率</th></tr>
    {_cov_rows}
  </table></div>'''
    except Exception:
        _coverage_html = ""

    _ev_report = _load_json(state_dir / "ev_report.json", {})
    _ev_html = _ev_section(_ev_report) if _ev_report else ""

    _dr = at.get("date_range") or []
    if isinstance(_dr, list) and len(_dr) >= 2:
        _dr_text = f"{_dr[0]} ~ {_dr[-1]}"
    else:
        _dr_text = str(_dr or "")

    _ref_date = ""
    if ledger:
        _ref_date = max((r.get("date") or "" for r in ledger), default="")
    elif isinstance(_dr, list) and _dr:
        _ref_date = str(_dr[-1])

    _evo_html = _evo_dashboard(state_dir=state_dir)
    _trend_html = _accuracy_trend_section(at)
    if not _trend_html and day_rows:
        _trend_html = f'''
  <div class="section-title">每日命中率（近14天）</div>
  <div class="card tabular" style="overflow-x:auto"><table>
    <tr><th>日期</th><th>场次</th><th>命中</th><th>命中率</th></tr>
    {day_rows}
  </table></div>'''
    _layers_html = _league_layers_section(state_dir, fallback_rows=lg_rows)
    _hcr_html = _hcr_section(state_dir)
    _score_html = _score_and_signal_section(ledger, _ref_date)
    _wb_html = _weekly_backtest_section(wb)

    _lm_path = state_dir.parent / "league_matrix.json"
    _lm = _load_league_matrix(_lm_path) if _lm_path.exists() else {}
    _lm_html = ""
    if _lm and _lm.get("leagues"):
        _lm_inner = _league_matrix_section(_lm, [], ref_date=_ref_date, embedded=True)
        _lm_html = f'''
  <details class="deep-fold">
    <summary>
      <span class="deep-fold-title">联赛矩阵（DJYY 参考数据 · {len(_lm.get("leagues", []))} 联赛）</span>
      <span class="deep-fold-meta">场均进球 / xG / BTTS / 胜平负分布</span>
      <span class="deep-fold-hint">点击展开 ▾</span>
    </summary>
    <div class="deep-fold-body">{_lm_inner}</div>
  </details>'''

    _pvm_row = ""
    if _pvm_n:
        _pvm_d = _pvm.get("delta_brier")
        _pvm_d_txt = f"{_pvm_d:+.4f}" if _pvm_d is not None else "—"
        _pvm_row = (
            f'<tr><td><b>v2 final（配对同样本 n={_pvm_n}）</b></td>'
            f'{_brier_cell(_pvm.get("brier_final"))}</tr>'
            f'<tr><td><b>市场去水基准（配对同样本 n={_pvm_n}）</b></td>'
            f'<td style="font-weight:700">{_pvm.get("brier_market"):.4f} '
            f'<span style="font-size:0.74rem;color:var(--dim)">(Δ={_pvm_d_txt} · t={_pvm.get("t", 0):+.2f})</span></td></tr>'
            if _pvm.get("brier_market") is not None else ""
        )

    html = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Track Record · 足球预测</title>
<style>
:root {{
  --bg:#f8fafc; --surface:#ffffff; --card:var(--surface); --surface2:#f1f5f9; --surface3:#e2e8f0;
  --border:#e2e8f0; --border-light:#cbd5e1;
  --text:#0f172a; --text-secondary:#475569; --dim:#94a3b8;
  --blue:#2563eb; --red:#be123c; --green:#15803d; --amber:#b45309; --cyan:#0e7490;
  --purple:#6d28d9; --teal:#0f766e;
  --blue-dim:rgba(37,99,235,.08); --red-dim:rgba(190,18,60,.08);
  --green-dim:rgba(21,128,61,.09); --amber-dim:rgba(180,83,9,.09);
  --purple-dim:rgba(109,40,217,.08);
  --radius:12px; --radius-sm:8px;
  --shadow:0 1px 2px rgba(15,23,42,.04);
  --shadow-lift:0 4px 12px rgba(15,23,42,.06);
  --transition:all .15s ease;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.55; -webkit-font-smoothing:antialiased;
}}
.tabular, table, .kpi .value {{ font-variant-numeric:tabular-nums; }}
a {{ color:var(--blue); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.page {{ max-width:1040px; margin:0 auto; padding:0 16px 56px; }}
.topbar {{
  position:sticky; top:0; z-index:50;
  display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;
  padding:12px 0; margin-bottom:18px;
  background:rgba(248,250,252,.92); backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
  border-bottom:1px solid var(--border);
}}
.brand {{ display:flex; align-items:baseline; gap:10px; }}
.brand h1 {{ font-size:1.05rem; font-weight:800; letter-spacing:-0.02em; }}
.brand .sub {{ color:var(--dim); font-size:0.74rem; font-variant-numeric:tabular-nums; }}
.seg-tabs {{
  display:inline-flex; gap:2px; padding:3px;
  background:var(--surface2); border:1px solid var(--border); border-radius:10px;
}}
.page-tab-btn {{
  padding:6px 14px; border:none; border-radius:7px; background:transparent;
  color:var(--text-secondary); font-size:0.8rem; font-weight:600; cursor:pointer;
  transition:var(--transition); font-family:inherit;
}}
.page-tab-btn:hover {{ color:var(--text); }}
.page-tab-btn.active {{ background:var(--surface); color:var(--text); font-weight:700; box-shadow:var(--shadow); }}
.page-tab-panel {{ display:none; }}
.page-tab-panel.active {{ display:block; }}
.nav-link {{
  color:var(--text-secondary); font-size:0.76rem; font-weight:600;
  padding:6px 12px; border:1px solid var(--border); border-radius:8px; background:var(--surface);
  transition:var(--transition); white-space:nowrap;
}}
.nav-link:hover {{ color:var(--blue); border-color:var(--blue); text-decoration:none; }}

.summary-strip {{
  display:grid; grid-template-columns:repeat(5, minmax(0, 1fr));
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  overflow:hidden; margin-bottom:22px; box-shadow:var(--shadow);
}}
.sum-cell {{ padding:14px 16px; border-right:1px solid var(--border); min-width:0; }}
.sum-cell:last-child {{ border-right:none; }}
.sum-label {{ color:var(--dim); font-size:0.68rem; font-weight:600; letter-spacing:0.03em; }}
.sum-val {{ font-size:1.3rem; font-weight:800; margin-top:4px; letter-spacing:-0.02em; font-variant-numeric:tabular-nums; }}
.sum-sub {{ font-size:0.68rem; color:var(--text-secondary); margin-top:3px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.green {{ color:var(--green); }} .amber {{ color:var(--amber); }} .red {{ color:var(--red); }} .blue {{ color:var(--blue); }}

.section-title {{
  font-size:0.88rem; font-weight:700; margin:24px 0 10px; color:var(--text);
  display:flex; align-items:baseline; justify-content:space-between; gap:8px; flex-wrap:wrap;
}}
.section-title .st-note {{ font-size:0.72rem; color:var(--dim); font-weight:500; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; margin-bottom:12px; box-shadow:var(--shadow); }}
.two-col {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:12px; align-items:start; }}
.stat {{ background:var(--surface2); border:1px solid var(--border); border-radius:var(--radius-sm); padding:10px 14px; min-width:115px; }}
.stat .label {{ font-size:0.68rem; color:var(--dim); }}
.stat .value {{ font-size:1.2rem; font-weight:800; margin-top:2px; }}

table {{ width:100%; border-collapse:collapse; font-size:0.78rem; }}
th {{ text-align:left; color:var(--dim); font-weight:600; padding:8px 10px; border-bottom:1px solid var(--border); background:var(--surface2); font-size:0.72rem; }}
th:first-child {{ border-radius:6px 0 0 6px; }}
th:last-child {{ border-radius:0 6px 6px 0; }}
td {{ padding:8px 10px; border-bottom:1px solid var(--border); color:var(--text-secondary); }}
tr:last-child td {{ border-bottom:none; }}
.edge-table, .data-table {{ width:100%; border-collapse:collapse; font-size:0.78rem; }}
.ev-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px; }}
.ev-grid > div {{ min-width:0; }}
.ev-table-wrap {{ overflow-x:auto; }}
.note {{ color:var(--text-secondary); font-size:0.72rem; margin-top:8px; }}

.evo-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:10px; }}
.evo-card {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); padding:13px 14px; box-shadow:var(--shadow); }}
.evo-title {{ font-size:0.7rem; color:var(--dim); margin-bottom:4px; font-weight:600; }}
.evo-verdict {{ font-size:1rem; font-weight:800; }}
.evo-detail {{ font-size:0.7rem; color:var(--text-secondary); margin-top:5px; line-height:1.5; }}
.evo-chip {{ display:inline-block; background:var(--surface2); border-radius:4px; padding:1px 6px; margin:2px 4px 2px 0; font-size:0.66rem; }}
.evo-chip i {{ color:var(--dim); font-style:normal; font-size:0.62rem; }}

.deep-fold {{ border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); margin:12px 0; overflow:hidden; box-shadow:var(--shadow); }}
.deep-fold > summary {{ list-style:none; cursor:pointer; user-select:none; display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:12px 16px; }}
.deep-fold > summary::-webkit-details-marker {{ display:none; }}
.deep-fold > summary::before {{ content:'▸'; color:var(--dim); font-size:0.8rem; transition:transform .15s; }}
.deep-fold[open] > summary::before {{ transform:rotate(90deg); }}
.deep-fold-title {{ font-weight:700; font-size:0.86rem; color:var(--text); }}
.deep-fold-meta {{ font-size:0.72rem; color:var(--dim); }}
.deep-fold-hint {{ margin-left:auto; font-size:0.68rem; color:var(--blue); font-weight:600; }}
.deep-fold[open] .deep-fold-hint {{ display:none; }}
.deep-fold-body {{ padding:6px 16px 14px; border-top:1px solid var(--border); }}

.lm-wrap {{ overflow-x:auto; }}
.lm-table th, .lm-table td {{ padding:6px 8px; text-align:center; font-size:0.74rem; }}
.lm-table th:nth-child(2), .lm-table td:nth-child(2) {{ text-align:left; font-weight:600; color:var(--text); }}
.lm-cat {{ display:inline-block; padding:1px 7px; border-radius:10px; font-size:0.62rem; font-weight:700; }}
.lm-cat-tier1 {{ background:var(--green-dim); color:var(--green); }}
.lm-cat-tier2 {{ background:var(--amber-dim); color:var(--amber); }}
.lm-cat-world {{ background:var(--blue-dim); color:var(--blue); }}
.lm-cat-other {{ background:var(--surface2); color:var(--dim); }}
.lm-cat-cup {{ background:var(--purple-dim); color:var(--purple); }}
.stale-warn {{ display:inline-flex; align-items:center; gap:6px; background:var(--amber-dim); color:var(--amber); border:1px solid rgba(180,83,9,.25); border-radius:6px; padding:4px 10px; font-size:0.72rem; font-weight:600; margin:6px 0; }}
.footer {{ margin-top:36px; padding-top:16px; border-top:1px solid var(--border); color:var(--dim); font-size:0.72rem; }}

@media (max-width:860px) {{
  .summary-strip {{ grid-template-columns:repeat(3, minmax(0, 1fr)); }}
  .sum-cell {{ border-bottom:1px solid var(--border); }}
}}
@media (max-width:560px) {{
  .summary-strip {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
  .topbar {{ gap:8px; }}
  .page-tab-btn {{ padding:5px 10px; font-size:0.75rem; }}
  .ev-grid {{ grid-template-columns:1fr; }}
}}
</style></head><body>
<div class="page">
  <div class="topbar">
    <div class="brand">
      <h1>Track Record</h1>
      <span class="sub">截至 {_dr_text or '最新'} · 更新 {at.get("generated_at", "")[:16]}</span>
    </div>
    <div class="seg-tabs">
      <button class="page-tab-btn active" data-panel="tr-overview">核心战绩</button>
      <button class="page-tab-btn" data-panel="tr-betting">联赛与投注</button>
      <button class="page-tab-btn" data-panel="tr-diagnostics">模型诊断</button>
    </div>
    <a href="index.html" class="nav-link">← 今日预测</a>
  </div>

  <div class="summary-strip">
    <div class="sum-cell"><div class="sum-label">v2 洁净场次</div><div class="sum-val blue">{_v2_n or '—'}</div><div class="sum-sub">主口径 chain=v2</div></div>
    <div class="sum-cell"><div class="sum-label">v2 方向命中率</div><div class="sum-val {'green' if (_v2_hr or 0)>=0.5 else ('amber' if (_v2_hr or 0)>=0.4 else 'red')}">{f'{_v2_hr*100:.1f}%' if _v2_hr is not None else '—'}</div><div class="sum-sub">{f'Brier {_v2_brier:.4f}' if _v2_brier is not None else '等待样本'}</div></div>
    <div class="sum-cell"><div class="sum-label">v2 vs 市场 (配对)</div><div class="sum-val {_pvm_cls}">{_pvm_lbl}</div><div class="sum-sub">{f'Δ{_pvm.get("delta_brier",0):+.4f} · t={_pvm.get("t",0):+.2f} · n={_pvm_n}' if _pvm_n else '待配对样本'}</div></div>
    <div class="sum-cell"><div class="sum-label">近7天命中率</div><div class="sum-val {'green' if _r7_hr>=0.5 else 'amber'}">{_r7_hr*100:.0f}%</div><div class="sum-sub">近7天 n={_r7_n}</div></div>
    <div class="sum-cell"><div class="sum-label">全量复盘 (含legacy)</div><div class="sum-val">{n}</div><div class="sum-sub">命中 {hit_rate*100:.1f}% · 平局 {draws_actual} ({draws_hit}中)</div></div>
  </div>

  <div class="page-tab-panel active" id="tr-overview">
    {_evo_html}
    <div class="two-col" style="margin-top:14px">
      <div>
        <div class="section-title">概率质量（Brier 越低越好）<span class="st-note">看同样本配对</span></div>
        <div class="card"><table>
          <tr><th>口径</th><th>Brier</th></tr>
          {_pvm_row}
          <tr><td>融合 final（全量生产）</td>{_brier_cell(_ov_b or _sd_f)}</tr>
          <tr><td>市场基准（全量去水）</td>{_brier_cell(_ov_m)}</tr>
          <tr><td>模型原始（全量未融合）</td>{_brier_cell(_ov_mdl)}</tr>
          <tr><td>shrinkage_dc（挑战者）</td>{_brier_cell(_sd_b)}</tr>
        </table></div>
      </div>
      <div>
        {_rel_html}
      </div>
    </div>
    {_trend_html}
  </div>

  <div class="page-tab-panel" id="tr-betting">
    {_parlay_html}
    {_ev_html}
    {_layers_html}
  </div>

  <div class="page-tab-panel" id="tr-diagnostics">
    {_coverage_html}
    {_hcr_html}
    {_score_html}
    {_wb_html}
    {_lm_html}
    <div class="section-title">方法论</div>
    <div class="card" style="font-size:0.78rem;color:var(--text-secondary)">
      {_pipeline_desc()}（DJYY 置信&lt;0.5 不参与、与市场分歧减半）。
      融合链每一步均为 config 开关，由消融回放（scripts/ablation_replay.py）对已结算账本
      配对验证后裁决去留；校准层自进化带显著性护栏（docs/UPGRADE2_20260829.md）。
      <div class="note">全程 GitHub Actions 自动运行，零人工干预。历史胜平负概率与结算结果全部可核查（review_ledger.jsonl，{n}+ 场）。</div>
    </div>
  </div>

  <div class="footer">
    竞彩足球概率分析系统 · 数据源：体彩官方/新浪/500万/DJYY · 仅供研究，理性购彩 ·
    <a href="https://github.com/wlrwx/football-engine">源代码</a>
  </div>
</div>
<script>
document.querySelectorAll('.page-tab-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    var id = this.getAttribute('data-panel');
    document.querySelectorAll('.page-tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    this.classList.add('active');
    document.querySelectorAll('.page-tab-panel').forEach(function(p) {{ p.classList.remove('active'); }});
    var el = document.getElementById(id);
    if (el) el.classList.add('active');
    window.scrollTo({{ top: 0, behavior: 'smooth' }});
  }});
}});
</script>
</body></html>'''
    out = web_dir / "track-record.html"
    out.write_text("\n".join(line.rstrip() for line in html.split("\n")), encoding="utf-8")
    return out.name


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
            # 注意：绝不按裸场次号（"001"/"周五001"）索引。
            # 2026-08-14 事故：周五001 被匹配成 7/28 周二001 的赛果 0-2，
            # 未开赛比赛在页面上显示伪造"实际比分"。
    return index


def _match_results_to_predictions(predictions: list, all_results: dict) -> list:
    """用全局索引为预测匹配赛果，返回匹配的 results 列表。

    只允许精确 match_id 或队名匹配；绝不用裸场次号（会跨日期/跨星期伪造赛果）。
    """
    matched = []
    for p in predictions:
        mid = p.get("match_id", "")
        r = all_results.get(mid)
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
    # 三票方案中的场次 = 真正的价值投注
    value_matches = set()
    for it in ticket.get("stable", []) + ticket.get("value", []):
        value_matches.add(it.get("match", ""))
    value_bets = [p for p in predictions if _is_value(p, value_matches)]
    breaker_mult = ticket.get("breaker_multiplier", 1.0)
    tier, tier_reason = _breaker_tier(breaker)

    # 水位监控汇总（2026-08-05 盘口系统修复）：当天各场水位时间序列累积情况
    odds_series_html = ""
    try:
        _os_rows = []
        for _p in predictions:
            _so = _p.get("sina_odds") or {}
            _ser = _so.get("series") or {}
            if _ser.get("points", 0) < 2:
                continue
            _parts = []
            for _side, _key in (("主", "recent_home"), ("客", "recent_away")):
                _v = _ser.get(_key)
                if _v is not None and abs(_v) >= 0.5:
                    _parts.append(f"{_side}{'↓' if _v < 0 else '↑'}{abs(_v):.1f}%")
            if not _parts:
                continue
            _span_min = _ser.get("span_min", "") or 0
            _os_rows.append(
                f'<tr><td>{_p.get("match_id", "").split("_")[-1]}</td>'
                f'<td>{_p.get("home_team", "")} vs {_p.get("away_team", "")}</td>'
                f'<td>{_ser.get("points", 0)}次</td>'
                f'<td>{" ".join(_parts)}</td>'
                f'<td style="color:var(--dim)">{("跨%.0fmin" % _span_min) if _span_min else ""}</td></tr>'
            )
        if _os_rows:
            odds_series_html = f'''
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>场次</th><th>对阵</th><th>快照数</th><th>近期水位变化</th><th>跨度</th></tr>
    {''.join(_os_rows)}
  </table>
  </div>'''
    except Exception as e:
        print(f"⚠ 水位监控区块跳过: {e}")

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

    # 按 match_id 排序：周六201→周六202→...→周日201→周日202→...→周一201→...
    import re
    _day_map = {'周六': 0, '周日': 1, '周一': 2, '周二': 3, '周三': 4, '周四': 5, '周五': 6}
    def _match_sort_key(p):
        mid = p.get("match_id", "")
        m = re.search(r'(周[一二三四五六日])(\d+)', mid)
        if m:
            day = _day_map.get(m.group(1), 99)
            num = int(m.group(2))
            return (day, num)
        return (99, 0)
    # 2026-08-30 重塑：按开赛时间排序（无开赛时间的按编号排后），次要键保持竞彩编号
    from datetime import datetime as _dtm
    def _kickoff_key(p):
        k = str(p.get("kickoff") or "")
        try:
            return _dtm.strptime(k[:16], "%Y-%m-%d %H:%M")
        except Exception:
            return None
    sorted_preds = [
        _p for _p, _k in sorted(
            ((p, _kickoff_key(p)) for p in predictions),
            key=lambda pk: (pk[1] is None, pk[1] or _dtm.min, _match_sort_key(pk[0])),
        )
    ]

    # 联赛筛选导航 + 体彩快筛/排序栏 + 齐整看板容器
    from collections import Counter
    league_counts = Counter(p.get("competition", "其他") for p in sorted_preds)
    nav_html = ""
    if len(league_counts) > 1:
        nav_html += '<div class="league-nav">'
        nav_html += '<button class="league-btn active" data-league="all">全部</button>'
        if value_bets:
            nav_html += f'<button class="league-btn" data-league="value">⭐ 价值注<span class="cnt">{len(value_bets)}</span></button>'
        for lg, cnt in league_counts.most_common():
            nav_html += f'<button class="league-btn" data-league="{_slug(lg)}">{lg}<span class="cnt">{cnt}</span></button>'
        nav_html += '</div>'

    def _is_pred_settled(p):
        if not results_map:
            return False
        _mid = p.get("match_id", "")
        _r = results_map.get(_mid)
        if not _r and p.get("home_team") and p.get("away_team"):
            _r = results_map.get(f"{p['home_team']}_vs_{p['away_team']}")
        return bool(_r and _r.get("home_score") is not None)

    board_rows = ""
    n_single = n_ev = n_steady = n_risk = n_settled = 0
    for global_idx, p in enumerate(sorted_preds):
        lg = p.get("competition", "其他")
        _is_v = _is_value(p, value_matches)
        _sk = _match_sort_key(p)
        _num_ord = _sk[0] * 1000 + _sk[1]
        _bev, _, _ = _best_match_ev(p)
        _mp = max((p.get("home_win_prob") or 0), (p.get("draw_prob") or 0), (p.get("away_win_prob") or 0))
        _single_f = bool(p.get("had_single") or p.get("hhad_single"))
        _risk_f = bool(_collect_match_risks(p) or p.get("league_forbidden"))
        _settled_f = _is_pred_settled(p)
        _ev_f = bool(_is_v or _bev >= 0.01 or (p.get("play_ev") or {}))
        _steady_f = bool(_mp >= 0.58 and not _risk_f)

        n_single += int(_single_f)
        n_ev += int(_ev_f)
        n_steady += int(_steady_f)
        n_risk += int(_risk_f)
        n_settled += int(_settled_f)

        board_rows += (
            f'<div class="league-section" data-league="{_slug(lg)}"'
            f' data-value="{"1" if _is_v else "0"}"'
            f' data-time-order="{global_idx}"'
            f' data-num-order="{_num_ord}"'
            f' data-best-ev="{_bev:.4f}"'
            f' data-max-prob="{_mp:.4f}"'
            f' data-single="{"1" if _single_f else "0"}"'
            f' data-risk="{"1" if _risk_f else "0"}"'
            f' data-settled="{"1" if _settled_f else "0"}"'
            f' data-has-ev="{"1" if _ev_f else "0"}">'
        )
        board_rows += _match_card(p, value_matches, global_idx, results_map)
        board_rows += '</div>'

    if board_rows:
        _settled_btn = f'<button type="button" class="qf-btn" data-qf="settled">已完赛<span class="cnt">{n_settled}</span></button>' if n_settled else ""
        _toolbar_html = (
            f'<div class="board-toolbar">'
            f'<div class="qf-group">'
            f'<button type="button" class="qf-btn active" data-qf="all">全部<span class="cnt">{len(sorted_preds)}</span></button>'
            f'<button type="button" class="qf-btn" data-qf="single">单关<span class="cnt">{n_single}</span></button>'
            f'<button type="button" class="qf-btn" data-qf="ev">正EV/全玩法<span class="cnt">{n_ev}</span></button>'
            f'<button type="button" class="qf-btn" data-qf="steady">高稳胆<span class="cnt">{n_steady}</span></button>'
            f'<button type="button" class="qf-btn" data-qf="risk">防冷避坑<span class="cnt">{n_risk}</span></button>'
            f'{_settled_btn}'
            f'</div>'
            f'<div class="sort-group">'
            f'<button type="button" class="sort-btn active" data-sort="time">按时间</button>'
            f'<button type="button" class="sort-btn" data-sort="num">按编号</button>'
            f'<button type="button" class="sort-btn" data-sort="ev">按EV</button>'
            f'</div>'
            f'</div>'
        )
        cards = (
            f'{nav_html}'
            f'{_toolbar_html}'
            f'<div class="match-board" id="match-board">'
            f'<div class="match-board-head">'
            f'<div>时间 / 编号</div>'
            f'<div>对阵与预期</div>'
            f'<div style="text-align:center">胜平负 · 让球（点击选号串关）</div>'
            f'<div>主推结论</div>'
            f'<div></div>'
            f'</div>'
            f'{board_rows}'
            f'</div>'
        )
    else:
        cards = ""

    focus_bar_html = _sporttery_focus_bar(sorted_preds, results_map)
    parlay_cal = (ticket.get("parlay_calibration") if isinstance(ticket, dict) else None) or {}
    if not parlay_cal:
        try:
            from engine.strategy.parlay import load_calibration
            parlay_cal = load_calibration(ROOT / "data" / "state" / "review_ledger.jsonl") or {}
        except Exception:
            parlay_cal = {}

    # 三票方案 / 串关 / 比分串（2026-08-30: 传入结算索引, 推荐卡直接标注中/未中）
    # 注意: _ps 正式加载在下方复盘区块, 此处自包含提前读（幂等读文件, 开销可忽略）
    _ps_day = {}
    try:
        _ps_early = json.loads((ROOT / "data" / "state" / "parlay_settle.json").read_text(encoding="utf-8"))
        _ps_day = (_ps_early.get("by_date") or {}).get(today, {})
    except Exception:
        _ps_day = {}
    _st_index = _settle_index(_ps_day)
    ticket_html = _ticket_section(ticket, predictions, _st_index)
    parlay_html = _parlay_section(ticket, predictions, _st_index)
    score_parlay_html = _score_parlay_section(ticket, _st_index)
    # 串关/波胆真实复盘（2026-08-10：真实出票结算）
    try:
        _ps_path = ROOT / "data" / "state" / "parlay_settle.json"
        _ps = json.loads(_ps_path.read_text(encoding="utf-8")) if _ps_path.exists() else {}
    except Exception:
        _ps = {}
    parlay_settle_html = _parlay_settle_section(_ps, today)
    news_html = _news_section(today)

    # 赛果复盘（优先用 results.json，fallback review_ledger）
    results_html = _results_section(results, results_preds or predictions, review_ledger, today)

    # 系统面板
    system_html = _system_panel(breaker, bundle, tier, breaker_mult, tier_reason)
    parlay_list = ticket.get("parlay", []) if isinstance(ticket, dict) else []

    # 全部日期按月分组（2026-08-30 日期导航折叠面板）
    _months = {}
    for _d in (all_dates or []):
        _months.setdefault(_d[:7], []).append(_d)
    _date_all_html = "".join(
        f'<div class="dam-month"><div class="dam-label">{m}</div><div class="dam-links">'
        + "".join(f'<a href="{d}.html" class="date-btn {"active" if d == today else ""}">{d[5:]}</a>' for d in ds)
        + "</div></div>"
        for m, ds in sorted(_months.items(), reverse=True)
    )

    health_badge = _health_badge()

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>足球预测 - {today}</title>
<style>
:root {{
  --bg:#f8fafc; --surface:#ffffff; --card:var(--surface); --surface2:#f1f5f9; --surface3:#e2e8f0;
  --border:#e2e8f0; --border-light:#cbd5e1;
  --text:#0f172a; --text-secondary:#475569; --dim:#94a3b8;
  --blue:#2563eb; --red:#be123c; --green:#15803d; --amber:#b45309; --cyan:#0e7490;
  --purple:#6d28d9; --teal:#0f766e;
  --blue-dim:rgba(37,99,235,.08); --red-dim:rgba(190,18,60,.08);
  --green-dim:rgba(21,128,61,.09); --amber-dim:rgba(180,83,9,.09);
  --purple-dim:rgba(109,40,217,.08);
  --radius:12px; --radius-sm:8px;
  --shadow:0 1px 2px rgba(15,23,42,.04);
  --shadow-lift:0 4px 12px rgba(15,23,42,.06);
  --transition:all .15s ease;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
html {{ scroll-behavior:smooth; }}
body {{
  font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
  background:var(--bg); color:var(--text); line-height:1.55;
  -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale;
}}
.tabular, table, .m-meta, .m-team-stat, .m-p-val, .m-p-odds, .sum-val, .ti-odds, .ob-val {{
  font-variant-numeric:tabular-nums;
}}
a {{ color:var(--blue); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.page {{ max-width:1040px; margin:0 auto; padding:0 16px 52px; }}

/* ===== STICKY TOPBAR ===== */
.topbar {{
  position:sticky; top:0; z-index:60;
  display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;
  padding:12px 0; margin-bottom:12px;
  background:rgba(248,250,252,.92); backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
  border-bottom:1px solid var(--border);
}}
.brand {{ display:flex; align-items:center; gap:8px; }}
.brand h1 {{ font-size:1.05rem; font-weight:800; letter-spacing:-0.02em; color:var(--text); }}
.brand .date-pill {{
  font-size:0.72rem; font-weight:600; color:var(--text-secondary);
  background:var(--surface2); border:1px solid var(--border); border-radius:6px; padding:2px 8px;
  font-variant-numeric:tabular-nums;
}}
.seg-tabs {{
  display:inline-flex; gap:2px; padding:3px;
  background:var(--surface2); border:1px solid var(--border); border-radius:10px;
}}
.page-tab-btn {{
  padding:6px 14px; border:none; border-radius:7px; background:transparent;
  color:var(--text-secondary); font-size:0.8rem; font-weight:600; cursor:pointer;
  transition:var(--transition); font-family:inherit;
}}
.page-tab-btn:hover {{ color:var(--text); }}
.page-tab-btn.active {{ background:var(--surface); color:var(--text); font-weight:700; box-shadow:var(--shadow); }}
.page-tab-panel {{ display:none; }}
.page-tab-panel.active {{ display:block; }}
.topbar-right {{ display:flex; align-items:center; gap:8px; }}
.nav-link {{
  color:var(--text-secondary); font-size:0.75rem; font-weight:600;
  padding:6px 12px; border:1px solid var(--border); border-radius:8px; background:var(--surface);
  transition:var(--transition); white-space:nowrap; display:inline-flex; align-items:center; gap:4px;
}}
.nav-link:hover {{ color:var(--blue); border-color:var(--blue); text-decoration:none; }}
.badge {{
  display:inline-flex; align-items:center; gap:5px; padding:5px 10px;
  border-radius:8px; font-size:0.68rem; font-weight:700;
}}
.badge::before {{ content:''; width:6px; height:6px; border-radius:50%; }}
.badge.ok {{ background:var(--green-dim); color:var(--green); border:1px solid rgba(21,128,61,.25); }}
.badge.ok::before {{ background:var(--green); }}
.badge.warn {{ background:var(--amber-dim); color:var(--amber); border:1px solid rgba(180,83,9,.25); }}
.badge.warn::before {{ background:var(--amber); }}

/* ===== DATE NAV ===== */
.date-nav {{ display:flex; align-items:center; gap:6px; margin-bottom:14px; }}
.date-nav-scroll {{ flex:1; display:flex; gap:5px; overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:none; }}
.date-nav-scroll::-webkit-scrollbar {{ display:none; }}
.date-btn {{
  padding:4px 11px; border-radius:7px; font-size:0.74rem; font-weight:600;
  color:var(--text-secondary); background:var(--surface); border:1px solid var(--border);
  text-decoration:none; white-space:nowrap; transition:var(--transition); font-variant-numeric:tabular-nums;
}}
.date-btn:hover {{ border-color:var(--blue); color:var(--blue); text-decoration:none; }}
.date-btn.active {{ color:#fff; background:var(--text); border-color:var(--text); }}
.date-btn.active:hover {{ color:#fff; }}
.date-jump {{
  display:inline-flex; align-items:center; justify-content:center;
  background:var(--surface); border:1px solid var(--border); border-radius:7px;
  width:30px; height:28px; cursor:pointer; font-size:0.8rem; flex:none;
}}
.date-jump:hover {{ border-color:var(--blue); }}
.date-jump input {{ position:absolute; width:1px; height:1px; opacity:0; border:none; padding:0; pointer-events:none; }}
.date-all {{ position:relative; display:inline-block; }}
.date-all > summary {{ list-style:none; cursor:pointer; display:inline-flex; align-items:center; }}
.date-all > summary::-webkit-details-marker {{ display:none; }}
.date-all[open] > summary {{ background:var(--text); border-color:var(--text); color:#fff; }}
.date-all-panel {{
  position:absolute; z-index:70; top:calc(100% + 6px); right:0;
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  box-shadow:var(--shadow-lift); padding:12px 14px; max-height:65vh; overflow-y:auto;
  width:max-content; max-width:88vw;
}}
.dam-month {{ margin-bottom:10px; }}
.dam-month:last-child {{ margin-bottom:0; }}
.dam-label {{ font-size:0.68rem; color:var(--dim); font-weight:700; margin-bottom:4px; }}
.dam-links {{ display:flex; flex-wrap:wrap; gap:5px; }}

/* ===== FLAT HAIRLINE SUMMARY STRIP ===== */
.summary-wrap {{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  box-shadow:var(--shadow); overflow:hidden; margin-bottom:20px;
}}
.summary-strip {{ display:grid; grid-template-columns:repeat(5, minmax(0, 1fr)); }}
.sum-cell {{ padding:13px 15px; border-right:1px solid var(--border); min-width:0; }}
.sum-cell:last-child {{ border-right:none; }}
.sum-label {{ font-size:0.68rem; color:var(--dim); font-weight:600; }}
.sum-val {{ font-size:1.25rem; font-weight:800; margin-top:3px; letter-spacing:-0.02em; }}
.sum-sub {{ font-size:0.68rem; color:var(--text-secondary); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.summary-foot {{
  display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;
  padding:8px 15px; border-top:1px solid var(--border); background:var(--surface2);
  font-size:0.72rem; color:var(--text-secondary);
}}
.bb-tickets {{ display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
.bb-ticket {{ font-size:0.7rem; background:var(--surface); border:1px solid var(--border); border-radius:6px; padding:2px 8px; display:inline-flex; gap:6px; align-items:center; }}
.bb-tag {{ font-weight:700; }}
.bb-tag.stable {{ color:var(--green); }}
.bb-tag.value {{ color:var(--blue); }}
.bb-tag.lottery {{ color:var(--purple); }}
.bb-n {{ color:var(--dim); }}
.bb-empty {{ font-size:0.74rem; color:var(--text-secondary); }}
.green {{ color:var(--green); }} .amber {{ color:var(--amber); }} .red {{ color:var(--red); }} .blue {{ color:var(--blue); }}

/* ===== SECTION TITLE & FILTER NAV ===== */
.section-head {{
  display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;
  margin:22px 0 10px;
}}
.section-title {{
  font-size:0.9rem; font-weight:700; margin:24px 0 10px; color:var(--text);
  display:flex; align-items:center; gap:8px; flex-wrap:wrap;
}}
.section-head .section-title {{ margin:0; }}
.section-title .st-note {{ font-size:0.72rem; color:var(--dim); font-weight:500; }}
.league-nav {{ display:flex; flex-wrap:wrap; gap:5px; margin-bottom:10px; }}
.league-btn {{
  padding:4px 11px; border-radius:16px; font-size:0.72rem; font-weight:600;
  color:var(--text-secondary); background:var(--surface); border:1px solid var(--border);
  cursor:pointer; transition:var(--transition); white-space:nowrap; font-family:inherit;
}}
.league-btn:hover {{ border-color:var(--blue); color:var(--blue); }}
.league-btn.active {{ background:var(--text); border-color:var(--text); color:#fff; }}
.league-btn .cnt {{ font-size:0.64rem; color:var(--dim); margin-left:4px; }}
.league-btn.active .cnt {{ color:rgba(255,255,255,.72); }}

/* ===== FOTMOB-STYLE COMPACT MATCH BOARD ===== */
.match-board {{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  box-shadow:var(--shadow); overflow:hidden;
}}
.match-board-head {{
  display:grid; grid-template-columns:112px minmax(175px, 1.15fr) 264px minmax(185px, 1fr) 22px;
  gap:12px; align-items:center; padding:8px 16px;
  background:var(--surface2); border-bottom:1px solid var(--border);
  font-size:0.66rem; font-weight:700; color:var(--dim); letter-spacing:0.04em;
}}
.league-section.hidden {{ display:none; }}
.match {{ border-bottom:1px solid var(--border); transition:var(--transition); }}
.league-section:last-child .match {{ border-bottom:none; }}
.match.value-pick {{ border-left:3px solid var(--green); }}
.match-header {{
  display:grid; grid-template-columns:112px minmax(175px, 1.15fr) 264px minmax(185px, 1fr) 22px;
  gap:12px; align-items:center; padding:12px 16px; cursor:pointer; user-select:none;
}}
.match-header:hover {{ background:rgba(241,245,249,.65); }}
.match.expanded .match-header {{ background:rgba(241,245,249,.55); }}

.m-meta {{ display:flex; flex-direction:column; gap:3px; min-width:0; }}
.m-meta-top {{ display:flex; align-items:center; gap:5px; }}
.m-kickoff {{ font-size:0.78rem; font-weight:700; color:var(--text); }}
.jc-no {{
  font-size:0.64rem; font-weight:700; color:var(--text-secondary);
  background:var(--surface2); border:1px solid var(--border);
  border-radius:4px; padding:0 5px; line-height:1.5; white-space:nowrap;
}}
.league-tag {{
  font-size:0.68rem; color:var(--text-secondary); font-weight:500;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}}

.m-teams {{ display:flex; flex-direction:column; gap:3px; min-width:0; }}
.m-team-row {{ display:flex; align-items:center; justify-content:space-between; gap:8px; min-width:0; }}
.m-team-name {{
  font-size:0.86rem; font-weight:600; color:var(--text);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}}
.m-team-name.fav {{ font-weight:800; }}
.m-team-stat {{ font-size:0.72rem; color:var(--dim); font-weight:600; flex-shrink:0; }}
.m-team-stat.score {{ font-size:0.88rem; font-weight:800; color:var(--text); }}

.m-probs {{ display:flex; flex-direction:column; gap:5px; min-width:0; }}
.m-prob-cols {{ display:grid; grid-template-columns:repeat(3, 1fr); gap:4px; text-align:center; }}
.m-prob-col {{ display:flex; flex-direction:column; line-height:1.2; }}
.m-p-val {{ font-size:0.78rem; font-weight:600; color:var(--text-secondary); }}
.m-p-val.best.home {{ color:var(--blue); font-weight:800; }}
.m-p-val.best.draw {{ color:var(--text); font-weight:800; }}
.m-p-val.best.away {{ color:var(--red); font-weight:800; }}
.m-p-odds {{ font-size:0.68rem; color:var(--dim); margin-top:1px; }}
.prob-bar {{ display:flex; height:4px; border-radius:2px; overflow:hidden; background:var(--surface2); }}
.prob-seg.h {{ background:var(--blue); }}
.prob-seg.d {{ background:#94a3b8; }}
.prob-seg.a {{ background:#e11d48; }}

.m-pick {{ display:flex; flex-direction:column; gap:3px; min-width:0; }}
.m-pick-top {{ display:flex; align-items:center; gap:5px; flex-wrap:wrap; }}
.m-pick-sub {{
  font-size:0.68rem; color:var(--dim); display:flex; align-items:center; gap:6px; flex-wrap:wrap;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}}
.provenance-inline {{ color:var(--text-secondary); }}
.provenance-box {{ margin:8px 0 12px; padding:10px 12px; background:var(--surface); border:1px solid var(--border); border-radius:8px; }}
.provenance-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }}
.provenance-grid > div {{ display:flex; flex-direction:column; gap:3px; min-width:0; }}
.provenance-grid span {{ color:var(--dim); font-size:0.68rem; }}
.provenance-grid strong {{ color:var(--text); font-size:0.82rem; }}
.provenance-box p {{ margin:7px 0 0; color:var(--text-secondary); font-size:0.72rem; }}
.pick-label {{ display:none; }}
.pick-val {{
  font-size:0.78rem; font-weight:700; padding:1px 7px; border-radius:5px; display:inline-block;
}}
.pick-val.home {{ color:var(--blue); background:var(--blue-dim); }}
.pick-val.draw {{ color:var(--text-secondary); background:var(--surface2); }}
.pick-val.away {{ color:var(--red); background:var(--red-dim); }}
.value-badge {{
  font-size:0.64rem; font-weight:700; color:var(--green); background:var(--green-dim);
  padding:1px 6px; border-radius:4px; white-space:nowrap;
}}
.status-pill {{ font-size:0.64rem; font-weight:600; padding:1px 6px; border-radius:4px; white-space:nowrap; }}
.status-pill.forbid {{ background:var(--red-dim); color:var(--red); }}
.status-pill.recover {{ background:var(--amber-dim); color:var(--amber); }}
.ar-hit {{
  display:inline-flex; align-items:center; justify-content:center;
  padding:0 5px; height:18px; border-radius:9px; font-size:0.66rem; font-weight:700;
}}
.ar-hit.hit {{ background:var(--green-dim); color:var(--green); }}
.ar-hit.miss {{ background:var(--red-dim); color:var(--red); }}
.hcap-label {{
  background:var(--purple-dim); color:var(--purple); padding:1px 5px; border-radius:4px; font-weight:600; font-size:0.68rem;
}}
.pred-score {{ font-family:'SF Mono','Fira Code',monospace; font-size:0.68rem; color:var(--text-secondary); }}
.risk-pill {{
  font-size:0.64rem; font-weight:600; color:var(--amber); background:var(--amber-dim);
  border-radius:4px; padding:0 5px; cursor:help;
}}
.m-chevron {{ color:var(--dim); font-size:0.78rem; text-align:right; transition:transform .15s; }}
.match.expanded .m-chevron {{ transform:rotate(180deg); color:var(--text); }}

/* ===== EXPANDED MATCH DETAIL DRAWER ===== */
.match-detail {{ display:none; border-top:1px solid var(--border); background:var(--bg); }}
.match.expanded .match-detail {{ display:block; }}
.tab-bar {{ display:flex; gap:18px; padding:0 16px; border-bottom:1px solid var(--border); background:var(--surface); }}
.tab-btn {{
  padding:9px 2px; font-size:0.76rem; font-weight:600; color:var(--dim);
  cursor:pointer; border:none; background:none; border-bottom:2px solid transparent;
  transition:var(--transition); font-family:inherit;
}}
.tab-btn:hover {{ color:var(--text); }}
.tab-btn.active {{ color:var(--blue); border-bottom-color:var(--blue); font-weight:700; }}
.tab-content {{ display:none; padding:14px 16px; }}
.tab-content.active {{ display:block; }}

.risk-banner {{
  background:var(--amber-dim); border:1px solid rgba(180,83,9,.22); border-radius:var(--radius-sm);
  padding:8px 12px; margin-bottom:10px; font-size:0.74rem; color:var(--text); line-height:1.5;
}}
.signal-strip {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }}
.info-chip {{
  font-size:0.68rem; color:var(--text-secondary); background:var(--surface);
  border:1px solid var(--border); border-radius:5px; padding:2px 7px;
  display:inline-flex; align-items:center; gap:4px;
}}
.info-chip b {{ color:var(--text); font-weight:600; }}

.detail-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:12px; margin-top:10px; }}
.card {{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:14px 16px; margin-bottom:12px; box-shadow:var(--shadow);
}}

table {{ width:100%; border-collapse:collapse; font-size:0.76rem; }}
th {{ text-align:left; padding:7px 8px; color:var(--dim); font-weight:600; font-size:0.68rem; background:var(--surface2); border-bottom:1px solid var(--border); }}
th:first-child {{ border-radius:6px 0 0 6px; }}
th:last-child {{ border-radius:0 6px 6px 0; }}
td {{ padding:7px 8px; border-bottom:1px solid var(--border); color:var(--text-secondary); vertical-align:middle; }}
tr:last-child td {{ border-bottom:none; }}

.model-table, .edge-table {{ width:100%; border-collapse:collapse; font-size:0.76rem; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); overflow:hidden; }}
.model-table .src-label, .edge-table .src-label {{ color:var(--text); font-weight:600; white-space:nowrap; }}
.prob-bar-cell {{ width:62%; }}
.mini-bar {{ display:flex; height:18px; border-radius:4px; overflow:hidden; background:var(--surface2); }}
.mini-bar > div {{ display:flex; align-items:center; justify-content:center; font-size:0.64rem; font-weight:700; color:#fff; min-width:22px; }}
.mb-h {{ background:var(--blue); }}
.mb-d {{ background:#64748b; }}
.mb-a {{ background:#e11d48; }}

.elo-row {{ display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }}
.elo-chip {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); padding:8px 12px; flex:1; min-width:90px; }}
.elo-label {{ color:var(--dim); font-size:0.64rem; }}
.elo-val {{ font-size:1rem; font-weight:800; margin-top:2px; }}
.wilson-meter {{ margin-top:10px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); padding:8px 12px; }}
.wilson-label {{ font-size:0.68rem; color:var(--dim); margin-bottom:5px; }}
.wilson-track {{ height:7px; background:var(--surface2); border-radius:4px; overflow:hidden; }}
.wilson-fill {{ height:100%; border-radius:4px; background:linear-gradient(90deg, var(--blue), var(--green)); }}
.wilson-val {{ font-size:0.68rem; color:var(--text-secondary); margin-top:4px; }}

/* Odds tab */
.odds-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(120px, 1fr)); gap:8px; margin-bottom:10px; }}
.odds-box {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); padding:9px 11px; }}
.ob-label {{ font-size:0.64rem; color:var(--dim); }}
.ob-val {{ font-size:1rem; font-weight:700; margin-top:2px; }}
.edge-table {{ margin-top:10px; }}
.edge-pos {{ color:var(--green); font-weight:700; }}
.edge-neg {{ color:var(--red); font-weight:600; }}
.odds-meta-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:10px; margin-top:10px; }}
.reverse-box, .same-odds-box {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); padding:10px 12px; }}
.reverse-box h5 {{ font-size:0.7rem; color:var(--amber); margin-bottom:6px; }}
.same-odds-box h5 {{ font-size:0.7rem; color:var(--cyan); margin-bottom:6px; }}
.reverse-row {{ display:flex; justify-content:space-between; font-size:0.72rem; padding:3px 0; border-bottom:1px dashed var(--border); }}
.reverse-row:last-child {{ border-bottom:none; }}
.reverse-row .rk {{ color:var(--dim); }}

/* Distribution tab */
.scores-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(68px, 1fr)); gap:6px; margin-bottom:12px; }}
.score-cell {{ background:var(--surface); border:1px solid var(--border); border-radius:6px; padding:6px 4px; text-align:center; }}
.sc-score {{ font-size:0.8rem; font-weight:700; color:var(--text); }}
.sc-prob {{ font-size:0.64rem; color:var(--dim); margin-top:1px; }}
.goals-bars {{ margin:10px 0; }}
.goal-bar-row, .xg-bar-row {{ display:flex; align-items:center; gap:8px; margin-bottom:6px; }}
.goal-bar-label, .xg-bar-label {{ font-size:0.68rem; color:var(--text-secondary); width:54px; text-align:right; flex-shrink:0; }}
.goal-bar-track, .xg-bar-track {{ flex:1; height:18px; background:var(--surface2); border-radius:4px; overflow:hidden; }}
.goal-bar-fill, .xg-bar-fill {{
  height:100%; border-radius:4px; display:flex; align-items:center;
  padding-left:6px; font-size:0.64rem; font-weight:700; color:#fff;
  background:linear-gradient(90deg, #8b5cf6, var(--purple));
}}
.xg-bar-fill.home {{ background:linear-gradient(90deg, #3b82f6, var(--blue)); }}
.xg-bar-fill.away {{ background:linear-gradient(90deg, #e11d48, var(--red)); }}
.xg-compare {{ margin-top:10px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); padding:10px 12px; }}
.xg-compare h5 {{ font-size:0.7rem; color:var(--text-secondary); margin-bottom:8px; font-weight:700; }}

/* ===== TICKETS / PARLAYS ===== */
.ticket-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:10px; }}
.ticket-card {{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:14px 16px; box-shadow:var(--shadow);
}}
.ticket-card h4 {{ font-size:0.76rem; margin-bottom:8px; font-weight:700; }}
.ticket-card h4.stable {{ color:var(--green); }}
.ticket-card h4.value {{ color:var(--blue); }}
.ticket-card h4.lottery {{ color:var(--purple); }}
.ticket-item {{
  display:flex; justify-content:space-between; align-items:center; gap:8px;
  font-size:0.74rem; padding:6px 0; border-bottom:1px solid var(--border);
}}
.ticket-item:last-child {{ border-bottom:none; }}
.ti-match {{ color:var(--text-secondary); }}
.ti-odds {{ color:var(--text); font-weight:600; white-space:nowrap; }}
.tag-warn {{ background:var(--amber-dim); color:var(--amber); border-radius:4px; padding:0 4px; font-size:0.66rem; margin-left:4px; }}
.ticket-empty {{ font-size:0.74rem; color:var(--dim); padding:6px 0; }}
.ticket-summary {{ margin-top:10px; display:grid; grid-template-columns:repeat(auto-fit, minmax(110px, 1fr)); gap:6px; }}
.ts-chip {{ background:var(--surface2); border:1px solid var(--border); border-radius:6px; padding:7px 9px; text-align:center; }}
.ts-label {{ font-size:0.62rem; color:var(--dim); }}
.ts-val {{ font-size:0.88rem; font-weight:700; margin-top:1px; }}
.sp-detail summary {{ cursor:pointer; color:var(--blue); font-size:0.74rem; padding:3px 0; user-select:none; }}
.ts-row {{ display:flex; justify-content:space-between; gap:10px; padding:4px 6px; font-size:0.74rem; color:var(--text-secondary); border-bottom:1px dashed var(--border); }}
.ts-statline {{ font-size:0.78rem; padding:8px 0; color:var(--text-secondary); border-bottom:1px dashed var(--border); }}
.ts-statline b {{ color:var(--text); }}
.ts-note {{ font-size:0.72rem; color:var(--text-secondary); margin-top:6px; }}
.sp-leg {{ white-space:nowrap; margin-right:6px; }}

/* ===== REVIEW / SYSTEM / FOLDS / CTA ===== */
.results-summary {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:8px; margin-bottom:12px; }}
.stat {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); padding:10px 12px; box-shadow:var(--shadow); }}
.stat .label {{ font-size:0.68rem; color:var(--dim); }}
.stat .value {{ font-size:1.25rem; font-weight:800; margin-top:2px; }}
.results-table-wrap {{ overflow-x:auto; }}
.results-table {{
  width:100%; border-collapse:collapse; font-size:0.74rem;
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; box-shadow:var(--shadow);
}}
.results-table tr.hit td {{ background:rgba(21,128,61,.03); }}
.results-table tr.miss td {{ background:rgba(190,18,60,.03); }}
.result-icon {{ display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px; border-radius:50%; font-size:0.7rem; font-weight:800; }}
.result-icon.hit {{ background:var(--green-dim); color:var(--green); }}
.result-icon.miss {{ background:var(--red-dim); color:var(--red); }}

.sys-grid {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:10px; }}
.sys-card {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; box-shadow:var(--shadow); }}
.sys-card h4 {{ font-size:0.72rem; color:var(--dim); margin-bottom:8px; font-weight:700; }}
.sys-row {{ display:flex; justify-content:space-between; align-items:center; font-size:0.74rem; padding:4px 0; }}
.sys-row .k {{ color:var(--dim); }}
.sys-row .v {{ font-weight:600; color:var(--text); }}
.hash {{
  font-family:'SF Mono','Fira Code',monospace; font-size:0.64rem; color:var(--purple);
  word-break:break-all; margin-top:8px; padding:7px; background:var(--surface2);
  border-radius:6px; border:1px solid var(--border);
}}
.tier-indicator {{ padding:2px 7px; border-radius:4px; font-size:0.68rem; font-weight:700; }}
.tier-indicator.safe {{ background:var(--green-dim); color:var(--green); }}
.tier-indicator.caution {{ background:var(--amber-dim); color:var(--amber); }}
.tier-indicator.danger {{ background:var(--red-dim); color:var(--red); }}

.deep-fold {{ border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); margin:14px 0; overflow:hidden; box-shadow:var(--shadow); }}
.deep-fold > summary {{ list-style:none; cursor:pointer; user-select:none; display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:12px 16px; }}
.deep-fold > summary::-webkit-details-marker {{ display:none; }}
.deep-fold > summary::before {{ content:'▸'; color:var(--dim); font-size:0.8rem; transition:transform .15s; }}
.deep-fold[open] > summary::before {{ transform:rotate(90deg); }}
.deep-fold-title {{ font-weight:700; font-size:0.84rem; color:var(--text); }}
.deep-fold-meta {{ font-size:0.72rem; color:var(--dim); }}
.deep-fold-hint {{ margin-left:auto; font-size:0.68rem; color:var(--blue); font-weight:600; }}
.deep-fold[open] .deep-fold-hint {{ display:none; }}
.deep-fold-body {{ padding:6px 16px 14px; border-top:1px solid var(--border); }}

.tr-cta {{
  display:flex; align-items:center; justify-content:space-between; gap:14px; flex-wrap:wrap;
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:15px 18px; margin:20px 0 6px; box-shadow:var(--shadow);
}}
.tr-cta-title {{ font-weight:700; font-size:0.9rem; color:var(--text); }}
.tr-cta-sub {{ font-size:0.74rem; color:var(--text-secondary); margin-top:2px; }}
.tr-cta-btn {{
  background:var(--text); color:#fff; font-size:0.76rem; font-weight:600;
  padding:7px 14px; border-radius:8px; white-space:nowrap; transition:var(--transition);
}}
.tr-cta-btn:hover {{ background:var(--blue); text-decoration:none; }}
.stale-warn {{ display:inline-flex; align-items:center; gap:6px; background:var(--amber-dim); color:var(--amber); border:1px solid rgba(180,83,9,.25); border-radius:6px; padding:4px 10px; font-size:0.72rem; font-weight:600; }}

.footer {{ margin-top:38px; padding-top:16px; border-top:1px solid var(--border); font-size:0.72rem; color:var(--dim); line-height:1.7; }}
.footer .chain {{
  font-family:'SF Mono','Fira Code',monospace; font-size:0.66rem; color:var(--text-secondary);
  background:var(--surface); padding:8px 12px; border-radius:var(--radius-sm);
  border:1px solid var(--border); margin-bottom:8px; overflow-x:auto; white-space:nowrap;
}}
.footer .disclaimer {{ margin-top:4px; }}

@media (max-width:900px) {{
  .summary-strip {{ grid-template-columns:repeat(3, minmax(0, 1fr)); }}
  .sum-cell {{ border-bottom:1px solid var(--border); }}
  .sys-grid {{ grid-template-columns:1fr; }}
}}
@media (max-width:780px) {{
  .match-board-head {{ display:none; }}
  .match-header {{
    grid-template-columns:1fr auto; grid-template-areas:"meta chev" "teams pick" "probs probs"; gap:8px 10px; padding:12px 14px;
  }}
  .m-meta {{ grid-area:meta; flex-direction:row; align-items:center; flex-wrap:wrap; gap:6px; }}
  .m-chevron {{ grid-area:chev; }}
  .m-teams {{ grid-area:teams; }}
  .m-pick {{ grid-area:pick; align-items:flex-end; text-align:right; }}
  .m-pick-top, .m-pick-sub {{ justify-content:flex-end; }}
  .m-probs {{ grid-area:probs; }}
  .focus-strip {{ grid-template-columns:1fr; }}
  .board-toolbar {{ flex-direction:column; align-items:stretch; }}
}}
@media (max-width:560px) {{
  .page {{ padding:0 10px 76px; }}
  .summary-strip {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
  .topbar {{ gap:8px; }}
  .page-tab-btn {{ padding:5px 11px; font-size:0.75rem; }}
  .ev-grid {{ grid-template-columns:1fr; }}
  .slip-panel {{ left:10px; right:10px; width:auto; bottom:72px; }}
  .slip-fab {{ right:12px; bottom:14px; }}
}}

/* ===== SPORTTERY PRO WORKBENCH (2026-11-28) ===== */
.focus-strip {{
  display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:10px;
  margin:0 0 12px;
}}
.focus-col {{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:10px 12px; box-shadow:var(--shadow); display:flex; flex-direction:column; gap:4px;
  border-top:2px solid var(--blue);
}}
.focus-col.focus-ev {{ border-top-color:var(--green); }}
.focus-col.focus-stable {{ border-top-color:var(--blue); }}
.focus-col.focus-risk {{ border-top-color:var(--amber); }}
.focus-kicker {{ font-size:0.64rem; font-weight:700; color:var(--dim); letter-spacing:0.03em; display:flex; justify-content:space-between; align-items:center; }}
.focus-main {{ font-size:0.84rem; font-weight:800; color:var(--text); line-height:1.3; }}
.focus-sub {{ font-size:0.68rem; color:var(--text-secondary); }}

.board-toolbar {{
  display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;
  margin-bottom:10px;
}}
.qf-group, .sort-group {{ display:flex; align-items:center; gap:5px; flex-wrap:wrap; }}
.qf-btn, .sort-btn {{
  padding:4px 10px; border-radius:7px; font-size:0.72rem; font-weight:600;
  color:var(--text-secondary); background:var(--surface); border:1px solid var(--border);
  cursor:pointer; transition:var(--transition); font-family:inherit; display:inline-flex; align-items:center; gap:4px;
}}
.qf-btn:hover, .sort-btn:hover {{ border-color:var(--blue); color:var(--blue); }}
.qf-btn.active {{ background:var(--blue); color:#fff; border-color:var(--blue); }}
.qf-btn.active .cnt {{ opacity:0.85; color:#fff; }}
.sort-btn.active {{ background:var(--text); color:#fff; border-color:var(--text); }}

.m-meta-top {{ flex-wrap:wrap; }}
.single-tag {{
  font-size:0.6rem; font-weight:800; color:#fff; background:var(--red);
  border-radius:4px; padding:0 4px; line-height:1.45; white-space:nowrap;
}}
.cutoff-tag {{
  font-size:0.6rem; font-weight:700; color:var(--amber); background:var(--amber-dim);
  border:1px solid rgba(180,83,9,.22); border-radius:4px; padding:0 4px; line-height:1.4; white-space:nowrap;
}}
.play-ev-pill {{
  font-size:0.64rem; font-weight:700; color:var(--purple); background:var(--purple-dim);
  border-radius:4px; padding:1px 6px; white-space:nowrap;
}}

.odd-row {{ display:grid; grid-template-columns:28px repeat(3, minmax(0, 1fr)); gap:4px; align-items:stretch; }}
.mkt-tag {{
  font-size:0.64rem; font-weight:700; color:var(--text-secondary); background:var(--surface2);
  border:1px solid var(--border); border-radius:5px;
  display:flex; align-items:center; justify-content:center; padding:0 2px;
}}
.mkt-tag.hhad {{ color:var(--purple); background:var(--purple-dim); border-color:rgba(109,40,217,.2); }}
.odd-cell {{
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  padding:4px 3px; border-radius:6px; border:1px solid var(--border); background:var(--surface);
  cursor:pointer; transition:var(--transition); font-family:inherit; line-height:1.15; min-width:0;
}}
.odd-cell:hover:not(:disabled) {{ border-color:var(--blue); background:var(--blue-dim); }}
.odd-cell:disabled {{ opacity:0.45; cursor:default; }}
.odd-cell .oc-p {{ font-size:0.72rem; font-weight:700; color:var(--text-secondary); }}
.odd-cell .oc-o {{ font-size:0.66rem; color:var(--dim); margin-top:1px; font-variant-numeric:tabular-nums; }}
.odd-cell.best {{ background:var(--surface2); border-color:var(--border-light); }}
.odd-cell.best .oc-p {{ color:var(--text); font-weight:800; }}
.odd-cell.best.home .oc-p {{ color:var(--blue); }}
.odd-cell.best.away .oc-p {{ color:var(--red); }}
.odd-cell.pos-ev {{ border-color:rgba(21,128,61,.45); background:rgba(21,128,61,.05); }}
.odd-cell.pos-ev .oc-o {{ color:var(--green); font-weight:700; }}
.odd-cell.selected {{
  background:var(--text); border-color:var(--text); color:#fff; box-shadow:var(--shadow);
}}
.odd-cell.selected .oc-p, .odd-cell.selected .oc-o {{ color:#fff; font-weight:800; }}

.sc-odd {{ font-size:0.64rem; color:var(--blue); font-weight:700; margin-top:1px; }}
.score-cell.pos-ev {{ border-color:rgba(21,128,61,.45); background:rgba(21,128,61,.06); }}
.sc-ev-badge {{ font-size:0.58rem; font-weight:700; color:var(--green); margin-top:1px; }}
.tag-ev {{
  font-size:0.62rem; font-weight:700; color:var(--green); background:var(--green-dim);
  border-radius:4px; padding:0 5px; margin-left:6px; white-space:nowrap;
}}
.hafu-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(118px, 1fr)); gap:6px; margin-bottom:12px; }}
.hafu-cell {{
  background:var(--surface); border:1px solid var(--border); border-radius:6px; padding:7px 9px;
  display:flex; flex-direction:column; gap:2px;
}}
.hafu-cell.pos-ev {{ border-color:rgba(21,128,61,.45); background:rgba(21,128,61,.05); }}
.hafu-top {{ display:flex; justify-content:space-between; align-items:center; font-size:0.74rem; font-weight:700; color:var(--text); }}
.hafu-sub {{ display:flex; justify-content:space-between; align-items:center; font-size:0.66rem; color:var(--dim); }}

.slip-fab {{
  position:fixed; right:20px; bottom:20px; z-index:90;
  display:none; align-items:center; gap:8px; padding:10px 16px;
  border-radius:999px; border:none; background:var(--text); color:#fff;
  font-size:0.78rem; font-weight:700; cursor:pointer; box-shadow:var(--shadow-lift);
  font-family:inherit; transition:var(--transition);
}}
.slip-fab.visible {{ display:inline-flex; }}
.slip-fab:hover {{ background:var(--blue); }}
.slip-fab-cnt {{
  background:var(--green); color:#fff; border-radius:999px; padding:1px 7px; font-size:0.7rem; font-weight:800;
}}
.slip-panel {{
  position:fixed; right:20px; bottom:74px; z-index:95; width:360px; max-height:76vh;
  display:none; flex-direction:column;
  background:var(--surface); border:1px solid var(--border-light); border-radius:var(--radius);
  box-shadow:var(--shadow-lift); overflow:hidden;
}}
.slip-panel.open {{ display:flex; }}
.slip-head {{
  display:flex; align-items:center; justify-content:space-between; gap:8px;
  padding:10px 14px; background:var(--surface2); border-bottom:1px solid var(--border);
}}
.slip-title {{ font-size:0.82rem; font-weight:800; color:var(--text); }}
.slip-head-actions {{ display:flex; gap:6px; }}
.slip-mini-btn {{
  padding:3px 8px; border-radius:5px; border:1px solid var(--border); background:var(--surface);
  color:var(--text-secondary); font-size:0.68rem; font-weight:600; cursor:pointer; font-family:inherit;
}}
.slip-mini-btn:hover {{ border-color:var(--blue); color:var(--blue); }}
.slip-body {{ padding:10px 14px; overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:8px; }}
.slip-match {{
  border:1px solid var(--border); border-radius:var(--radius-sm); padding:8px 10px; background:var(--bg);
}}
.slip-match-head {{ display:flex; justify-content:space-between; align-items:center; gap:6px; margin-bottom:5px; }}
.slip-match-name {{ font-size:0.74rem; font-weight:700; color:var(--text); }}
.slip-chips {{ display:flex; flex-wrap:wrap; gap:5px; }}
.slip-chip {{
  display:inline-flex; align-items:center; gap:4px; padding:3px 7px; border-radius:5px;
  background:var(--surface); border:1px solid var(--border); font-size:0.68rem; font-weight:600; color:var(--text);
  cursor:pointer; font-family:inherit;
}}
.slip-chip:hover {{ border-color:var(--red); color:var(--red); }}
.slip-warn {{
  font-size:0.68rem; color:var(--amber); background:var(--amber-dim); border-radius:5px; padding:5px 8px; font-weight:600;
}}
.slip-foot {{
  padding:10px 14px; border-top:1px solid var(--border); background:var(--surface2);
  display:flex; flex-direction:column; gap:8px;
}}
.slip-metrics {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:6px; }}
.slip-metric {{ background:var(--surface); border:1px solid var(--border); border-radius:6px; padding:6px 8px; }}
.slip-metric .k {{ font-size:0.62rem; color:var(--dim); }}
.slip-metric .v {{ font-size:0.8rem; font-weight:800; color:var(--text); font-variant-numeric:tabular-nums; }}
.slip-ctrl {{ display:flex; align-items:center; justify-content:space-between; gap:8px; }}
.slip-mult-wrap {{ display:flex; align-items:center; gap:6px; font-size:0.72rem; font-weight:600; color:var(--text-secondary); }}
.slip-mult-input {{
  width:56px; padding:4px 6px; border:1px solid var(--border); border-radius:6px;
  text-align:center; font-weight:700; font-size:0.76rem; background:var(--surface); color:var(--text);
}}
.slip-copy-btn {{
  flex:1; padding:7px 12px; border:none; border-radius:7px; background:var(--blue); color:#fff;
  font-size:0.74rem; font-weight:700; cursor:pointer; font-family:inherit;
}}
.slip-copy-btn:hover {{ opacity:0.92; }}
</style>
</head>
<body>
<div class="page">
  <!-- STICKY TOPBAR -->
  <div class="topbar">
    <div class="brand">
      <h1>足球预测</h1>
      <span class="date-pill">{today}</span>
    </div>
    <div class="seg-tabs">
      <button class="page-tab-btn active" data-panel="tab-decision">今日决策</button>
      <button class="page-tab-btn" data-panel="tab-review">当日复盘</button>
    </div>
    <div class="topbar-right">
      <a href="track-record.html" class="nav-link">全量战绩 ↗</a>
      {health_badge}
    </div>
  </div>

  <!-- DATE NAV -->
  <div class="date-nav">
    <div class="date-nav-scroll">
    {''.join(f'<a href="{d}.html" class="date-btn {"active" if d == today else ""}">{d[5:]}</a>' for d in (all_dates or [today])[:7])}
    </div>
    <label class="date-jump" title="选择日期，自动跳到最接近的有数据日期" id="date-jump-label">
      <span>📅</span>
      <input type="date" id="date-jump" min="{(all_dates or [today])[-1]}" max="{(all_dates or [today])[0]}" value="{today}">
    </label>
    <details class="date-all">
      <summary class="date-btn">全部 ▾</summary>
      <div class="date-all-panel">{_date_all_html}</div>
    </details>
  </div>

  <!-- UNIFIED DECISION SUMMARY -->
  {_battle_board(ticket, predictions, parlay_list, breaker_tier=tier, breaker_mult=breaker_mult)}

  <!-- TAB: 今日决策 -->
  <div class="page-tab-panel active" id="tab-decision">
    {focus_bar_html}
    <div class="section-head">
      <div class="section-title">今日赛程与预测 <span class="st-note">点击胜平负/让球格子选号串关 · 点击空白处展开详情</span></div>
    </div>
    {cards if cards else '<div class="card" style="color:var(--dim);padding:36px;text-align:center;font-size:0.84rem;">等待每日流水线运行...</div>'}

    {f'''<details class="deep-fold">
      <summary>
        <span class="deep-fold-title">📰 赛前资讯情报（仅参考 · 不入模型）</span>
        <span class="deep-fold-hint">点击展开 ▾</span>
      </summary>
      <div class="deep-fold-body">{news_html}</div>
    </details>''' if news_html else ''}

    {ticket_html}
    {parlay_html}
    {score_parlay_html}
  </div>

  <!-- TAB: 当日复盘 -->
  <div class="page-tab-panel" id="tab-review">
    {results_html if results_html else '<div class="card" style="padding:18px;color:var(--text-secondary);font-size:0.84rem">⏳ 当日比赛尚未完赛或待回溯结算，赛果入账后自动更新。</div>'}

    {parlay_settle_html}

    {f'''<details class="deep-fold">
      <summary>
        <span class="deep-fold-title">水位监控</span>
        <span class="deep-fold-meta">时间序列累积：每次抓取追加快照，赛前资金流可查</span>
        <span class="deep-fold-hint">点击展开 ▾</span>
      </summary>
      <div class="deep-fold-body">{odds_series_html}</div>
    </details>''' if odds_series_html else ''}

    {system_html}

    <div class="tr-cta">
      <div>
        <div class="tr-cta-title">📈 全量历史战绩与模型诊断</div>
        <div class="tr-cta-sub">v2 vs 市场配对检验 · 进化仪表盘 · 联赛分层 · 高置信反向样本 · 每周回测已集中至 Track Record</div>
      </div>
      <a href="track-record.html" class="tr-cta-btn">查看全量预测 →</a>
    </div>
  </div>

  <!-- FOOTER -->
  <div class="footer">
    <div class="chain">{_pipeline_desc()}</div>
    <p>数据源: 体彩 / 新浪 / 500万 / DJYY &middot; 零服务器 GitHub Actions &middot; <a href="https://github.com/wlrwx/football-engine">源代码</a></p>
    <p class="disclaimer">仅供研究学习，不构成任何投注建议。模型输出为概率估计，不保证准确性。</p>
  </div>

  <button type="button" id="slip-fab" class="slip-fab">🎫 串关计算器 <span class="slip-fab-cnt" id="slip-fab-cnt">0场</span></button>
  <div id="slip-panel" class="slip-panel" aria-hidden="true">
    <div class="slip-head">
      <div class="slip-title">体彩模拟串关票</div>
      <div class="slip-head-actions">
        <button type="button" class="slip-mini-btn" id="slip-clear">清空</button>
        <button type="button" class="slip-mini-btn" id="slip-close">收起</button>
      </div>
    </div>
    <div class="slip-body" id="slip-body"></div>
    <div class="slip-foot" id="slip-foot">
      <div class="slip-metrics" id="slip-metrics"></div>
      <div class="slip-ctrl">
        <label class="slip-mult-wrap">倍数 <input type="number" id="slip-mult" class="slip-mult-input" min="1" max="999" value="1"> 倍</label>
        <button type="button" class="slip-copy-btn" id="slip-copy">复制方案文本</button>
      </div>
    </div>
  </div>
</div>

<script>
// ===== DATE JUMP (2026-08-30: 原生日历 → 最接近的可用日期) =====
var ALL_DATES = {json.dumps(all_dates or [])};
var PARLAY_CAL = {json.dumps(parlay_cal, ensure_ascii=False)};
document.getElementById('date-jump-label').addEventListener('click', function(e) {{
  var inp = document.getElementById('date-jump');
  try {{ if (inp.showPicker) inp.showPicker(); else {{ inp.focus(); inp.click(); }} }}
  catch (err) {{ try {{ inp.focus(); }} catch (e2) {{}} }}
}});
document.getElementById('date-jump').addEventListener('change', function() {{
  var v = this.value;
  if (!v) return;
  var best = ALL_DATES[0] || v;
  var bestGap = Infinity;
  for (var i = 0; i < ALL_DATES.length; i++) {{
    var g = Math.abs(new Date(ALL_DATES[i]) - new Date(v));
    if (g < bestGap) {{ bestGap = g; best = ALL_DATES[i]; }}
  }}
  if (best) location.href = best + '.html';
}});
// 点外部收起全部日期面板
document.addEventListener('click', function(e) {{
  var d = document.querySelector('.date-all');
  if (d && !d.contains(e.target)) d.removeAttribute('open');
}});
// ===== PAGE TABS (2026-08-10 页面重构) =====
document.querySelectorAll('.page-tab-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    var panelId = this.getAttribute('data-panel');
    document.querySelectorAll('.page-tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    this.classList.add('active');
    document.querySelectorAll('.page-tab-panel').forEach(function(p) {{ p.classList.remove('active'); }});
    var panel = document.getElementById(panelId);
    if (panel) panel.classList.add('active');
    // 切换后回到页首（复盘数据在下方，避免停留在空白区）
    window.scrollTo({{ top: 0, behavior: 'smooth' }});
  }});
}});

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
    panel.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    panel.querySelectorAll('.tab-content').forEach(function(c) {{ c.classList.remove('active'); }});
    this.classList.add('active');
    panel.querySelector('#' + tabId).classList.add('active');
  }});
}});

// ===== JOINT LEAGUE + QUICK FILTER + SORT (2026-11-28) =====
var activeLeague = 'all';
var activeQf = 'all';

function applyBoardFilters() {{
  document.querySelectorAll('.league-section').forEach(function(sec) {{
    var lgOk = (activeLeague === 'all')
      || (sec.getAttribute('data-league') === activeLeague)
      || (activeLeague === 'value' && sec.getAttribute('data-value') === '1');
    var qfOk = true;
    if (activeQf === 'single') {{
      qfOk = sec.getAttribute('data-single') === '1';
    }} else if (activeQf === 'ev') {{
      qfOk = sec.getAttribute('data-has-ev') === '1';
    }} else if (activeQf === 'steady') {{
      var mp = parseFloat(sec.getAttribute('data-max-prob') || '0');
      qfOk = mp >= 0.58 && sec.getAttribute('data-risk') === '0';
    }} else if (activeQf === 'risk') {{
      qfOk = sec.getAttribute('data-risk') === '1';
    }} else if (activeQf === 'settled') {{
      qfOk = sec.getAttribute('data-settled') === '1';
    }}
    if (lgOk && qfOk) {{
      sec.classList.remove('hidden');
    }} else {{
      sec.classList.add('hidden');
    }}
  }});
}}

document.querySelectorAll('.league-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    activeLeague = this.getAttribute('data-league') || 'all';
    document.querySelectorAll('.league-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    this.classList.add('active');
    applyBoardFilters();
  }});
}});

document.querySelectorAll('.qf-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    activeQf = this.getAttribute('data-qf') || 'all';
    document.querySelectorAll('.qf-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    this.classList.add('active');
    applyBoardFilters();
  }});
}});

document.querySelectorAll('.sort-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    var mode = this.getAttribute('data-sort') || 'time';
    document.querySelectorAll('.sort-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    this.classList.add('active');
    var board = document.getElementById('match-board');
    if (!board) return;
    var items = Array.prototype.slice.call(board.querySelectorAll('.league-section'));
    items.sort(function(a, b) {{
      if (mode === 'num') {{
        return (parseInt(a.getAttribute('data-num-order') || '0', 10))
          - (parseInt(b.getAttribute('data-num-order') || '0', 10));
      }}
      if (mode === 'ev') {{
        var diff = (parseFloat(b.getAttribute('data-best-ev') || '0'))
          - (parseFloat(a.getAttribute('data-best-ev') || '0'));
        if (Math.abs(diff) > 1e-6) return diff;
      }}
      return (parseInt(a.getAttribute('data-time-order') || '0', 10))
        - (parseInt(b.getAttribute('data-time-order') || '0', 10));
    }});
    items.forEach(function(el) {{ board.appendChild(el); }});
  }});
}});

// ===== INTERACTIVE SPORTTERY PARLAY SLIP =====
var slipSelections = {{}};
var slipLastSummary = '';

function escHtml(s) {{
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function calProb(rawProb, isTop) {{
  var p = parseFloat(rawProb) || 0;
  if (!p || !PARLAY_CAL || !PARLAY_CAL.table || !isTop) return p;
  var tbl = PARLAY_CAL.table;
  var keys = Object.keys(tbl);
  for (var i = 0; i < keys.length; i++) {{
    var lo = parseFloat(keys[i]);
    var hi = lo >= 0.70 ? 1.01 : lo + 0.05;
    if (p >= lo - 1e-6 && p < hi) {{
      return Math.max(0.03, Math.min(0.96, parseFloat(tbl[keys[i]]) || p));
    }}
  }}
  if (PARLAY_CAL.overall) {{
    return Math.min(p, Math.max(p * 0.92, parseFloat(PARLAY_CAL.overall)));
  }}
  return p;
}}

function syncOddCellClasses() {{
  document.querySelectorAll('.odd-cell').forEach(function(btn) {{
    var k = btn.getAttribute('data-mid') + '|' + btn.getAttribute('data-mkt') + '|' + btn.getAttribute('data-sel');
    if (slipSelections[k]) btn.classList.add('selected');
    else btn.classList.remove('selected');
  }});
}}

function renderSlip() {{
  syncOddCellClasses();
  var fab = document.getElementById('slip-fab');
  var panel = document.getElementById('slip-panel');
  var fabCnt = document.getElementById('slip-fab-cnt');
  var body = document.getElementById('slip-body');
  var metrics = document.getElementById('slip-metrics');
  var multInp = document.getElementById('slip-mult');
  if (!fab || !panel || !body || !metrics) return;

  var grouped = {{}};
  var matchOrder = [];
  Object.keys(slipSelections).forEach(function(k) {{
    var s = slipSelections[k];
    if (!grouped[s.mid]) {{
      grouped[s.mid] = [];
      matchOrder.push(s.mid);
    }}
    grouped[s.mid].push(s);
  }});

  var mCount = matchOrder.length;
  fabCnt.textContent = mCount + '场';
  if (mCount === 0) {{
    fab.classList.remove('visible');
    panel.classList.remove('open');
    body.innerHTML = '';
    metrics.innerHTML = '';
    slipLastSummary = '';
    return;
  }}
  fab.classList.add('visible');

  var mult = Math.max(1, parseInt(multInp.value || '1', 10) || 1);
  var combos = 1;
  var minOdds = 1;
  var maxOdds = 1;
  var expUnitSum = 1;
  var ticketHitProb = 1;
  var hasNonSingle = false;
  var hasMixedMarket = false;
  var bodyHtml = '';
  var summaryParts = [];

  matchOrder.forEach(function(mid) {{
    var items = grouped[mid];
    combos *= items.length;
    var mkts = {{}};
    var mMin = Infinity;
    var mMax = 0;
    var mExp = 0;
    var mHit = 0;
    var chipsHtml = '';
    var txtParts = [];
    items.forEach(function(s) {{
      mkts[s.mkt] = true;
      if (s.single !== '1') hasNonSingle = true;
      if (s.odd < mMin) mMin = s.odd;
      if (s.odd > mMax) mMax = s.odd;
      var cp = calProb(s.prob, s.isTop === '1');
      mExp += cp * s.odd;
      mHit += cp;
      var prefix = s.mkt === 'hhad' ? (s.mktLabel + '·') : '';
      chipsHtml += '<button type="button" class="slip-chip" data-slip-key="' + escHtml(s.key) + '">'
        + escHtml(prefix + s.selLabel + ' ' + s.odd.toFixed(2)) + ' ×</button>';
      txtParts.push(prefix + s.selLabel + s.odd.toFixed(2));
    }});
    if (Object.keys(mkts).length > 1) hasMixedMarket = true;
    minOdds *= mMin;
    maxOdds *= mMax;
    expUnitSum *= mExp;
    ticketHitProb *= Math.min(0.95, mHit);

    var headText = (items[0].jc ? (items[0].jc + ' · ') : '') + items[0].match;
    bodyHtml += '<div class="slip-match">'
      + '<div class="slip-match-head"><span class="slip-match-name">' + escHtml(headText) + '</span></div>'
      + '<div class="slip-chips">' + chipsHtml + '</div>'
      + '</div>';
    summaryParts.push((items[0].jc || items[0].match) + '[' + txtParts.join('/') + ']');
  }});

  if (mCount === 1 && hasNonSingle) {{
    bodyHtml += '<div class="slip-warn">⚠ 所选包含非单关玩法，体彩至少需 2 场串关</div>';
  }}
  if (hasMixedMarket) {{
    bodyHtml += '<div class="slip-warn">⚠ 同场同时选胜平负与让球不可混串，出票需拆单</div>';
  }}
  if (mCount > 8) {{
    bodyHtml += '<div class="slip-warn">⚠ 已超体彩过关 8 场上限</div>';
  }}
  body.innerHTML = bodyHtml;

  body.querySelectorAll('.slip-chip').forEach(function(chip) {{
    chip.addEventListener('click', function() {{
      var key = this.getAttribute('data-slip-key');
      delete slipSelections[key];
      renderSlip();
    }});
  }});

  var cost = 2 * combos * mult;
  var minRet = 2 * mult * minOdds;
  var maxRet = 2 * mult * maxOdds;
  var expRet = 2 * mult * expUnitSum;
  var calEv = cost > 0 ? (expRet / cost - 1) : 0;
  var passLabel = mCount === 1 ? '单关' : (mCount + '串1');
  var oddsText = Math.abs(maxOdds - minOdds) < 0.01 ? minOdds.toFixed(2) : (minOdds.toFixed(2) + '~' + maxOdds.toFixed(2));
  var retText = Math.abs(maxRet - minRet) < 0.05 ? ('¥' + minRet.toFixed(2)) : ('¥' + minRet.toFixed(2) + '~' + maxRet.toFixed(2));
  var evColor = calEv >= 0 ? 'var(--green)' : 'var(--red)';

  metrics.innerHTML = ''
    + '<div class="slip-metric"><div class="k">' + passLabel + ' · 组合与投入</div><div class="v">' + combos + '注 × ' + mult + '倍 = ¥' + cost + '</div></div>'
    + '<div class="slip-metric"><div class="k">总赔率 / 预计奖金</div><div class="v">' + oddsText + ' · ' + retText + '</div></div>'
    + '<div class="slip-metric"><div class="k">校准命中率参考</div><div class="v">' + (Math.min(0.99, ticketHitProb) * 100).toFixed(1) + '%</div></div>'
    + '<div class="slip-metric"><div class="k">校准预期 EV</div><div class="v" style="color:' + evColor + '">' + (calEv >= 0 ? '+' : '') + (calEv * 100).toFixed(1) + '%</div></div>';

  slipLastSummary = summaryParts.join(' × ') + ' | ' + passLabel + ' · ' + combos + '注×' + mult + '倍=¥' + cost + ' · 奖金' + retText;
}}

document.querySelectorAll('.odd-cell').forEach(function(btn) {{
  btn.addEventListener('click', function(e) {{
    e.stopPropagation();
    if (this.disabled) return;
    var odd = parseFloat(this.getAttribute('data-odd') || '0');
    if (!odd || odd <= 1.0) return;
    var mid = this.getAttribute('data-mid') || '';
    var mkt = this.getAttribute('data-mkt') || 'had';
    var sel = this.getAttribute('data-sel') || '';
    var key = mid + '|' + mkt + '|' + sel;
    if (slipSelections[key]) {{
      delete slipSelections[key];
    }} else {{
      slipSelections[key] = {{
        key: key,
        mid: mid,
        jc: this.getAttribute('data-jc') || '',
        match: this.getAttribute('data-match') || '',
        mkt: mkt,
        mktLabel: this.getAttribute('data-mkt-label') || '',
        sel: sel,
        selLabel: this.getAttribute('data-sel-label') || '',
        odd: odd,
        prob: parseFloat(this.getAttribute('data-prob') || '0'),
        isTop: this.getAttribute('data-is-top') || '0',
        single: this.getAttribute('data-single') || '0'
      }};
      var panel = document.getElementById('slip-panel');
      if (panel && Object.keys(slipSelections).length === 1) {{
        panel.classList.add('open');
      }}
    }}
    renderSlip();
  }});
}});

(function() {{
  var fab = document.getElementById('slip-fab');
  var panel = document.getElementById('slip-panel');
  var clearBtn = document.getElementById('slip-clear');
  var closeBtn = document.getElementById('slip-close');
  var multInp = document.getElementById('slip-mult');
  var copyBtn = document.getElementById('slip-copy');
  if (fab && panel) {{
    fab.addEventListener('click', function() {{ panel.classList.toggle('open'); }});
  }}
  if (closeBtn && panel) {{
    closeBtn.addEventListener('click', function() {{ panel.classList.remove('open'); }});
  }}
  if (clearBtn) {{
    clearBtn.addEventListener('click', function() {{ slipSelections = {{}}; renderSlip(); }});
  }}
  if (multInp) {{
    multInp.addEventListener('input', renderSlip);
  }}
  if (copyBtn) {{
    copyBtn.addEventListener('click', function() {{
      if (!slipLastSummary) return;
      var btnEl = this;
      var done = function() {{
        var prev = btnEl.textContent;
        btnEl.textContent = '已复制 ✓';
        setTimeout(function() {{ btnEl.textContent = prev; }}, 1400);
      }};
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(slipLastSummary).then(done, done);
      }} else {{
        done();
      }}
    }});
  }}
}})();
</script>
</body>
</html>"""


def _odds_movement_chip(p):
    """赔率变动信号标签（初盘→即时盘变化对比）"""
    sina = p.get("sina_odds")
    if not sina:
        return ""
    ini = sina.get("initial_odds") or {}
    cur = sina.get("current_odds") or {}
    mv = sina.get("movement") or {}
    # 生成 初盘→即时盘 对比（变化超过 3% 才显示）
    arrows = []
    colors = {"down": "↓", "up": "↑", "flat": "→"}
    _names = {"home": "主", "draw": "平", "away": "客"}
    for sel in ["home", "draw", "away"]:
        a, b = ini.get(sel), cur.get(sel)
        if not a or not b or a <= 0 or b <= 0:
            continue
        chg = (b - a) / a
        if abs(chg) >= 0.03:
            direction = mv.get(sel, "up" if chg > 0 else "down")
            arrows.append(f"{_names[sel]}{a:.2f}→{b:.2f}{colors.get(direction,'→')}")
    if not arrows:
        return ""
    return f'<span class="info-chip" style="color:var(--purple)">赔率变动 {" ".join(arrows)}</span>'


def _odds_series_chip(p):
    """水位时间序列标签（2026-08-05 盘口系统修复）：展示累积快照与近期资金流"""
    sina = p.get("sina_odds")
    if not sina:
        return ""
    ser = sina.get("series") or {}
    pts = ser.get("points", 0)
    if pts < 2:
        return ""
    parts = [f"水位监控 {pts}次"]
    for side, key in (("主", "recent_home"), ("客", "recent_away")):
        v = ser.get(key)
        if v is not None and abs(v) >= 0.5:
            arrow = "↓" if v < 0 else "↑"
            parts.append(f"{side}{arrow}{abs(v):.1f}%")
    span = ser.get("span_min")
    if span:
        parts.append(f"跨{span:.0f}min")
    return f'<span class="info-chip" style="color:var(--teal, #14b8a6)">{" · ".join(parts)}</span>'


def _prob3(value):
    if isinstance(value, dict):
        vals = [value.get(k) for k in ("home", "draw", "away")]
    elif isinstance(value, (list, tuple)) and len(value) >= 3:
        vals = list(value[:3])
    else:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return None
    return [float(v) for v in vals]


def _direction_view(value):
    probs = _prob3(value)
    if probs is None:
        return None
    idx = max(range(3), key=lambda i: probs[i])
    return idx, probs[idx]


def _format_direction_view(view):
    if view is None:
        return "—"
    return f"{('主胜', '平局', '客胜')[view[0]]} {view[1]:.0%}"


def _prediction_provenance(p):
    probs = _prob3([
        p.get("home_win_prob"), p.get("draw_prob"), p.get("away_win_prob"),
    ])
    final = _direction_view(probs)
    direction = p.get("direction")
    if probs is not None and direction in ("home", "draw", "away"):
        idx = ("home", "draw", "away").index(direction)
        final = (idx, probs[idx])
    return {
        "model": _direction_view(p.get("model_raw")),
        "market": _direction_view(p.get("market_fair")),
        "final": final,
    }


def _market_reading(p):
    views = _prediction_provenance(p)
    market, final = views["market"], views["final"]
    if market is None or final is None:
        return ""
    labels = ("主胜", "平局", "客胜")
    if final[0] != market[0]:
        return f"市场比最终倾向更看好{labels[market[0]]}；最终融合更看好{labels[final[0]]}"
    gap = (final[1] - market[1]) * 100
    if gap > 3:
        return f"最终融合比市场更看好{labels[final[0]]}，高 {gap:.0f} 个百分点"
    if gap < -3:
        return f"市场比最终融合更看好{labels[final[0]]}，高 {-gap:.0f} 个百分点"
    return "最终倾向与市场基本一致"


def _effective_weight_text(p):
    weights = p.get("fusion_effective_weights")
    if not isinstance(weights, dict):
        return ""
    vals = [float(weights.get(k) or 0) for k in ("model", "market", "djyy")]
    return f"实际权重：模型 {vals[0]:.0%} · 市场 {vals[1]:.0%} · DJYY {vals[2]:.0%}"


def _prediction_provenance_html(p):
    views = _prediction_provenance(p)
    labels = (("原始模型", "model"), ("市场", "market"), ("最终", "final"))
    cells = "".join(
        f'<div><span>{label}</span><strong>{_format_direction_view(views[key])}</strong></div>'
        for label, key in labels
    )
    reading = _market_reading(p)
    weights = _effective_weight_text(p)
    details = "".join(f'<p>{text}</p>' for text in (reading, weights) if text)
    return f'<div class="provenance-box"><div class="provenance-grid">{cells}</div>{details}</div>'


def _market_follow_summary(predictions):
    views = [_prediction_provenance(p) for p in (predictions or [])]
    paired = [v for v in views if v["market"] is not None and v["final"] is not None]
    agreements = sum(v["market"][0] == v["final"][0] for v in paired)
    weight_rows = [
        p.get("fusion_effective_weights") for p in (predictions or [])
        if isinstance(p.get("fusion_effective_weights"), dict)
    ]
    parts = []
    if paired:
        parts.append(f"最终跟随市场 {agreements}/{len(paired)} 场")
    if weight_rows:
        avg_market = sum(float(w.get("market") or 0) for w in weight_rows) / len(weight_rows)
        parts.append(f"平均市场权重 {avg_market:.0%}")
    return " · ".join(parts)


def _divergence_chip(p):
    """模型 vs 市场分歧信号：分歧大 = 模型独立判断（高信息量），分歧小 = 跟随市场"""
    model = p.get("model_raw") or {}
    market = p.get("market_fair")
    # 市场分歧候选（2026-08-05）：非模型方向但有显著正 EV 的赔率，被方向纪律拦下
    md = p.get("market_disagreement") or {}
    md_chips = []
    for sel, info in md.items():
        md_chips.append(f'{sel[0].upper()}+EV <b>{info["ev"]:.0%}</b>')
    md_html = ""
    if md_chips:
        _chips = " ".join(md_chips)
        md_html = f'<span class="info-chip" style="color:var(--amber)">市场分歧 {_chips}（纪律拦下）</span>'
    if not model or not market or len(market) < 3:
        return md_html
    _probs = [model.get("home", 0), model.get("draw", 0), model.get("away", 0)]
    div = max(abs(_probs[i] - market[i]) for i in range(3))
    if div > 0.15:
        return md_html + f'<span class="info-chip" style="color:var(--red)">模型vs市场分歧 <b>{div:.0%}</b></span>'
    if div > 0.08:
        return md_html + f'<span class="info-chip" style="color:var(--amber)">分歧 <b>{div:.0%}</b></span>'
    return md_html


def _context_chip(p):
    """比赛情境 chips（2026-08-30）：动机/裁判/伤停——有数据才显示，无数据零噪音。"""
    ctx = p.get("context") or {}
    parts = []
    st = ctx.get("stakes")
    if st:
        if isinstance(st, dict):
            a = str(st.get("home") or st.get("主") or "")[:6]
            b = str(st.get("away") or st.get("客") or "")[:6]
            if a or b:
                parts.append(f"动机 {a}|{b}")
        else:
            parts.append("动机 " + str(st)[:12])
    if ctx.get("referee"):
        parts.append("裁判 " + str(ctx["referee"])[:8])
    inj = ctx.get("injuries") or {}
    if inj.get("home_attackers") or inj.get("away_attackers"):
        parts.append(f"伤停 {inj.get('home_attackers', 0)}+{inj.get('away_attackers', 0)}")
    if not parts:
        return ""
    return f'<span class="info-chip" style="color:var(--dim)">{" · ".join(parts)}</span>'


def _fusion_trace_row(p):
    """融合归因链（2026-08-30）：本步 fusion_trace 落盘的可视化——哪几步动了概率。"""
    tr = p.get("fusion_trace") or []
    if not tr:
        return ""
    steps = " &rarr; ".join(str(t.get("step", "")) for t in tr)
    last = tr[-1].get("after") if tr else None
    _tail = ""
    if isinstance(last, (list, tuple)) and len(last) == 3:
        _tail = f'<span style="margin-left:8px">末态 H{last[0]:.3f} D{last[1]:.3f} A{last[2]:.3f}</span>'
    return (f'<div style="font-size:0.68rem;color:var(--dim);margin:0 0 6px;padding:6px 8px;'
            f'background:var(--surface2);border-radius:6px">'
            f'<b style="color:var(--text-secondary)">融合归因链</b> {steps}{_tail}</div>')


def _pred_pick(p):
    """生成明确的预测结论。

    优先用 direction 字段（含 R1 平局改判），与结算/串关口径一致；
    否则回退 argmax。避免卡片显示"主胜"而数据里 direction=draw 的矛盾。

    2026-10-13（issue #1）：合成赔率场次加「不可投注」标记。
    外部用户报"竞彩未开盘但生成胜平负推荐"——下注层其实早就拦住合成赔率
    （engine/main.py:1016，曾因此对 8 笔共 ¥67,353 押在模拟赔率上），
    selfcheck 也有告警，但**卡片上看不出差别**：读者只看到"预测 主胜 75%"，
    以为能照着买。合成赔率是 1/(final*1.05) 反推的，去水后≈模型自己的输出，
    把它佯装成市场结论才是问题所在。
    """
    d = p.get("direction") or ""
    if d in ("home", "draw", "away"):
        prob = p.get(f"{'home_win_prob' if d == 'home' else ('draw_prob' if d == 'draw' else 'away_win_prob')}") or 0
        cls = "home" if d == "home" else ("draw" if d == "draw" else "away")
        label = {"home": "主胜", "draw": "平局", "away": "客胜"}[d]
        base = f'<span class="pick-label">预测</span> <span class="pick-val {cls}">{label} {prob:.0%}</span>'
    else:
        ph = p.get("home_win_prob") or 0
        pd = p.get("draw_prob") or 0
        pa = p.get("away_win_prob") or 0
        if ph >= pd and ph >= pa:
            base = f'<span class="pick-label">预测</span> <span class="pick-val home">主胜 {ph:.0%}</span>'
        elif pd >= ph and pd >= pa:
            base = f'<span class="pick-label">预测</span> <span class="pick-val draw">平局 {pd:.0%}</span>'
        else:
            base = f'<span class="pick-label">预测</span> <span class="pick-val away">客胜 {pa:.0%}</span>'

    # 合成赔率（无真实盘口）：明确标注不可投注，避免被当成可执行建议
    if p.get("odds_synthetic"):
        base += (' <span class="pick-val" style="font-size:.75em;border-color:#e6a23c;color:#e6a23c"'
                 ' title="该场无真实盘口，赔率由模型概率反推（1/(p×1.05)）。'
                 '数学上必然负 EV，系统不会对其下注；此概率仅供算法参考，不构成投注建议">'
                 '⚠ 无盘口·不可投注</span>')
    return base


def _handicap_pick(p):
    """让球玩法预测结论（竞彩 hhad）"""
    hcap = p.get("handicap")
    if hcap is None:
        return ""
    hhp, hdp, hap = (p.get("handicap_home_prob") or 0, p.get("handicap_draw_prob") or 0, p.get("handicap_away_prob") or 0)
    if not (hhp or hdp or hap):
        return ""
    label = "主胜" if hhp >= hdp and hhp >= hap else ("平局" if hdp >= hhp and hdp >= hap else "客胜")
    prob = max(hhp, hdp, hap)
    edge = p.get("handicap_kelly_edge")
    cls = "home" if hhp >= hdp and hhp >= hap else ("draw" if hdp >= hhp and hdp >= hap else "away")
    edge_html = f' <span class="pick-val {cls}" style="font-size:.85em">让球EV {edge:+.0%}</span>' if edge is not None else ""
    sign = "+" if hcap > 0 else ""
    # 2026-08-06 UI 修复：让球盘明显化——独立标签+背景色+tooltip，与"预测（模型观点）"区分
    # 2026-08-06 v2：三方向 EV 全量展示——argmax 只显示概率最高方向，
    # 但让球平/让球主常有正 EV 藏在窄区间+高赔率里，逐个方向标出正 EV
    ev_html = ""
    _hev = p.get("handicap_ev") or {}
    _edges = _hev.get("edges") or {}
    if _edges:
        _ev_labels = {"home": "让球主", "draw": "让球平", "away": "让球客"}
        for _dir in ("home", "draw", "away"):
            _e = _edges.get(_dir)
            if _e is not None and _e > 0.005:  # 正 EV 才展示
                _ev_cls = "draw" if _dir == "draw" else _dir
                _star = " ⭐" if _dir == "draw" else ""  # 让球平正EV是隐藏价值，高亮
                # 2026-08-07：>30% edge 多为脏数据/赔率错位，sanity check 已设
                # recommended=False 不出注——页面需标注"超限"避免误读为强信号
                _over = " ⚠超限" if _e > 0.30 else ""
                ev_html += (f' <span class="pick-val {_ev_cls}" style="font-size:.78em;'
                            f'border-color:{"var(--purple)" if _dir=="draw" else {"home":"var(--green)","away":"var(--red)"}[_dir]};'
                            f'background:rgba(147,51,234,0.08)" '
                            f'title="模型概率×市场赔率-1。edge>30% 超出可信区间，系统不据此出注">'
                            f'{_ev_labels[_dir]} +EV {_e:+.0%}{_star}{_over}</span>')
    return (f'<span class="pick-label hcap-label" title="让球玩法（市场盘口观点）：主队让{hcap:g}球后的胜平负概率，与上方模型预测不同源">'
            f'让球盘 {sign}{hcap:g}</span> '
            f'<span class="pick-val {cls}" style="border-color:{ {"home":"var(--green)","draw":"var(--purple)","away":"var(--red)"}[cls] }">'
            f'{label} {prob:.0%}</span>{edge_html}{ev_html}')


def _pred_score(p):
    """预测比分（前5个最可能比分）"""
    # 2026-08-05: 3→5。账本 113 场实证：top3 命中 38% vs top5 52%，
    # 高进球场次(≥3球,占一半) top3 仅 16%——前3个被 1-0/1-1 占满，
    # DJYY 实际给了 8-10 个比分，主推 3 个浪费了能中的第4/5个。
    top_scores = p.get("top_scores")
    if not top_scores or not isinstance(top_scores, list) or len(top_scores) == 0:
        return ""
    scores = []
    for item in top_scores[:5]:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            scores.append(f"{item[0]}-{item[1]}")
    if scores:
        # 2026-08-06 UI 修复：明确"预测比分"（模型观点），避免与赛果混淆
        return f' <span class="pick-label" title="模型预测的最可能比分（非赛果）">预测比分</span> <span class="pred-score">{" / ".join(scores)}</span>'
    return ""


def _recent_cell(r):
    """近期窗口列：最近5场命中（2026-08-06）"""
    n = r.get("recent_n", 0)
    if not n:
        return '<td style="color:var(--dim)">—</td>'
    rate = r.get("recent_hit_rate", 0)
    color = "var(--green)" if rate >= 0.6 else ("var(--amber)" if rate >= 0.4 else "var(--red)")
    return (f'<td style="color:{color}">近{n}场 {r.get("recent_hits", 0)}中 ({rate*100:.0f}%)'
            f'<br><span style="font-size:0.62rem;color:var(--dim)">近ROI {r.get("recent_roi", 0)*100:+.0f}%</span></td>')


def _recent10_cell(r):
    """近期10场窗口列（2026-08-13 新增：三窗口对照，中期趋势）"""
    n = r.get("recent10_n", 0)
    if not n:
        return '<td style="color:var(--dim)">—</td>'
    rate = r.get("recent10_hit_rate", 0)
    color = "var(--green)" if rate >= 0.55 else ("var(--amber)" if rate >= 0.4 else "var(--red)")
    return (f'<td style="color:{color}">近{n}场 {r.get("recent10_hits", 0)}中 ({rate*100:.0f}%)'
            f'<br><span style="font-size:0.62rem;color:var(--dim)">近ROI {r.get("recent10_roi", 0)*100:+.0f}%</span></td>')


def _verdict_cell(r):
    """联赛判断列渲染（2026-08-06 增加回暖解禁态）"""
    v = r.get("verdict", "观望")
    if v == "价值区":
        return '<td style="color:var(--green);font-weight:700">✅ 价值区</td>'
    if v == "送钱区":
        return '<td style="color:var(--red);font-weight:700">🚫 送钱区·禁投</td>'
    if v == "回暖解禁":
        return ('<td style="color:#b45309;font-weight:700" title="累计口径送钱区，但近5场命中≥60% 且近10场≥50%，已自动解除禁投（双窗口判定，2026-08-13）">'
                '✅ 回暖解禁·观察</td>')
    if v == "谨慎":
        return '<td style="color:var(--amber)">⚠️ 谨慎</td>'
    return '<td style="color:var(--dim)">👀 观望</td>'


def _load_league_matrix(path):
    """加载 DJYY 联赛矩阵数据"""
    import json
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"leagues": []}


def _league_matrix_section(league_matrix, predictions, ref_date=None, embedded=False):
    """联赛矩阵面板：展示联赛统计数据，带静态快照过期检测"""
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
    for p in predictions or []:
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
    for p in predictions or []:
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

    gen_time = (league_matrix.get("generated_at") or "")[:10]
    age_days = None
    if gen_time:
        try:
            from datetime import datetime as _dtm
            _gen_dt = _dtm.strptime(gen_time, "%Y-%m-%d").date()
            _ref_dt = _dtm.strptime(ref_date[:10], "%Y-%m-%d").date() if ref_date else _dtm.now().date()
            age_days = (_ref_dt - _gen_dt).days
        except Exception:
            age_days = None

    is_stale = age_days is not None and age_days > 7
    if is_stale:
        freshness_html = f'<div class="stale-warn">⚠ 快照停留在 {gen_time}（已过期 {age_days} 天，非实时）</div>'
        sync_label = f'⚠ 快照停留在 {gen_time}（已过期 {age_days} 天）'
    else:
        freshness_html = ''
        sync_label = f'⚡ 数据来自 DJYY（{gen_time or "最新"}）'

    active_count = sum(1 for lg in sorted_leagues if lg["name_zh"] in predicted_leagues)
    active_count += len(missing_comps)

    # 今日赛事提醒横幅
    alert_html = freshness_html
    if active_count > 0:
        _src_tag = sync_label if is_stale else f'数据源: DJYY · {gen_time or "最新"}'
        alert_html += (
            '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;margin:0 0 2px;'
            'background:linear-gradient(135deg,rgba(34,197,94,0.12),rgba(6,182,212,0.08));'
            'border:1px solid rgba(34,197,94,0.25);border-radius:var(--radius-sm);font-size:0.78rem">'
            '<span style="display:inline-flex;align-items:center;gap:4px;font-weight:800;color:var(--green)">'
            '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);'
            'animation:pulse 2s infinite"></span>'
            '今日赛事</span>'
            '<span style="color:var(--text-secondary)">' + str(active_count) + ' 个联赛有比赛</span>'
            f'<span style="font-size:0.62rem;color:var(--dim);margin-left:auto">{_src_tag}</span>'
            '</div>'
        )

    table_block = (
        '<div class="lm-wrap"><table class="lm-table">'
        + '<thead><tr><th>级别</th><th>联赛</th><th>场次</th><th>场均进球</th><th>场均xG</th>'
        + '<th>BTTS</th><th>主胜</th><th>平局</th><th>客胜</th><th>零封</th>'
        + '<th>角球</th><th>黄牌</th></tr></thead>'
        + '<tbody>' + rows + '</tbody></table></div>'
        + '<div style="padding:8px 12px;font-size:0.62rem;color:var(--dim);display:flex;gap:12px;flex-wrap:wrap">'
        + '<span>数据来源: <a href="https://djyylive.com" style="color:var(--blue)">DJYY</a></span>'
        + ('<span>&#x25cf; <span style="color:var(--green)">高亮行</span> = 当天有预测的联赛</span>' if active_count > 0 else '')
        + '<span>BTTS = 双方进球率</span>'
        + f'<span>{sync_label}</span></div>'
    )
    if embedded:
        return alert_html + f'<div id="league-matrix">{table_block}</div>'

    return (
        '<div class="section-title" onclick="document.getElementById(\'league-matrix\').classList.toggle(\'collapsed\')" style="cursor:pointer">'
        + '联赛矩阵 &middot; ' + str(len(leagues) + len(missing_comps)) + ' 联赛/杯赛 &middot; ' + gen_time
        + ' <span style="font-size:0.65rem;color:var(--dim)">&#9654; 点击展开</span></div>'
        + alert_html
        + '<div id="league-matrix" class="collapsed">' + table_block + '</div>'
    )


def _collect_match_risks(p):
    """汇总单场风险提醒，避免卡片头部徽章堆叠，详情进模型 tab 展示"""
    risks = []
    if p.get("draw_alert"):
        _dmsg = {
            "cold_draw": "冷门平局：一方被看好但平局风险偏高",
            "league_draw": "联赛平局：高平联赛 + 市场平局被低估（R1 改判）",
            "balanced_draw": "均势平局：双方接近，平局被低估",
        }.get(p.get("draw_alert"), "均势平局：双方接近，平局被低估")
        risks.append(("平局预警", _dmsg))
    if p.get("prob_band_60_risk"):
        risks.append(("60%+反向风险", "该联赛高置信反向样本≥2场，60%+段已降档"))
    if p.get("prob_band_5060"):
        risks.append(("50-60%盲点", "50-60%概率段=平局盲点区，已降档"))
    if (p.get("freshness") or {}).get("risk") in ("watch", "alert"):
        risks.append(("数据新鲜度", "数据新鲜度风险（watch/alert）"))
    return risks


def _brief_handicap(p):
    """主列表紧凑让球结论（完整多方向 EV 保留在展开抽屉）"""
    hcap = p.get("handicap")
    if hcap is None:
        return ""
    hhp, hdp, hap = (p.get("handicap_home_prob") or 0, p.get("handicap_draw_prob") or 0, p.get("handicap_away_prob") or 0)
    if not (hhp or hdp or hap):
        return ""
    label = "主" if hhp >= hdp and hhp >= hap else ("平" if hdp >= hhp and hdp >= hap else "客")
    prob = max(hhp, hdp, hap)
    sign = "+" if hcap > 0 else ""
    return f'<span class="hcap-label">让{sign}{hcap:g} {label}{prob:.0%}</span>'


def _best_match_ev(p):
    """提取单场跨玩法（胜平负 / 让球 / 全玩法）最佳正EV信息: (edge, label, odd_str)"""
    candidates = []
    _dir_names = (("home", "主胜"), ("draw", "平局"), ("away", "客胜"))
    for k, lbl in _dir_names:
        prob = p.get(f"{'home_win' if k == 'home' else ('away_win' if k == 'away' else 'draw')}_prob") or 0
        odd = p.get(f"{k}_odds") or 0
        if odd and odd > 1.0 and prob > 0:
            ev = prob * odd - 1.0
            if 0.01 < ev <= 0.35:
                candidates.append((ev, f"胜平负·{lbl}", f"@{odd:.2f}"))
    _ke = p.get("kelly_edge")
    if isinstance(_ke, (int, float)) and _ke > 0.01:
        _kd = p.get("direction") or "home"
        _kl = {"home": "主胜", "draw": "平局", "away": "客胜"}.get(_kd, _kd)
        _ko = p.get(f"{_kd}_odds") or 0
        candidates.append((float(_ke), f"胜平负·{_kl}", f"@{_ko:.2f}" if _ko and _ko > 1.0 else ""))

    hev = p.get("handicap_ev") or {}
    hcap = hev.get("handicap", p.get("handicap"))
    h_edges = hev.get("edges") or {}
    h_odds = hev.get("odds") or {}
    if hcap is not None and isinstance(h_edges, dict):
        sign = "+" if hcap > 0 else ""
        for k, lbl in (("home", "让胜"), ("draw", "让平"), ("away", "让客")):
            ev = h_edges.get(k)
            odd = h_odds.get(k) or 0
            if isinstance(ev, (int, float)) and 0.01 < ev <= 0.35:
                candidates.append((float(ev), f"让{sign}{hcap:g}·{lbl}", f"@{odd:.2f}" if odd and odd > 1.0 else ""))

    _pev = p.get("play_ev") or {}
    if isinstance(_pev, dict):
        _pnames = {"ttg": "总进球", "crs": "比分", "hafu": "半全场"}
        for k, ev in _pev.items():
            if isinstance(ev, (int, float)) and 0.01 < ev <= 0.35:
                candidates.append((float(ev), _pnames.get(k, str(k)), "全玩法"))

    if not candidates:
        return (0.0, "", "")
    return max(candidates, key=lambda x: x[0])


def _sporttery_focus_bar(predictions, results_map=None):
    """体彩今日三焦点栏：最佳玩法机会 · 低损稳胆参考 · 头号防冷避坑。"""
    if not predictions:
        return ""

    def _is_settled_pred(p):
        if not results_map:
            return False
        mid = p.get("match_id", "")
        r = results_map.get(mid)
        if not r and p.get("home_team") and p.get("away_team"):
            r = results_map.get(f"{p['home_team']}_vs_{p['away_team']}")
        return bool(r and r.get("home_score") is not None)

    pool = [p for p in predictions if not _is_settled_pred(p)] or list(predictions)

    # 1) 最佳玩法机会（优先非禁投场次）
    best_ev_row = None
    for prefer_allowed in (True, False):
        for p in pool:
            if prefer_allowed and p.get("league_forbidden"):
                continue
            ev, lbl, odd_s = _best_match_ev(p)
            if ev > 0.01 and (best_ev_row is None or ev > best_ev_row[0]):
                best_ev_row = (ev, lbl, odd_s, p)
        if best_ev_row:
            break

    if best_ev_row:
        _ev, _lbl, _os, _ep = best_ev_row
        _ejc = _esc(_jc_no(_ep.get("match_id", "")))
        _etm = _esc(f"{_ep.get('home_team', '')} vs {_ep.get('away_team', '')}")
        _ev_single = "单关" if (_ep.get("had_single") or _ep.get("hhad_single")) else "串关"
        ev_main = f"{_ejc} · {_etm}"
        ev_sub = f"{_esc(_lbl)} {_os} · EV {_ev:+.0%} · {_ev_single}"
    else:
        ev_main = "今日暂无显著正 EV"
        ev_sub = "不强行凑单 · 可仅观察低损稳胆或休战"

    # 2) 低损稳胆参考
    steady_cands = []
    for p in pool:
        probs = (
            ("home", "主胜", p.get("home_win_prob") or 0, p.get("home_odds") or 0),
            ("draw", "平局", p.get("draw_prob") or 0, p.get("draw_odds") or 0),
            ("away", "客胜", p.get("away_win_prob") or 0, p.get("away_odds") or 0),
        )
        top = max(probs, key=lambda x: x[2])
        pen = 0
        if p.get("league_forbidden"):
            pen += 3
        if p.get("draw_alert"):
            pen += 2
        if p.get("prob_band_5060"):
            pen += 1
        steady_cands.append((pen, -top[2], p, top))
    steady_cands.sort(key=lambda x: (x[0], x[1]))
    _, _, _sp, _stop = steady_cands[0]
    _sjc = _esc(_jc_no(_sp.get("match_id", "")))
    _stm = _esc(f"{_sp.get('home_team', '')} vs {_sp.get('away_team', '')}")
    _sodd = f" @{_stop[3]:.2f}" if _stop[3] and _stop[3] > 1.0 else ""
    _ssingle = "单关" if _sp.get("had_single") else "串关胆材"
    st_main = f"{_sjc} · {_stm}"
    st_sub = f"{_stop[1]} {_stop[2]:.0%}{_sodd} · {_ssingle}"

    # 3) 头号防冷避坑
    risk_cands = []
    for p in pool:
        score = 0
        tags = []
        if p.get("league_forbidden"):
            score += 3
            tags.append("禁投联赛")
        if p.get("draw_alert"):
            score += 2
            tags.append("平局预警")
        if p.get("prob_band_60_risk"):
            score += 2
            tags.append("60%+反向风险")
        if p.get("prob_band_5060"):
            score += 1
            tags.append("50-60%盲点")
        if (p.get("freshness") or {}).get("risk") in ("watch", "alert"):
            score += 1
            tags.append("新鲜度告警")
        if score > 0:
            risk_cands.append((-score, p, tags))
    risk_cands.sort(key=lambda x: x[0])
    if risk_cands:
        _, _rp, _rtags = risk_cands[0]
        _rjc = _esc(_jc_no(_rp.get("match_id", "")))
        _rtm = _esc(f"{_rp.get('home_team', '')} vs {_rp.get('away_team', '')}")
        rk_main = f"{_rjc} · {_rtm}"
        rk_sub = " · ".join(_rtags[:3])
    else:
        rk_main = "无高危避坑场次"
        rk_sub = "当前赛程无禁投联赛或强冷平告警"

    return f"""
  <div class="focus-strip">
    <div class="focus-col focus-ev">
      <div class="focus-kicker"><span>最佳玩法机会</span><span>EV 雷达</span></div>
      <div class="focus-main">{ev_main}</div>
      <div class="focus-sub">{ev_sub}</div>
    </div>
    <div class="focus-col focus-stable">
      <div class="focus-kicker"><span>低损稳胆参考</span><span>高胜率优先</span></div>
      <div class="focus-main">{st_main}</div>
      <div class="focus-sub">{st_sub}</div>
    </div>
    <div class="focus-col focus-risk">
      <div class="focus-kicker"><span>头号防冷避坑</span><span>风险雷达</span></div>
      <div class="focus-main">{rk_main}</div>
      <div class="focus-sub">{rk_sub}</div>
    </div>
  </div>"""


def _match_card(p, value_matches, idx, results_map=None):
    """Render a compact FotMob-style horizontal match row with expandable detail drawer."""
    hp = (p.get("home_win_prob") or 0) * 100
    dp = (p.get("draw_prob") or 0) * 100
    ap = (p.get("away_win_prob") or 0) * 100
    is_val = _is_value(p, value_matches)
    match_id = p.get("match_id", "")
    jc_no = _jc_no(match_id)
    uid = f"m{idx}"

    odds_h = p.get("home_odds") or 0
    odds_d = p.get("draw_odds") or 0
    odds_a = p.get("away_odds") or 0
    xg_h = p.get("home_xg") or 0
    xg_a = p.get("away_xg") or 0

    kickoff_str = str(p.get("kickoff") or "")
    kickoff_short = kickoff_str[11:16] if len(kickoff_str) >= 16 else (kickoff_str or "—")

    # 确定主推方向用于表格视觉强调
    d_field = p.get("direction") or ""
    if d_field in ("home", "draw", "away"):
        pred_dir = d_field
    elif hp >= dp and hp >= ap:
        pred_dir = "home"
    elif dp >= hp and dp >= ap:
        pred_dir = "draw"
    else:
        pred_dir = "away"

    # 赛果解析（若已完赛则右列显示实际比分，否则显示模型 xG）
    settled_hs = settled_as = None
    settled_badge = ""
    if results_map:
        r = results_map.get(match_id)
        if not r:
            home_team = p.get("home_team", "")
            away_team = p.get("away_team", "")
            if home_team and away_team:
                r = results_map.get(f"{home_team}_vs_{away_team}")
        if r and r.get("home_score") is not None:
            settled_hs, settled_as = int(r["home_score"]), int(r["away_score"])
            ph = p.get("home_win_prob") or 0
            pd_ = p.get("draw_prob") or 0
            pa = p.get("away_win_prob") or 0
            pred_outcome = "home" if (ph >= pd_ and ph >= pa) else ("draw" if (pd_ >= ph and pd_ >= pa) else "away")
            hit = pred_outcome == ("home" if settled_hs > settled_as else "draw" if settled_hs == settled_as else "away")
            hit_icon = "✓" if hit else "✗"
            hit_cls = "hit" if hit else "miss"
            score_rank_html = ""
            top_scores = p.get("top_scores") or []
            _rank = 0
            for _i, item in enumerate(top_scores):
                if isinstance(item, (list, tuple)) and len(item) >= 2 and int(item[0]) == settled_hs and int(item[1]) == settled_as:
                    _rank = _i + 1
                    break
            if _rank > 0:
                _badge_cls = "hit" if _rank <= 5 else ""
                score_rank_html = f' <span class="ar-hit {_badge_cls}" title="比分命中候选第{_rank}位">🎯{_rank}</span>'
            settled_badge = f'<span class="ar-hit {hit_cls}" title="赛果 {settled_hs}-{settled_as}">{hit_icon}</span>{score_rank_html}'

    if settled_hs is not None:
        home_stat_html = f'<span class="m-team-stat score">{settled_hs}</span>'
        away_stat_html = f'<span class="m-team-stat score">{settled_as}</span>'
    else:
        home_stat_html = f'<span class="m-team-stat" title="本场模型预期进球 xG">{xg_h:.2f}</span>'
        away_stat_html = f'<span class="m-team-stat" title="本场模型预期进球 xG">{xg_a:.2f}</span>'

    cutoff_early = False
    if settled_hs is None and len(kickoff_short) >= 5 and ":" in kickoff_short:
        try:
            cutoff_early = int(kickoff_short.split(":", 1)[0]) < 9
        except ValueError:
            cutoff_early = False

    had_single = bool(p.get("had_single"))
    hhad_single = bool(p.get("hhad_single"))
    single_html = '<span class="single-tag" title="体彩胜平负/让球支持单关固定投注">单</span>' if (had_single or hhad_single) else ""
    cutoff_html = '<span class="cutoff-tag" title="凌晨开赛场次通常22:00-23:50提前停售，注意提前打票">早截单</span>' if cutoff_early else ""

    # 至多 1 个主状态徽章 + 1 个紧凑风险胶囊 + 全玩法EV提示
    status_badge_html = ""
    if is_val:
        status_badge_html = '<span class="value-badge">价值精选</span>'
    elif p.get("league_forbidden"):
        status_badge_html = '<span class="status-pill forbid" title="送钱区联赛（累计ROI<-5%），此场不出注">禁投联赛</span>'
    elif p.get("league_recovered"):
        status_badge_html = '<span class="status-pill recover" title="该联赛累计送钱但最近5场命中≥60%，已自动解禁">回暖解禁</span>'

    play_ev_html = ""
    _pev = p.get("play_ev")
    if isinstance(_pev, dict) and _pev:
        _play_names = {"ttg": "总进球", "crs": "比分", "hafu": "半全场"}
        _best_play = max(
            ((k, float(v)) for k, v in _pev.items() if isinstance(v, (int, float)) and v > 0),
            key=lambda kv: kv[1],
            default=None,
        )
        if _best_play:
            _pk, _pv = _best_play
            play_ev_html = f'<span class="play-ev-pill" title="体彩全玩法检测到正EV">{_play_names.get(_pk, _pk)} {_pv:+.0%}</span>'

    risks = _collect_match_risks(p)
    risk_pill_html = ""
    if len(risks) == 1:
        _rname, _rdesc = risks[0]
        risk_pill_html = f'<span class="risk-pill" title="{_esc(_rdesc, quote=True)}">⚠ {_rname}</span>'
    elif len(risks) > 1:
        _rtips = " | ".join(f"{n}: {d}" for n, d in risks)
        risk_pill_html = f'<span class="risk-pill" title="{_esc(_rtips, quote=True)}">⚠ {len(risks)}项提醒</span>'

    # 紧凑主推副行：前3最可能比分 + 简明让球 + 风险胶囊
    _top3_scores = []
    for item in (p.get("top_scores") or [])[:3]:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            _top3_scores.append(f"{item[0]}-{item[1]}")
    brief_scores_html = f'<span class="pred-score">{" / ".join(_top3_scores)}</span>' if _top3_scores else ""
    brief_hcap_html = _brief_handicap(p)

    model_tab = _tab_model(p, uid)
    odds_tab = _tab_odds(p, uid)
    dist_tab = _tab_distribution(p, uid)
    provenance = _prediction_provenance(p)
    provenance_inline = (
        f'模型 {_format_direction_view(provenance["model"])} · '
        f'市场 {_format_direction_view(provenance["market"])} · '
        f'最终 {_format_direction_view(provenance["final"])}'
    )

    def _fmt_odd(v):
        return f"{v:.2f}" if v and v > 0 else "—"

    match_title = f"{p.get('home_team', '')} vs {p.get('away_team', '')}"

    def _render_odd_row(mkt, tag, tag_cls, mkt_label, items, row_single):
        cells_html = ""
        for sel_code, sel_label, dir_cls, prob, odd, is_best, is_ev in items:
            cls_parts = ["odd-cell"]
            if is_best:
                cls_parts.extend(["best", dir_cls])
            if is_ev:
                cls_parts.append("pos-ev")
            dis = "" if (odd and odd > 1.0) else " disabled"
            odd_num = f"{odd:.2f}" if (odd and odd > 1.0) else "0"
            ev_tip = f" title=\"模型估值正EV ({(prob * odd - 1):+.1%})\"" if (is_ev and odd and odd > 1.0) else ""
            cells_html += (
                f'<button type="button" class="{" ".join(cls_parts)}"{dis}{ev_tip}'
                f' data-mid="{_esc(match_id, quote=True)}"'
                f' data-jc="{_esc(jc_no, quote=True)}"'
                f' data-match="{_esc(match_title, quote=True)}"'
                f' data-mkt="{mkt}"'
                f' data-mkt-label="{_esc(mkt_label, quote=True)}"'
                f' data-sel="{sel_code}"'
                f' data-sel-label="{_esc(sel_label, quote=True)}"'
                f' data-odd="{odd_num}"'
                f' data-prob="{prob:.4f}"'
                f' data-is-top="{"1" if is_best else "0"}"'
                f' data-single="{"1" if row_single else "0"}">'
                f'<span class="oc-p">{prob * 100:.0f}%</span>'
                f'<span class="oc-o">{_fmt_odd(odd)}</span>'
                f'</button>'
            )
        return (
            f'<div class="odd-row">'
            f'<span class="mkt-tag {tag_cls}">{tag}</span>'
            f'{cells_html}'
            f'</div>'
        )

    _hp_raw = p.get("home_win_prob") or 0
    _dp_raw = p.get("draw_prob") or 0
    _ap_raw = p.get("away_win_prob") or 0
    had_items = [
        ("h", "主胜", "home", _hp_raw, odds_h, pred_dir == "home", bool(odds_h and odds_h > 1.0 and 0.01 < (_hp_raw * odds_h - 1) <= 0.30)),
        ("d", "平局", "draw", _dp_raw, odds_d, pred_dir == "draw", bool(odds_d and odds_d > 1.0 and 0.01 < (_dp_raw * odds_d - 1) <= 0.30)),
        ("a", "客胜", "away", _ap_raw, odds_a, pred_dir == "away", bool(odds_a and odds_a > 1.0 and 0.01 < (_ap_raw * odds_a - 1) <= 0.30)),
    ]
    odds_rows_html = _render_odd_row("had", "0", "", "胜平负", had_items, had_single)

    hev = p.get("handicap_ev") or {}
    hcap = p.get("handicap", hev.get("handicap"))
    h_probs = hev.get("probs") or {}
    h_odds = hev.get("odds") or {}
    h_edges = hev.get("edges") or {}
    hhp = float(h_probs.get("home") or p.get("handicap_home_prob") or 0)
    hdp = float(h_probs.get("draw") or p.get("handicap_draw_prob") or 0)
    hap = float(h_probs.get("away") or p.get("handicap_away_prob") or 0)
    hoh = float(h_odds.get("home") or p.get("handicap_home_odds") or 0)
    hod = float(h_odds.get("draw") or p.get("handicap_draw_odds") or 0)
    hoa = float(h_odds.get("away") or p.get("handicap_away_odds") or 0)
    if hcap is not None and (any(o > 1.0 for o in (hoh, hod, hoa)) or any(pr > 0 for pr in (hhp, hdp, hap))):
        h_sign = "+" if hcap > 0 else ""
        h_tag = f"{h_sign}{hcap:g}"
        h_best = "home" if (hhp >= hdp and hhp >= hap) else ("draw" if (hdp >= hhp and hdp >= hap) else "away")
        def _h_ev_flag(k, prob, odd):
            ev = h_edges.get(k)
            if isinstance(ev, (int, float)):
                return 0.01 < ev <= 0.30
            return bool(odd and odd > 1.0 and 0.01 < (prob * odd - 1) <= 0.30)
        hhad_items = [
            ("hh", "让胜", "home", hhp, hoh, h_best == "home" and hhp > 0, _h_ev_flag("home", hhp, hoh)),
            ("hd", "让平", "draw", hdp, hod, h_best == "draw" and hdp > 0, _h_ev_flag("draw", hdp, hod)),
            ("ha", "让客", "away", hap, hoa, h_best == "away" and hap > 0, _h_ev_flag("away", hap, hoa)),
        ]
        odds_rows_html += _render_odd_row("hhad", h_tag, "hhad", f"让{h_tag}", hhad_items, hhad_single)

    return f"""
  <div class="match {'value-pick' if is_val else ''}">
    <div class="match-header">
      <div class="m-meta" title="{_esc(kickoff_str, quote=True)}">
        <div class="m-meta-top">
          <span class="m-kickoff">{kickoff_short}</span>
          <span class="jc-no" title="竞彩编号">{jc_no}</span>
          {single_html}{cutoff_html}
        </div>
        <span class="league-tag">{p.get('competition', '')}</span>
      </div>

      <div class="m-teams">
        <div class="m-team-row">
          <span class="m-team-name {'fav' if pred_dir == 'home' else ''}">{p.get('home_team', '')}</span>
          {home_stat_html}
        </div>
        <div class="m-team-row">
          <span class="m-team-name {'fav' if pred_dir == 'away' else ''}">{p.get('away_team', '')}</span>
          {away_stat_html}
        </div>
      </div>

      <div class="m-probs">
        {odds_rows_html}
        <div class="prob-bar">
          <div class="prob-seg h" style="width:{hp:.1f}%"></div>
          <div class="prob-seg d" style="width:{dp:.1f}%"></div>
          <div class="prob-seg a" style="width:{ap:.1f}%"></div>
        </div>
      </div>

      <div class="m-pick">
        <div class="m-pick-top">{_pred_pick(p)}{settled_badge or status_badge_html}{play_ev_html}</div>
        <div class="m-pick-sub">{brief_scores_html}{brief_hcap_html}{risk_pill_html}</div>
        <div class="m-pick-sub provenance-inline" title="原始模型是独立统计判断；市场由赔率去水得到；最终是两者按实际权重融合">{provenance_inline}</div>
      </div>

      <div class="m-chevron">&#9662;</div>
    </div>
    <div class="match-detail">
      <div class="tab-bar">
        <button class="tab-btn active" data-tab="{uid}-model">模型与归因</button>
        <button class="tab-btn" data-tab="{uid}-odds">赔率与边际</button>
        <button class="tab-btn" data-tab="{uid}-dist">体彩全玩法</button>
      </div>
      <div class="tab-content active" id="{uid}-model">{_fusion_trace_row(p)}{model_tab}</div>
      <div class="tab-content" id="{uid}-odds">{odds_tab}</div>
      <div class="tab-content" id="{uid}-dist">{dist_tab}</div>
    </div>
  </div>"""


def _tab_model(p, uid):
    """模型 tab: signal strip, model_raw vs market_fair vs djyy vs final, Elo, Wilson, + DJYY xG/ injuries."""
    model_raw = p.get("model_raw") or {}
    market_fair = p.get("market_fair")
    djyy = p.get("djyy_model_prob")
    final_h = p.get("home_win_prob") or 0
    final_d = p.get("draw_prob") or 0
    final_a = p.get("away_win_prob") or 0
    elo_home = p.get("elo_home")
    elo_away = p.get("elo_away")
    wilson = p.get("wilson_trust") or 0
    conf = p.get("confidence") or 0

    risks = _collect_match_risks(p)
    risk_banner = ""
    if risks:
        _r_lines = "<br>".join(f"<b>⚠ {n}</b>：{d}" for n, d in risks)
        risk_banner = f'<div class="risk-banner">{_r_lines}</div>'

    _hcap_full = _handicap_pick(p)
    _chips = [f'<span class="info-chip">置信度 <b>{conf:.0%}</b></span>']
    for _ch in (
        _odds_movement_chip(p),
        _odds_series_chip(p),
        _divergence_chip(p),
        _context_chip(p),
    ):
        if _ch:
            _chips.append(_ch)
    if _hcap_full:
        _chips.append(f'<span class="info-chip">{_hcap_full}</span>')
    signal_strip = f'<div class="signal-strip">{"".join(_chips)}</div>'

    # ===== DJYY 增强数据 =====
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
        <h5>DJYY 历史场均 xG (近5场平均，非本场模型xG)</h5>
        <div class="xg-bar-row">
          <span class="xg-bar-label">主队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill home" style="width:{h_pct}%">{xg_home:.2f}</div></div>
        </div>
        <div class="xg-bar-row">
          <span class="xg-bar-label">客队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill away" style="width:{a_pct}%">{xg_away:.2f}</div></div>
        </div>
      </div>"""

    rest_html = ""
    rest_days = p.get("rest_days") or {}
    if rest_days:
        rh = rest_days.get("home") or 7
        ra = rest_days.get("away") or 7
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

    inj_html = ""
    injuries = p.get("injuries") or {}
    if injuries:
        h_cnt = injuries.get("home_count", 0)
        h_att = injuries.get("home_attackers", 0)
        a_cnt = injuries.get("away_count", 0)
        a_att = injuries.get("away_attackers", 0)
        h_warn = "⚠️" if h_att >= 1 else ""
        a_warn = "⚠️" if a_att >= 1 else ""
        inj_html = f"""
      <div class="reverse-box">
        <h5>伤停预警</h5>
        <div class="reverse-row"><span class="rk">主队</span><span>伤停{h_cnt}人{h_warn} (含攻击线{h_att}人)</span></div>
        <div class="reverse-row"><span class="rk">客队</span><span>伤停{a_cnt}人{a_warn} (含攻击线{a_att}人)</span></div>
      </div>"""

    rows = ""
    mr_h = model_raw.get("home", 0) * 100 if model_raw else 0
    mr_d = model_raw.get("draw", 0) * 100 if model_raw else 0
    mr_a = model_raw.get("away", 0) * 100 if model_raw else 0
    rows += _model_row("DC+MC 原始", mr_h, mr_d, mr_a)

    if market_fair and len(market_fair) >= 3:
        mf_h = market_fair[0] * 100
        mf_d = market_fair[1] * 100
        mf_a = market_fair[2] * 100
        rows += _model_row("Shin公平", mf_h, mf_d, mf_a)
    else:
        rows += _model_row_empty("Shin公平")

    if djyy and djyy.get("home"):
        dj_h = djyy.get("home", 0) * 100
        dj_d = djyy.get("draw", 0) * 100
        dj_a = djyy.get("away", 0) * 100
        rows += _model_row("DJYY模型", dj_h, dj_d, dj_a)
    else:
        rows += _model_row_empty("DJYY模型")

    rows += _model_row("最终融合", final_h * 100, final_d * 100, final_a * 100)

    elo_html = ""
    if elo_home is not None and elo_away is not None:
        elo_diff = (elo_home or 0) - (elo_away or 0)
        elo_html = f"""
      <div class="elo-row">
        <div class="elo-chip"><div class="elo-label">主队Elo</div><div class="elo-val" style="color:var(--blue)">{elo_home:.0f}</div></div>
        <div class="elo-chip"><div class="elo-label">客队Elo</div><div class="elo-val" style="color:var(--red)">{elo_away:.0f}</div></div>
        <div class="elo-chip"><div class="elo-label">Elo差值</div><div class="elo-val" style="color:{'var(--green)' if elo_diff > 0 else 'var(--red)'}">{elo_diff:+.0f}</div></div>
      </div>"""

    wilson_pct = (wilson or 0) * 100
    wilson_html = f"""
      <div class="wilson-meter">
        <div class="wilson-label">Wilson信任分</div>
        <div class="wilson-track"><div class="wilson-fill" style="width:{wilson_pct:.0f}%"></div></div>
        <div class="wilson-val">{wilson_pct:.1f}% 置信权重</div>
      </div>"""

    extras = "".join(x for x in (xg_html, rest_html, inj_html) if x)
    extras_grid = f'<div class="detail-grid">{extras}</div>' if extras else ""

    return f"""
      {risk_banner}
      {signal_strip}
      {_prediction_provenance_html(p)}
      <table class="model-table">
        <tr><th>信号源</th><th class="prob-bar-cell">主 / 平 / 客 概率分布</th></tr>
        {rows}
      </table>
      {extras_grid}
      {elo_html}
      {wilson_html}"""


def _model_row(label, h, d, a):
    return f"""
        <tr>
          <td class="src-label">{label}</td>
          <td class="prob-bar-cell">
            <div class="mini-bar">
              <div class="mb-h" style="width:{h:.1f}%">{h:.0f}</div>
              <div class="mb-d" style="width:{d:.1f}%">{d:.0f}</div>
              <div class="mb-a" style="width:{a:.1f}%">{a:.0f}</div>
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
    """赔率 tab: honest multi-bookie comparison (no fake Sporttery fallback) + split Fair Edge vs raw EV."""
    odds_h = p.get("home_odds") or 0
    odds_d = p.get("draw_odds") or 0
    odds_a = p.get("away_odds") or 0
    handicap = p.get("handicap", "")
    market_fair = p.get("market_fair")
    final_h = p.get("home_win_prob", 0)
    final_d = p.get("draw_prob", 0)
    final_a = p.get("away_win_prob", 0)

    is_synthetic = bool(p.get("odds_synthetic", False))
    src_label = "模型合成" if is_synthetic else "体彩官方"
    sporttery_odds = p.get("sporttery_odds") or {}
    st_h = sporttery_odds.get("home") or odds_h
    st_d = sporttery_odds.get("draw") or odds_d
    st_a = sporttery_odds.get("away") or odds_a

    # 2. Bet365（绝不回退到体彩赔率）
    bet365_odds = p.get("bet365_odds") or {}
    b365_h = bet365_odds.get("home")
    b365_d = bet365_odds.get("draw")
    b365_a = bet365_odds.get("away")

    # 3. Pinnacle（若缺原始赔率，优先用 cross_market 去水隐含反推公平赔率，绝不回退体彩）
    pinn_odds = p.get("pinnacle_odds") or {}
    pinn_h = pinn_odds.get("home")
    pinn_d = pinn_odds.get("draw")
    pinn_a = pinn_odds.get("away")
    pinn_label = "Pinnacle"
    if not any(v and v > 0 for v in (pinn_h, pinn_d, pinn_a)):
        cm = (p.get("lgbm_inputs") or {}).get("cross_market") or {}
        ph_imp = cm.get("pin_implied_home") or 0
        pd_imp = cm.get("pin_implied_draw") or 0
        pa_imp = cm.get("pin_implied_away") or 0
        if ph_imp > 0 and pd_imp > 0 and pa_imp > 0:
            pinn_label = "Pinnacle(隐含公平)"
            pinn_h = 1.0 / ph_imp
            pinn_d = 1.0 / pd_imp
            pinn_a = 1.0 / pa_imp

    # 4. DJYY 模型赔率（反向计算）
    djyy_odds = p.get("djyy_model_prob") or {}
    djyy_h = 1 / djyy_odds.get("home") if djyy_odds.get("home") and djyy_odds.get("home") > 0 else None
    djyy_d = 1 / djyy_odds.get("draw") if djyy_odds.get("draw") and djyy_odds.get("draw") > 0 else None
    djyy_a = 1 / djyy_odds.get("away") if djyy_odds.get("away") and djyy_odds.get("away") > 0 else None

    def _o(v):
        return f"{v:.2f}" if v and v > 0 else "-"

    bookies_html = f"""
      <table class="edge-table" style="margin-top: 8px;">
        <tr><th>来源</th><th>主胜</th><th>平局</th><th>客胜</th></tr>
        <tr><td class="src-label">{src_label}</td><td>{_o(st_h)}</td><td>{_o(st_d)}</td><td>{_o(st_a)}</td></tr>
        <tr><td class="src-label">Bet365</td><td>{_o(b365_h)}</td><td>{_o(b365_d)}</td><td>{_o(b365_a)}</td></tr>
        <tr><td class="src-label">{pinn_label}</td><td>{_o(pinn_h)}</td><td>{_o(pinn_d)}</td><td>{_o(pinn_a)}</td></tr>
        <tr><td class="src-label" style="color:var(--purple);">DJYY模型</td><td>{_o(djyy_h)}</td><td>{_o(djyy_d)}</td><td>{_o(djyy_a)}</td></tr>
      </table>"""

    # Implied probs from raw odds
    imp_h = (1 / odds_h * 100) if odds_h else 0
    imp_d = (1 / odds_d * 100) if odds_d else 0
    imp_a = (1 / odds_a * 100) if odds_a else 0

    has_mf = bool(market_fair and len(market_fair) >= 3)
    sf_h = market_fair[0] * 100 if has_mf else 0
    sf_d = market_fair[1] * 100 if has_mf else 0
    sf_a = market_fair[2] * 100 if has_mf else 0

    # 去水边际 (Fair Edge = 模型概率 - Shin公平概率) 与 含水 EV (模型概率 * 赔率 - 1)
    fair_h = (final_h * 100 - sf_h) if has_mf else None
    fair_d = (final_d * 100 - sf_d) if has_mf else None
    fair_a = (final_a * 100 - sf_a) if has_mf else None

    ev_h = ((final_h * odds_h - 1.0) * 100) if (odds_h and odds_h > 0 and not is_synthetic) else None
    ev_d = ((final_d * odds_d - 1.0) * 100) if (odds_d and odds_d > 0 and not is_synthetic) else None
    ev_a = ((final_a * odds_a - 1.0) * 100) if (odds_a and odds_a > 0 and not is_synthetic) else None

    def _fair_cls(v):
        if v is None:
            return ""
        return "edge-pos" if v > 1.5 else "edge-neg" if v < -1.5 else ""

    def _ev_cls(v):
        if v is None:
            return ""
        return "edge-pos" if v > 0 else "edge-neg" if v < 0 else ""

    def _fmt_pp(v):
        return f"{v:+.1f}pp" if v is not None else "—"

    def _fmt_ev(v):
        return f"{v:+.1f}%" if v is not None else "—"

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
        <div class="odds-box"><div class="ob-label">主胜</div><div class="ob-val">{_o(odds_h)}</div></div>
        <div class="odds-box"><div class="ob-label">平局</div><div class="ob-val">{_o(odds_d)}</div></div>
        <div class="odds-box"><div class="ob-label">客胜</div><div class="ob-val">{_o(odds_a)}</div></div>
        <div class="odds-box"><div class="ob-label">让球</div><div class="ob-val">{handicap if handicap not in ("", None) else '暂无'}</div></div>
      </div>
      <h5 style="margin: 14px 0 4px 0; color: var(--text-secondary); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.5px;">多源赔率对比 <span style="font-weight:500;color:var(--dim);text-transform:none">（无真实数据时显示 -，不回退体彩赔率）</span></h5>
      {bookies_html}
      <table class="edge-table">
        <tr><th>结果</th><th>赔率</th><th>含水隐含</th><th>Shin公平</th><th>模型</th><th>去水边际</th><th>含水 EV</th></tr>
        <tr>
          <td>主胜</td><td>{_o(odds_h)}</td><td>{imp_h:.1f}%</td><td>{f'{sf_h:.1f}%' if has_mf else '—'}</td><td>{final_h*100:.1f}%</td>
          <td class="{_fair_cls(fair_h)}">{_fmt_pp(fair_h)}</td>
          <td class="{_ev_cls(ev_h)}">{_fmt_ev(ev_h)}</td>
        </tr>
        <tr>
          <td>平局</td><td>{_o(odds_d)}</td><td>{imp_d:.1f}%</td><td>{f'{sf_d:.1f}%' if has_mf else '—'}</td><td>{final_d*100:.1f}%</td>
          <td class="{_fair_cls(fair_d)}">{_fmt_pp(fair_d)}</td>
          <td class="{_ev_cls(ev_d)}">{_fmt_ev(ev_d)}</td>
        </tr>
        <tr>
          <td>客胜</td><td>{_o(odds_a)}</td><td>{imp_a:.1f}%</td><td>{f'{sf_a:.1f}%' if has_mf else '—'}</td><td>{final_a*100:.1f}%</td>
          <td class="{_fair_cls(fair_a)}">{_fmt_pp(fair_a)}</td>
          <td class="{_ev_cls(ev_a)}">{_fmt_ev(ev_a)}</td>
        </tr>
      </table>
      <div class="odds-meta-grid">
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
        </div>
      </div>"""


def _tab_distribution(p, uid):
    """体彩全玩法与分布 tab: 波胆(crs_odds), 总进球(ttg_odds), 半全场(hafu_odds), xG."""
    top_scores = p.get("top_scores")
    total_goals = p.get("total_goals")
    xg_h = p.get("home_xg", 0)
    xg_a = p.get("away_xg", 0)

    try:
        from engine.strategy.multi_play_ev import parse_crs_odds, parse_ttg_odds, parse_hafu_odds
        crs_map = parse_crs_odds(p.get("crs_odds") or {})
        ttg_map = parse_ttg_odds(p.get("ttg_odds") or {})
        hafu_map = parse_hafu_odds(p.get("hafu_odds") or {})
    except Exception:
        crs_map, ttg_map, hafu_map = {}, {}, {}

    # 1) 波胆矩阵 (含官方 crs_odds 与正EV标记)
    scores_html = ""
    if top_scores and isinstance(top_scores, list) and len(top_scores) > 0:
        cells = ""
        for item in top_scores[:12]:
            score_label = ""
            prob = 0.0
            pair = None
            if isinstance(item, dict):
                score_label = str(item.get("score", ""))
                prob = float(item.get("prob", 0) or 0)
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                score_label = f"{item[0]}-{item[1]}"
                prob = float(item[2] or 0)
                try:
                    pair = (int(item[0]), int(item[1]))
                except (TypeError, ValueError):
                    pair = None
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                score_label = str(item[0])
                prob = float(item[1] or 0)
            if pair is None and "-" in score_label:
                _sp = score_label.split("-", 1)
                try:
                    pair = (int(_sp[0]), int(_sp[1]))
                except ValueError:
                    pair = None
            odd = crs_map.get(pair, 0.0) if pair else 0.0
            ev = (prob * odd - 1.0) if (odd > 1.0 and prob > 0) else 0.0
            is_pos = 0.01 < ev <= 0.30
            odd_html = f'<div class="sc-odd">@{odd:.1f}</div>' if odd > 1.0 else ""
            ev_html = f'<div class="sc-ev-badge">+EV {ev*100:.0f}%</div>' if is_pos else ""
            cells += (
                f'<div class="score-cell{" pos-ev" if is_pos else ""}">'
                f'<div class="sc-score">{_esc(score_label)}</div>'
                f'<div class="sc-prob">{prob*100:.1f}%</div>'
                f'{odd_html}{ev_html}</div>'
            )
        if cells:
            _crs_note = " · 含体彩官方波胆赔率" if crs_map else ""
            scores_html = f'<div style="margin-bottom:6px;font-size:0.68rem;color:var(--dim);letter-spacing:0.4px;">波胆预测{_crs_note}</div><div class="scores-grid">{cells}</div>'
    else:
        scores_html = '<div style="font-size:0.72rem;color:var(--dim);font-style:italic;margin-bottom:12px;">比分分布暂无数据</div>'

    # 2) 总进球分布 (含官方 ttg_odds 与正EV标记)
    goals_html = ""
    if isinstance(total_goals, dict):
        total_goals = [[int(float(k)), v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else (v if isinstance(v, (int, float)) else 0)]
                       for k, v in total_goals.items()]
    if total_goals and isinstance(total_goals, list) and len(total_goals) > 0:
        max_prob = max((item.get("prob", 0) if isinstance(item, dict) else (item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else 0) for item in total_goals), default=1) or 1
        bars = ""
        for item in total_goals:
            if isinstance(item, dict):
                label = item.get("goals", item.get("label", ""))
                prob = float(item.get("prob", 0) or 0)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                label = item[0]
                prob = float(item[1] or 0)
            else:
                continue
            try:
                g_int = 7 if str(label).strip().startswith("7") else int(float(label))
            except (TypeError, ValueError):
                g_int = None
            disp_lbl = "7+球" if g_int == 7 else (f"{g_int}球" if g_int is not None else str(label))
            odd = ttg_map.get(g_int, 0.0) if g_int is not None else 0.0
            ev = (prob * odd - 1.0) if (odd > 1.0 and prob > 0) else 0.0
            pct = (prob / max_prob) * 100
            odd_tag = f'<span style="font-size:0.66rem;color:var(--blue);font-weight:700;width:44px;text-align:right;flex-shrink:0;">@{odd:.1f}</span>' if odd > 1.0 else ""
            ev_tag = f'<span class="tag-ev">+EV {ev*100:.0f}%</span>' if (0.01 < ev <= 0.30) else ""
            bars += f"""
          <div class="goal-bar-row">
            <span class="goal-bar-label">{_esc(disp_lbl)}</span>
            <div class="goal-bar-track"><div class="goal-bar-fill" style="width:{pct:.0f}%">{prob*100:.1f}%</div></div>
            {odd_tag}{ev_tag}
          </div>"""
        _ttg_note = " · 含体彩官方总进球赔率" if ttg_map else ""
        goals_html = f'<div style="margin:14px 0 6px;font-size:0.68rem;color:var(--dim);letter-spacing:0.4px;">总进球分布{_ttg_note}</div><div class="goals-bars">{bars}</div>'
    else:
        goals_html = '<div style="font-size:0.72rem;color:var(--dim);font-style:italic;margin:12px 0;">总进球分布暂无数据</div>'

    # 3) 半全场矩阵 (htft + 官方 hafu_odds)
    hafu_html = ""
    htft = p.get("htft") or {}
    _hafu_names = {"HH": "胜胜", "HD": "胜平", "HA": "胜负", "DH": "平胜", "DD": "平平", "DA": "平负", "AH": "负胜", "AD": "负平", "AA": "负负"}
    _hafu_codes = [c for c in _hafu_names if (c in htft or c in hafu_map)]
    if _hafu_codes:
        if htft:
            _hafu_codes.sort(key=lambda c: -float(htft.get(c, 0) or 0))
        else:
            _hafu_codes.sort(key=lambda c: float(hafu_map.get(c, 999) or 999))
        h_cells = ""
        for code in _hafu_codes[:6]:
            prob = float(htft.get(code, 0) or 0)
            odd = float(hafu_map.get(code, 0) or 0)
            ev = (prob * odd - 1.0) if (odd > 1.0 and prob > 0) else 0.0
            is_pos = 0.01 < ev <= 0.30
            prob_txt = f"{prob*100:.1f}%" if prob > 0 else "—"
            odd_txt = f"@{odd:.2f}" if odd > 1.0 else "—"
            ev_txt = f'<span class="tag-ev" style="margin-left:0">+{ev*100:.0f}%</span>' if is_pos else ""
            h_cells += (
                f'<div class="hafu-cell{" pos-ev" if is_pos else ""}">'
                f'<div class="hafu-top"><span>{_hafu_names[code]}</span><span style="color:var(--blue)">{odd_txt}</span></div>'
                f'<div class="hafu-sub"><span>{prob_txt}</span>{ev_txt}</div>'
                f'</div>'
            )
        hafu_html = f'<div style="margin:14px 0 6px;font-size:0.68rem;color:var(--dim);letter-spacing:0.4px;">半全场预测 TOP6</div><div class="hafu-grid">{h_cells}</div>'

    # 4) xG comparison
    max_xg = max(xg_h, xg_a, 0.1)
    xg_h_pct = (xg_h / (max_xg * 1.3)) * 100
    xg_a_pct = (xg_a / (max_xg * 1.3)) * 100
    xg_html = f"""
      <div class="xg-compare">
        <h5>本场模型预期进球 (Poisson/DC xG)</h5>
        <div class="xg-bar-row">
          <span class="xg-bar-label">{p.get('home_team', 'Home')[:8]}</span>
          <div class="xg-bar-track"><div class="xg-bar-fill home" style="width:{xg_h_pct:.0f}%">{xg_h:.2f}</div></div>
        </div>
        <div class="xg-bar-row">
          <span class="xg-bar-label">{p.get('away_team', 'Away')[:8]}</span>
          <div class="xg-bar-track"><div class="xg-bar-fill away" style="width:{xg_a_pct:.0f}%">{xg_a:.2f}</div></div>
        </div>
      </div>"""

    return f"{scores_html}{goals_html}{hafu_html}{xg_html}"


def _jc_no(mid: str) -> str:
    """竞彩编号: '2026-08-29_周六023' → '周六023'（体彩官方对账口径）"""
    return mid.split("_", 1)[-1] if "_" in (mid or "") else (mid or "")

def _tk_key(t):
    """出票/结算票的关联键: (玩法, 排序后的腿 match_id 集)"""
    return (t.get("type", ""), tuple(sorted(lg.get("match", "") for lg in t.get("legs", []))))


def _settle_index(day_entry):
    """parlay_settle.json.by_date[date] → {关联键: 结算票}"""
    idx = {}
    if not isinstance(day_entry, dict):
        return idx
    for kind in ("parlay", "score_parlay"):
        block = day_entry.get(kind)
        if not isinstance(block, dict):
            continue
        for t in block.get("tickets") or []:
            if isinstance(t, dict) and t.get("legs"):
                idx[_tk_key(t)] = t
    return idx


def _settle_badge(st, stake_fallback=0.0):
    """结算状态徽章: ✅ 已中奖 +回报 / ❌ 未中 -投入 / ⏳ 待开赛"""
    if not st:
        return ""
    if st.get("pending"):
        return ('<span style="font-size:.68em;font-weight:700;color:var(--text-secondary);'
                'margin-left:6px">⏳ 待开赛</span>')
    if st.get("won"):
        ret = float(st.get("return") or 0)
        return (f'<span style="font-size:.68em;font-weight:700;color:var(--green);'
                f'margin-left:6px">✅ 中 +&yen;{ret:.2f}</span>')
    staked = float(st.get("stake") or stake_fallback or 0)
    return (f'<span style="font-size:.68em;font-weight:700;color:var(--red);'
            f'margin-left:6px">❌ 未中 -&yen;{staked:.0f}</span>')


def _ticket_section(ticket, predictions, st_index=None):
    if not ticket:
        return ""
    stable = ticket.get("stable", [])
    value = ticket.get("value", [])
    lottery = ticket.get("lottery", [])
    total_stake = ticket.get("total_stake", 0)
    bankroll = ticket.get("bankroll", 0)
    breaker_mult = ticket.get("breaker_multiplier", 1.0)

    def _items(items):
        if not items:
            return '<div class="ticket-empty">暂无选择</div>'
        html = ""
        _hafu_name = {"HH": "胜胜", "HD": "胜平", "HA": "胜负", "DH": "平胜", "DD": "平平", "DA": "平负", "AH": "负胜", "AD": "负平", "AA": "负负"}
        for it in items:
            match_id = it.get("match", "")
            _play = ""
            if "#" in match_id:
                match_id, _play = match_id.split("#", 1)
            teams = match_id.split("_", 1)[-1] if "_" in match_id else match_id
            for p in predictions:
                if p.get("match_id") == match_id:
                    teams = f'{p["home_team"]} vs {p["away_team"]}'
                    break
            sel_map = {"home": "主胜", "draw": "平局", "away": "客胜"}
            sel = it.get("sel", "")
            if sel.startswith("hcap_"):
                sel_label = "让球·" + sel_map.get(sel[5:], sel[5:])
            elif sel.startswith("ttg_"):
                sel_label = "总进球" + (sel[4:] + "+" if int(sel[4:]) >= 7 else sel[4:]) + "球"
            elif sel.startswith("crs_"):
                _parts = sel[4:].split("_")
                sel_label = f"比分{_parts[0]}:{_parts[1]}" if len(_parts) == 2 else sel
            elif sel.startswith("hafu_"):
                sel_label = "半全场·" + _hafu_name.get(sel[5:], sel[5:])
            else:
                sel_label = sel_map.get(sel, sel)
            # 降档角标（2026-08-06）：50-60% 概率段 / E 规则高置信反向风险联赛
            _downgrade = ""
            _reason = it.get("downgrade_reason", "")
            if _reason == "prob_5060":
                _downgrade = ' <span class="tag-warn" title="50-60% 概率段（平局盲点区）已降档">降档·平局盲点</span>'
            elif _reason == "league_60_risk":
                _downgrade = ' <span class="tag-warn" title="联赛高置信反向样本≥2场，60%+段降一档">降档·联赛风险</span>'
            elif it.get("downgraded") == "stake_half":
                _downgrade = ' <span class="tag-warn" title="50-60% 概率段（平局盲点区）减注50%">减注</span>'
            elif it.get("prob") and 0.50 <= it.get("prob", 0) < 0.60:
                _downgrade = ' <span class="tag-warn" title="50-60% 概率段（平局盲点区）已降档">降档</span>'
            # 2026-08-30: 已赛果的单注直接标注中/未中，免手工核对
            _res_badge = ""
            if sel in ("home", "draw", "away"):
                _p = next((x for x in predictions if x.get("match_id") == match_id), None)
                if _p is not None and _p.get("actual_home_score") is not None:
                    _ah, _aa = _p.get("actual_home_score"), _p.get("actual_away_score")
                    _actual = "home" if _ah > _aa else ("away" if _aa > _ah else "draw")
                    _stake = float(it.get("stake") or 0)
                    _odds = float(it.get("odds") or 0)
                    if sel == _actual:
                        _res_badge = (f'<span style="font-size:.68em;font-weight:700;'
                                      f'color:var(--green);margin-left:6px">✅ +&yen;{_stake * _odds:.0f}</span>')
                    else:
                        _res_badge = (f'<span style="font-size:.68em;font-weight:700;'
                                      f'color:var(--red);margin-left:6px">❌ -&yen;{_stake:.0f}</span>')
            html += (f'<div class="ticket-item"><span class="ti-match"><span class="jc-no" '
                     f'style="font-size:.62rem;padding:1px 5px;margin-right:4px">{_jc_no(match_id)}</span>'
                     f'{teams} [{sel_label}]{_downgrade}</span>'
                     f'<span class="ti-odds">@{it.get("odds", 0):.2f} / &yen;{it.get("stake", 0):.0f}</span>{_res_badge}</div>')
        return html

    if not stable and not value and not lottery:
        return f"""
  <div class="section-title">投注方案（三票制 60/30/10）<span class="st-note">单注投入 &yen;{total_stake:.0f} &middot; 资金池 &yen;{bankroll:.0f} &middot; 熔断 x{breaker_mult:.2f}</span></div>
  <div class="card" style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 16px;font-size:0.78rem;color:var(--text-secondary)">
    <span><b>今日单注空仓</b> — 当前无同时通过正 EV 与纪律门槛的推荐注单</span>
    <span style="font-size:0.72rem;color:var(--dim)">稳胆 0 · 搏冷 0 · 彩票 0</span>
  </div>"""

    return f"""
  <div class="section-title">投注方案（三票制 60/30/10）<span class="st-note">单注投入 &yen;{total_stake:.0f} &middot; 资金池 &yen;{bankroll:.0f} &middot; 熔断 x{breaker_mult:.2f}</span></div>
  <div class="ticket-grid">
    <div class="ticket-card"><h4 class="stable">稳胆（60%）</h4>{_items(stable)}</div>
    <div class="ticket-card"><h4 class="value">搏冷（30%）</h4>{_items(value)}</div>
    <div class="ticket-card"><h4 class="lottery">彩票（10%）</h4>{_items(lottery)}</div>
  </div>"""


def _site_spf_status(predictions):
    """竞彩胜平负在售状态：({match_id: "ok"|"no_spf"}, 有快照的日期集合)。

    数据源是 duty 入库的站点板快照（odds 三位齐全才算在售）。匹配复用挑战侧的
    match_prediction——「同场」的定义必须和提交/结算侧一致，不能另造一套。
    只用于页面标注（issue #1），不参与选腿/EV/账本。
    """
    status, dates = {}, set()
    try:
        from scripts.challenge_hourly import match_prediction
    except Exception:
        return status, dates
    by_date = {}
    for p in predictions or []:
        mid = str(p.get("match_id") or "")
        if len(mid) >= 10:
            by_date.setdefault(mid[:10], []).append(p)
    for d, preds in by_date.items():
        f = ROOT / "data" / "state" / "njstats" / "site_odds" / f"{d}.json"
        if not f.exists():
            continue
        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        dates.add(d)
        index = {(p.get("home_team", ""), p.get("away_team", "")): p for p in preds}
        for m in snap.get("matches") or []:
            p = match_prediction(index, m.get("home", ""), m.get("away", ""))
            if not p:
                continue
            odds = m.get("odds") or []
            ok = (len(odds) >= 3
                  and all(isinstance(o, (int, float)) and o > 1.0 for o in odds[:3]))
            mid = p.get("match_id")
            status[mid] = "ok" if ok or status.get(mid) == "ok" else "no_spf"
    return status, dates


def _news_section(today):
    """赛前情报：第三方新闻标题（store-only，不进模型/账本）。

    issue #1 与新闻源同一原则：外链内容只做展示，不做投注依据；
    标题来自外部，必须转义后再进 HTML。伤病/首发类排在最前。
    """
    f = ROOT / "data" / "daily" / today / "news.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    _order = {"injury": 0, "other": 1, "transfer": 2}
    _kind = {"injury": ("伤病/首发", "#e6a23c"), "transfer": ("转会", "#909399")}
    items = sorted(d.get("items") or [],
                   key=lambda x: _order.get(x.get("kind") or "other", 1))
    rows = []
    for it in items[:12]:
        url = str(it.get("url") or "")
        if not url.startswith("https://news.zhibo8.com/"):
            continue
        title = _esc(str(it.get("title") or ""))
        teams = _esc("、".join((it.get("teams") or [])[:3]))
        _label, _color = _kind.get(it.get("kind") or "other", ("", ""))
        badge = (f'<span style="font-size:.68em;color:{_color};border:1px solid {_color};'
                 f'border-radius:3px;padding:0 4px;margin-right:4px">{_label}</span>'
                 if _label else "")
        rows.append(
            f'<li style="margin:3px 0">{badge}<a href="{_esc(url)}" target="_blank" '
            f'rel="noopener noreferrer">{title}</a>'
            f'<span style="opacity:.55;font-size:.72em;margin-left:6px">{teams}</span></li>'
        )
    if not rows:
        return ""
    counts = d.get("counts") or {}
    _count_txt = (" · ".join(f"{k} {counts[k]}" for k in ("injury", "transfer", "other")
                             if counts.get(k)))
    return f"""
  <div class="section-title">赛前情报（第三方新闻）</div>
  <div class="ticket-card" style="opacity:.95">
    <div style="font-size:.72rem;opacity:.65;margin-bottom:4px">
      来源：{_esc(str(d.get('source') or ''))} · 抓取于 {_esc(str(d.get('generated_at') or ''))}
      {(' · ' + _esc(_count_txt)) if _count_txt else ''} ·
      按预测队名关键词自动匹配，未经核实，不构成投注建议。
    </div>
    <ul style="margin:4px 0 0 16px;padding:0;font-size:.82rem">{''.join(rows)}</ul>
  </div>"""


def _parlay_section(ticket, predictions, st_index=None):
    """串关方案（2026-08-08 新增；2026-08-13 用户确认保留——竞彩主流玩法）。

    数学纪律：串关吃双重抽水，模型概率高估（账本校准 0.55-0.60 段命中率仅 31.6%）。
    串票 EV 用账本校准命中率计算，只推荐 cal_ev>0 的；负 EV 串票 ⚠ 展示不出注；
    无腿/全负 → 空仓并展示校准表（为什么不该串）。
    """
    if not ticket:
        return ""
    # 旧格式 ticket（无 parlay key，2026-08-08 之前生成）不显示串关区，
    # 避免把"未计算过串关"误显示成"空仓"
    if "parlay" not in ticket:
        return ""
    parlay = ticket.get("parlay", [])
    cal = ticket.get("parlay_calibration", {})
    if not parlay and not cal:
        return ""

    sel_map = {"home": "主胜", "draw": "平局", "away": "客胜"}
    spf_status, snap_dates = _site_spf_status(predictions)

    def _ticket_state(t):
        legs = t.get("legs") or []
        dates = {str(lg.get("match_id", ""))[:10] for lg in legs}
        if not (dates & snap_dates):
            return "unverified"          # 没有当日快照：无法验证，保持原标注
        ok = bool(legs) and all(spf_status.get(lg.get("match_id")) == "ok" for lg in legs)
        return "ok" if ok else "reference"

    def _legs_html(t):
        html = ""
        for lg in t.get("legs", []):
            sel = lg.get("sel", "")
            _hp = lg.get("hit_prob")
            _cp = lg.get("cal_prob")
            _mp = lg.get("market_prob")
            _src = lg.get("source", "fusion")
            # 概率标注：融合腿显示 模型→校准；市场腿显示 市场公平→市场段实测
            if _src == "market":
                _prob_label = f"市场{(_mp or 0)*100:.0f}%→实际{(_hp or 0)*100:.0f}%"
                _src_badge = '<span class="pick-val away" style="font-size:.7em;margin-right:4px">市场</span>'
            else:
                _prob_label = (f"{lg.get('prob', 0)*100:.0f}%→{(_hp or _cp or 0)*100:.0f}%"
                               if _hp or _cp else f"{lg.get('prob', 0)*100:.0f}%")
                _src_badge = '<span class="pick-val home" style="font-size:.7em;margin-right:4px">模型</span>'
            _st = spf_status.get(lg.get("match_id"))
            _leg_date = str(lg.get("match_id", ""))[:10]
            if _st == "no_spf":
                _st_badge = ('<span class="pick-val draw" style="font-size:.68em;margin-left:4px" '
                             'title="站点快照显示该场胜平负未开售；此处是参考盘口赔率，不可按此投注">'
                             '竞彩未开售</span>')
            elif _st is None and _leg_date in snap_dates:
                _st_badge = ('<span class="pick-val draw" style="font-size:.68em;margin-left:4px" '
                             'title="站点快照中找不到该场，无法确认竞彩在售；按参考盘口展示">'
                             '未确认在售</span>')
            else:
                _st_badge = ""
            html += (f'<div class="ticket-item"><span class="ti-match"><span class="jc-no" '
                     f'style="font-size:.62rem;padding:1px 5px;margin-right:4px">{_jc_no(lg.get("match", ""))}</span>'
                     f'{_src_badge}{lg.get("home", "")} vs '
                     f'{lg.get("away", "")} <span style="opacity:.6">[{lg.get("league", "")}]</span> '
                     f'<span class="pick-val {"home" if sel=="home" else ("draw" if sel=="draw" else "away")}" '
                     f'style="font-size:.8em">{sel_map.get(sel, sel)} {_prob_label}</span></span>'
                     f'<span class="ti-odds">@{lg.get("odds", 0):.2f}</span>{_st_badge}</div>')
        return html

    def _ticket_html(t):
        state = _ticket_state(t)
        rec = t.get("recommended", False) and state != "reference"
        src = t.get("source", "calibrated")
        ptype = t.get("type", "")
        if t.get("recommended") and state == "reference":
            badge = ('<span class="pick-val draw" style="font-size:.75em" '
                     'title="含竞彩未开售/未确认在售的腿；EV 按参考盘口计算，系统不据此出注">'
                     '⚠ 仅参考·竞彩未确认在售</span>')
        elif rec:
            badge = '<span class="pick-val home" style="font-size:.75em">⭐ 正EV 推荐</span>'
        elif src == "market":
            badge = ('<span class="pick-val draw" style="font-size:.75em" '
                     'title="市场热门腿串关，期望为负（水钱），小注娱乐，勿重注">🎯 娱乐串</span>')
        else:
            badge = ('<span class="pick-val draw" style="font-size:.75em" '
                     'title="校准后负EV，系统不据此出注">⚠ 负EV</span>')
        _card_style = ("border-left:3px solid var(--green)" if rec
                       else "border-left:3px solid var(--amber)" if state == "reference"
                       else "border-left:3px solid var(--border-light)")
        # 市场腿串票用市场口径 ROI 标注（诚实），推荐串用校准口径
        ev = t.get("cal_ev", 0)
        if src == "market" and not rec:
            roi = t.get("market_roi", t.get("cal_roi", 0))
            ev = t.get("market_ev", ev)
            roi_label = "市场ROI"
        else:
            roi = t.get("cal_roi", 0)
            roi_label = "校准ROI"
        ev_html = (f'<span class="ts-chip"><div class="ts-label">{roi_label}</div>'
                   f'<div class="ts-val" style="color:{("var(--green)" if ev > 0 else "var(--red)")}">'
                   f'{"+" if ev > 0 else ""}{ev:.1f}元 ({roi:+.0%})</div></span>')
        return f"""
    <div class="ticket-card" style="{_card_style}">
      <h4 class="{'stable' if rec else 'lottery'}" style="display:flex;justify-content:space-between;align-items:center">
        <span>{ptype} {badge}{_settle_badge((st_index or {}).get(_tk_key(t)), t.get('stake', 0))}</span>
        <span style="font-size:.7rem;opacity:.7">全中@{t.get('total_odds', 0):.2f} · 实际命中率 {t.get('hit_prob_cal', 0)*100:.0f}%</span>
      </h4>
      {_legs_html(t)}
      <div class="ticket-summary" style="margin-top:6px">
        <div class="ts-chip"><div class="ts-label">投入</div><div class="ts-val">&yen;{t.get('stake', 0):.0f}</div></div>
        {ev_html}
        <div class="ts-chip"><div class="ts-label">最高奖金</div><div class="ts-val" style="color:var(--amber)">&yen;{t.get('potential', 0):.0f}</div></div>
        {f'<div class="ts-chip"><div class="ts-label">容错</div><div class="ts-val" style="color:var(--purple);font-size:.72rem">{t.get("worst_win", 0):.0f}元</div></div>' if t.get("worst_win") else ''}
      </div>
      <div style="font-size:.7rem;opacity:.65;margin-top:4px">{t.get('note', '')}</div>
    </div>"""

    cal_html = ""
    if cal:
        table = cal.get("table", {})
        overall = cal.get("overall", 0.433)
        n = cal.get("n", 0)
        rows = "".join(
            f"<tr><td>≥{float(k):.2f}</td><td>{v*100:.0f}%</td></tr>"
            for k, v in sorted(table.items(), key=lambda kv: float(kv[0]))
        )
        cal_html = f"""
  <details style="margin-top:8px;font-size:.75rem;color:var(--text-secondary)">
    <summary>串关为什么难赚钱？账本校准表（{n} 场，整体命中 {overall*100:.0f}%）</summary>
    <table style="margin-top:6px;border-collapse:collapse">
      <tr><th style="padding:2px 8px;text-align:left">模型概率段</th><th style="padding:2px 8px">实际命中率</th></tr>
      {rows}
      <tr><td style="padding:2px 8px">整体</td><td style="padding:2px 8px;text-align:center">{overall*100:.0f}%</td></tr>
    </table>
    <div style="margin-top:4px">串关 = 各腿命中率连乘 × 赔率连乘，天然吃双重抽水。模型概率系统性高估
    （0.70+ 段实际仅 50% 命中）；市场腿实际命中率也低于赔率隐含 → 绝大多数串票期望为负，
    标 🎯 娱乐串的小注参与即可，⭐ 正EV串才值得重注。</div>
  </details>"""

    if not parlay:
        _pool_n = ticket.get("parlay_pool_n")
        if isinstance(_pool_n, int) and _pool_n > 0:
            _why = f"可串池只有 {_pool_n} 条腿（2串1 需要 ≥2 条）"
        elif _pool_n == 0:
            _why = "可串池为空：没有腿同时过赔率区间与价值门槛（校准命中率×赔率）"
        else:
            _why = "无可串腿：未过价值门槛或不足 2 条"
        return f"""
  <div class="section-title">串关方案（过关玩法）</div>
  <div class="ticket-card" style="border-left:3px solid var(--amber)">
    <h4 class="lottery">🎯 今日无串关 <span class="pick-val draw" style="font-size:.75em">空仓</span></h4>
    <div style="font-size:.8rem;opacity:.8;padding:4px 0">
      {_why}—— 竞彩串关吃双重抽水，宁可不串，不送钱。
    </div>
    {cal_html}
  </div>"""

    cards = "".join(_ticket_html(t) for t in parlay)
    states = [_ticket_state(t) for t in parlay]
    n_rec = sum(1 for t, s in zip(parlay, states)
                if t.get("recommended") and s != "reference")
    n_ref = sum(1 for t, s in zip(parlay, states)
                if t.get("recommended") and s == "reference")
    _parlay_total = sum(t.get("stake", 0) for t in parlay)
    _parts = []
    if n_rec:
        _parts.append(f"{n_rec} 张推荐")
    if n_ref:
        _parts.append(f"{n_ref} 张仅参考")
    _parlay_label = ("· " + " · ".join(_parts)) if _parts else ""
    _parlay_label += f" · 投入¥{_parlay_total:.2f}"
    # 2026-08-30: 已结算时标题直接给战绩汇总
    _pw = _pl = 0
    _pnet = 0.0
    for _t in parlay:
        _st = (st_index or {}).get(_tk_key(_t))
        if _st and not _st.get("pending"):
            if _st.get("won"):
                _pw += 1
                _pnet += float(_st.get("return") or 0) - float(_t.get("stake") or 0)
            else:
                _pl += 1
                _pnet -= float(_t.get("stake") or 0)
    if _pw + _pl:
        _pnet_str = f"{_pnet:+.0f}".replace("+", "+")
        _parlay_label += (f' · 已结算 <span style="color:{("var(--green)" if _pnet >= 0 else "var(--red)")}'
                          f'">中{_pw} 亏{_pl} 净{_pnet_str}元</span>')
    _spf_note = ("在售状态以站点赔率快照为准；标「仅参考」的串票含竞彩未开售/未确认在售的腿"
                 if snap_dates else
                 "⚠ 今日未找到站点赔率快照，「竞彩在售」未验证；串票按参考盘口展示")
    return f"""
  <div class="section-title">串关方案（过关玩法）{_parlay_label}</div>
  <div style="font-size:.7rem;opacity:.6;margin:2px 0 8px">{_spf_note}</div>
  <div class="ticket-grid">{cards}</div>
  {cal_html}"""


def _score_parlay_section(ticket, st_index=None):
    """比分串（波胆过关）— 2026-08-13 科学性改造后恢复。

    84 张 0 中（ROI -100%）教训：DJYY top_scores 概率未校准（0-0 系统性
    高估 2-3 倍）+ 模拟赔率表算 EV 自欺欺人。改造后只出官方真实波胆赔率
    （crs_odds）且校准概率×赔率>1 的正 EV 组合；无官方赔率/全负 EV →
    空仓不展示（说明原因）。
    """
    if not ticket:
        return ""
    sp = ticket.get("score_parlay", [])
    if not sp:
        return ""
    sel_odds_src = {"official": "官方", "simulated": "模拟"}

    def _leg_short(lg):
        return (f'<span class="sp-leg"><span class="jc-no" '
                f'style="font-size:.6rem;padding:1px 4px;margin-right:3px">{_jc_no(lg.get("match", ""))}</span>'
                f'{lg.get("home", "")[:6]}'
                f'<b>{lg.get("score", "")}</b>@{lg.get("odds", 0):.1f}</span>')

    rows = []
    for t in sp:
        ptype = t.get("type", "")
        legs = " + ".join(_leg_short(lg) for lg in t.get("legs", []))
        hit = t.get("hit_prob", 0)
        n_bets = t.get("n_bets", 1)
        worst = t.get("worst_win", 0)
        src = sel_odds_src.get(t.get("odds_source", "simulated"), "模拟")
        worst_cell = (f'<span style="color:var(--amber);font-weight:700">错1场¥{worst:.0f}</span>'
                      if worst > 0 else '<span style="color:var(--dim)">—</span>')
        rows.append(
            f'<tr>'
            f'<td><b>{ptype}</b>{_settle_badge((st_index or {}).get(_tk_key(t)), t.get("stake", 0))}<br><span style="font-size:.62rem;color:var(--dim)">{n_bets}注 · {src}赔率</span></td>'
            f'<td>{legs}</td>'
            f'<td style="text-align:right;color:var(--dim)">{hit*100:.2f}%</td>'
            f'<td style="text-align:right">¥{t.get("stake", 0):.0f}</td>'
            f'<td style="text-align:right;color:var(--green);font-weight:700">¥{t.get("potential", 0):.0f}</td>'
            f'<td>{worst_cell}</td>'
            f'</tr>'
        )

    details = []
    for t in sp:
        ptype = t.get("type", "")
        note = t.get("note", "")
        leg_lines = "".join(
            f'<div class="ts-row"><span>{lg.get("home","")} vs {lg.get("away","")} '
            f'<span style="opacity:.6">[{lg.get("league","")}]</span></span>'
            f'<span>比分 {lg.get("score","")} · 校准 {lg.get("cal_prob", lg.get("prob",0))*100:.1f}% · @{lg.get("odds",0):.1f}</span></div>'
            for lg in t.get("legs", [])
        )
        details.append(
            f'<details class="sp-detail"><summary>{ptype} · {note[:40]}</summary>{leg_lines}</details>'
        )

    n = len(sp)
    total_stake = sum(t.get("stake", 0) for t in sp)
    max_pot = max((t.get("potential", 0) for t in sp), default=0)
    _sw = _sl = 0
    _snet = 0.0
    for _t in sp:
        _st = (st_index or {}).get(_tk_key(_t))
        if _st and not _st.get("pending"):
            if _st.get("won"):
                _sw += 1
                _snet += float(_st.get("return") or 0) - float(_t.get("stake") or 0)
            else:
                _sl += 1
                _snet -= float(_t.get("stake") or 0)
    _settled_label = (f' · <span style="color:{("var(--green)" if _snet >= 0 else "var(--red)")}'
                      f'">中{_sw} 亏{_sl} 净{_snet:+.0f}元</span>') if _sw + _sl else ""

    return f"""
  <div class="section-title">🎯 比分串（波胆过关）
    <span style="float:right;font-weight:500;text-transform:none;letter-spacing:0;color:var(--dim)">{n}张 · 投入¥{total_stake:.0f} · 最高¥{max_pot:.0f}{_settled_label}</span>
  </div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>玩法</th><th>比分组合（单腿 @官方赔率）</th><th style="text-align:right">校准概率</th><th style="text-align:right">投入</th><th style="text-align:right">最高奖金</th><th>容错</th></tr>
    {''.join(rows)}
  </table>
  </div>
  <div style="font-size:.68rem;color:var(--dim);padding:4px 2px">
    2026-08-13 科学性改造：仅出<b>官方真实波胆赔率</b>且<b>校准概率×赔率&gt;1 的正 EV</b>组合
    （DJYY 概率系统性高估已压校准，模拟赔率不再使用——84 张 0 中的教训）。
    <details style="display:inline"><summary style="cursor:pointer;display:inline;color:var(--blue)"> 腿明细</summary>{''.join(details)}</details>
  </div>"""


def _parlay_settle_section(settle: dict | None, target_date: str = "") -> str:
    """串关/波胆真实复盘（2026-08-10 新增）：真实出票的结算结果。

    与 parlay_report（历史回测模拟）不同，这里是 ticket_plan 里真实
    出过的串票用当日赛果逐腿结算的命中率/ROI。诚实展示：
    - 胜平负串：整票命中率 + ROI
    - 波胆串：整票命中率 + 单腿命中率（精确比分极难，腿级信息量更大）

    2026-08-10 改：按出票日渲染——复盘归属当天页面，当天无票不显示
    区块（不再把全部历史串票堆在每个页面）。
    """
    if not settle:
        return ""
    day = (settle.get("by_date") or {}).get(target_date or "")
    if not day:
        return ""

    def _fmt_roi(v):
        return "—" if v is None else f"{v:+.1%}"

    def _ticket_rows(kind):
        tickets = (day.get(kind) or {}).get("tickets") or []
        out = []
        for t in tickets:
            mark = "✅" if t["won"] else ("⏳" if t["pending"] else "❌")
            legs = " + ".join(
                f"<span class='jc-no' style='font-size:.6rem;padding:1px 4px;margin-right:3px'>{_jc_no(l.get('match') or '')}</span>"
                f"{l['home'][:6]}({l['sel'] or l['score']})" for l in t["legs"]
            )
            leg_marks = " ".join(
                "✓" if l["hit"] is True else ("·" if l["hit"] is None else "✗")
                for l in t["legs"]
            )
            ret = "—" if t["pending"] else f"¥{t['return']:.2f}"
            out.append(
                f'<tr><td>{t["type"]} {mark}</td>'
                f'<td>{legs} <span style="color:var(--dim);font-size:.7rem">{leg_marks}</span></td>'
                f'<td style="text-align:right">¥{t["stake"]:.0f} → {ret}</td></tr>'
            )
        return "".join(out)

    def _stat_line(title, s, extra=None):
        if not s or not s.get("n_tickets"):
            return ""
        hr = f"{s['hit_rate']:.0%}" if s.get("hit_rate") is not None else "—"
        roi = s.get("roi")
        roi_html = ("—" if roi is None else
                    f'<b style="color:{"var(--green)" if roi > 0 else "var(--red)"}">{roi:+.0%}</b>')
        _stake_committed = s.get("stake_committed", s["stake"])
        _stake_settled = s["stake"]
        if _stake_committed != _stake_settled:
            _stake_text = f"已出票¥{_stake_committed:.2f} / 已结算¥{_stake_settled:.2f}"
        else:
            _stake_text = f"投入¥{_stake_settled:.2f}"
        parts = [f"出票{s['n_tickets']}/{s['n_settled']}结算", f"中{s['n_won']}张({hr})", _stake_text,
                 f"回报¥{s['return']:.2f}", f"ROI {roi_html}"]
        if extra:
            parts.append(extra)
        by_type = ""
        if s.get("by_type"):
            by_type = " · ".join(
                f"{k}: {v['n']}张中{v['won']} ROI {_fmt_roi(v.get('roi'))}"
                for k, v in s["by_type"].items()
            )
        return (f'<div class="ts-statline"><b>{title}</b> · '
                + " · ".join(parts) + (f'<br><span style="color:var(--dim);font-size:.68rem">{by_type}</span>' if by_type else "") + "</div>")

    p = (day.get("parlay") or {}).get("stats")
    sp = (day.get("score_parlay") or {}).get("stats")
    p_tickets = (day.get("parlay") or {}).get("tickets") or []
    sp_tickets = (day.get("score_parlay") or {}).get("tickets") or []
    if not p_tickets and not sp_tickets:
        return ""

    verdict = ""
    if (p and p.get("n_settled")) or (sp and sp.get("n_settled")):
        verdict = "<div class='ts-note'>"
        if p and p.get("roi") is not None:
            if p["roi"] > 0:
                verdict += f"胜平负串实证 ROI 为正（+{p['roi']:.0%}），但样本极少（{p['n_won']}张），不足以下结论。"
            else:
                verdict += f"胜平负串实证 ROI {p['roi']:+.0%}，串关吃双重抽水。"
        if sp and sp.get("n_settled"):
            verdict += f"波胆整票命中率 {sp['hit_rate']:.0%}（{sp['n_won']}/{sp['n_settled']}），单腿 {sp['leg_hit_rate']:.0%}——两腿都中太难，纯彩票定位。"
        verdict += "</div>"

    return f"""
    <div id="parlay-settle">
      <div class="section-title">串关/波胆复盘 <span style="font-weight:500;text-transform:none;letter-spacing:0;color:var(--dim)">当日真实出票结算</span></div>
      {_stat_line("🎰 胜平负串", p)}
      {_stat_line("🎰 比分串（波胆）", sp, (f"单腿命中 {sp['leg_hit_rate']:.0%}（{sp['n_legs_settled']}腿）" if sp and sp.get('leg_hit_rate') is not None else None))}
      <details class="sp-detail" style="margin-top:6px"><summary>📋 逐票明细（{len(p_tickets)+len(sp_tickets)} 张）</summary>
      <table class="edge-table" style="margin-top:6px">
        <tr><th>玩法</th><th>组合（腿 ✓/✗/·）</th><th style="text-align:right">投入 → 回报</th></tr>
        {_ticket_rows('parlay')}
        {_ticket_rows('score_parlay')}
      </table>
      </details>
      {verdict}
    </div>"""


def _ev_section(ev_report):
    """EV 价值区报告：全量已结算预测按赔率区间×联赛分层的 ROI"""
    if not ev_report or not ev_report.get("total"):
        return ""
    t = ev_report["total"]
    layers = ev_report.get("layers", {})
    leagues = ev_report.get("leagues", {})
    takeaways = ev_report.get("takeaways", [])

    def _cls(roi):
        return "green" if roi > 0 else ("red" if roi < -0.10 else "amber")

    rows = "".join(
        f"<tr><td>{k}</td><td>{v['n']}</td><td>{v['hit_rate']*100:.1f}%</td>"
        f"<td class='{_cls(v['roi'])}'>{v['roi']*100:+.1f}%</td>"
        f"<td class='{_cls(v['roi'])}'>{v['verdict']}</td></tr>"
        for k, v in layers.items()
    )
    lg_rows = "".join(
        f"<tr><td>{k}</td><td>{v['n']}</td><td>{v['hit_rate']*100:.1f}%</td>"
        f"<td class='{_cls(v['roi'])}'>{v['roi']*100:+.1f}%</td></tr>"
        for k, v in leagues.items()
    )
    tk = "".join(f"<li style='color:var(--text-secondary);font-size:0.82rem;margin:3px 0;'>{x}</li>" for x in takeaways)

    return f"""
  <div class="section-title">🎯 EV 价值区报告（全量复盘）</div>
  <div class="card" style="padding:20px;margin-bottom:14px;">
    <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:16px;">
      <div class="stat"><div class="label">已结算场次</div><div class="value">{t['n']}</div></div>
      <div class="stat"><div class="label">方向命中率</div><div class="value">{t['hit_rate']*100:.1f}%</div></div>
      <div class="stat"><div class="label">整体 ROI（每场押1单位）</div><div class="value {_cls(t['roi'])}">{t['roi']*100:+.1f}%</div></div>
    </div>
    <ul style="padding-left:18px;margin-bottom:16px;">{tk}</ul>
    <div class="ev-grid">
      <div class="ev-table-wrap">
        <div style="font-weight:700;font-size:0.85rem;margin-bottom:8px;color:var(--text);">按赔率区间（押注结构诊断）</div>
        <table class="data-table" style="width:100%;font-size:0.82rem;">
          <tr><th>区间</th><th>场数</th><th>命中率</th><th>ROI</th><th>判读</th></tr>
          {rows}
        </table>
      </div>
      <div class="ev-table-wrap">
        <div style="font-weight:700;font-size:0.85rem;margin-bottom:8px;color:var(--text);">按联赛（≥3场）</div>
        <table class="data-table" style="width:100%;font-size:0.82rem;">
          <tr><th>联赛</th><th>场数</th><th>命中率</th><th>ROI</th></tr>
          {lg_rows}
        </table>
      </div>
    </div>
  </div>
"""


def _results_section(results, predictions, review_ledger=None, target_date=""):
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

    # 账本盈亏索引（results.json 不含 pnl 字段，盈亏以账本为准——
    # 2026-08-13 修复：此前页面盈亏列永远 0，总盈亏与 review.json 对不上）
    _ledger_pnl: dict = {}
    for _rl in review_ledger or []:
        _mid = _rl.get("match_id", "")
        if _mid:
            _ledger_pnl[_mid] = _rl.get("pnl", 0)
            _fx = _extract_fixture(_mid)
            if _fx:
                _ledger_pnl.setdefault(_fx, _rl.get("pnl", 0))

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
            # 队名匹配（最可靠；绝不用裸场次号跨日期匹配，避免伪造赛果）
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

        # 预测结果：必须与 engine/main.py 的 _pick_direction / 结算口径完全一致
        # （2026-08-13 修复：此前页面用纯 argmax，未同步 draw_alert 平局改判，
        #  如 8/7 横滨水手——账本/预测 direction=draw 记 ✗，页面却按 argmax 显示 ✓，自相矛盾）
        ph = pred.get("home_win_prob", 0)
        pd = pred.get("draw_prob", 0)
        pa = pred.get("away_win_prob", 0)
        _alert = pred.get("draw_alert")
        if ph >= pd and ph >= pa:
            predicted = "home"
        elif pd >= ph and pd >= pa:
            predicted = "draw"
        else:
            predicted = "away"
        # draw_alert 平局改判已全部停用（2026-08-17，实盘证伪：
        # balanced/cold 8/13 停用、R1 league_draw 8/17 停用——8/13 起 8 场
        # 0 中，5 场把正确 argmax 改错）。统一纯 argmax 展示。
        pred_label = {"home": "主胜", "draw": "平局", "away": "客胜"}[predicted]

        hit = predicted == actual
        if hit:
            hits += 1

        # Brier score: sum of (prob - indicator)^2
        ind_h = 1.0 if actual == "home" else 0.0
        ind_d = 1.0 if actual == "draw" else 0.0
        ind_a = 1.0 if actual == "away" else 0.0
        brier = (ph - ind_h)**2 + (pd - ind_d)**2 + (pa - ind_a)**2
        total_brier += brier

        # 投注盈亏：results.json 无 pnl 字段 → 优先账本 pnl（含让球单票结算），
        # 其次 results 自带（fallback 路径构造时已从账本带入）
        pnl = _ledger_pnl.get(mid)
        if pnl is None:
            _fx = _extract_fixture(mid)
            pnl = _ledger_pnl.get(_fx) if _fx else None
        if pnl is None:
            pnl = r.get("pnl", 0) or 0
        total_pnl += pnl

        hit_cls = "hit" if hit else "miss"
        hit_icon = "✓" if hit else "✗"
        pnl_color = "var(--green)" if pnl > 0 else "var(--red)" if pnl < 0 else "var(--dim)"
        pred_prob = {"home": ph, "draw": pd, "away": pa}[predicted]

        # 让球胜负平（竞彩玩法口径）：handicap 为让球数，负=主队让球
        # 如 -1 表示主让1球 → 让球后主队进球 = home_score + handicap（-1 → 减1球）
        hcap = pred.get("handicap")
        hcap_label = ""
        hcap_result = ""
        if hcap is not None:
            try:
                _h = float(hcap)
                _hh = home_score + _h  # handicap 负=主让，让球后主队净胜减去让球数
                if _hh > away_score:
                    hcap_result = "让球主胜"
                elif _hh == away_score:
                    hcap_result = "让球平"
                else:
                    hcap_result = "让球客胜"
                _hcap_str = f"{_h:+.0f}" if _h == int(_h) else f"{_h:+.1f}"
                hcap_label = f"({_hcap_str})"
                # 颜色：让球结果
                if "主胜" in hcap_result:
                    hcap_color = "var(--green)"
                elif "客胜" in hcap_result:
                    hcap_color = "var(--red)"
                else:
                    hcap_color = "var(--amber)"
                hcap_result = f'<span style="color:{hcap_color};font-size:0.72rem;">{hcap_result}</span>'
            except (TypeError, ValueError):
                hcap_label = hcap_result = ""

        rows += f"""
        <tr class="{hit_cls}">
          <td><span class="jc-no" style="font-size:.62rem;padding:1px 5px;margin-right:4px">{_jc_no(mid)}</span>{pred.get('home_team', '')} vs {pred.get('away_team', '')}</td>
          <td style="font-weight:800;text-align:center;">{home_score}-{away_score}<div style="font-size:0.62rem;font-weight:500;color:var(--dim);">{home_score + away_score}球</div></td>
          <td>{actual_label}</td>
          <td style="font-size:0.75rem;color:var(--dim);">{hcap_label} {hcap_result}</td>
          <td>{pred_label} ({pred_prob:.0%})</td>
          <td style="text-align:center;"><span class="result-icon {hit_cls}">{hit_icon}</span></td>
          <td style="font-family:monospace;font-size:0.68rem;">{brier:.3f}</td>
          <td style="color:{pnl_color};font-weight:600;">{'+' if pnl > 0 else ''}{pnl:.0f}</td>
        </tr>"""

    if matched == 0:
        return ""

    hit_rate = hits / matched
    avg_brier = total_brier / matched

    # 分层评价（2026-08-06 借鉴 MBS 方法论）：从 review.json 读 LogLoss/进球框架/概率分段
    _layered_html = ""
    # review.json 的日期用页面目标日期，而不是从 results[0].match_id 反推
    # （后者在赛果被跨日期匹配时会把别的日期的 review 数据加载进本页）。
    _rv_date = target_date if target_date else ""
    _rv = _load_json(ROOT / "data" / "daily" / _rv_date / "review.json", None) if _rv_date else None
    if _rv and _rv.get("layered"):
        _ly = _rv["layered"]
        _ll = _ly.get("log_loss_final")
        _gf = _ly.get("goal_framework", {})
        _bands = _ly.get("prob_bands", {})
        _fres = _ly.get("freshness_groups", {})
        _chips = ""
        if _ll is not None:
            _chips += f'<div class="ts-chip"><div class="ts-label">LogLoss(final)</div><div class="ts-val" style="color:{"var(--green)" if _ll < 0.9 else "var(--amber)"}">{_ll:.3f}</div></div>'
        if _gf.get("n"):
            _gf_rate = _gf["hits"] / _gf["n"]
            _chips += f'<div class="ts-chip"><div class="ts-label">进球框架</div><div class="ts-val" style="color:{"var(--green)" if _gf_rate >= 0.5 else "var(--amber)"}">{_gf_rate:.0%} ({_gf["hits"]}/{_gf["n"]})</div></div>'
        if _bands:
            _band_str = " · ".join(f"{k}:{v['hit_rate']:.0%}" for k, v in _bands.items())
            _chips += f'<div class="ts-chip"><div class="ts-label">概率分段</div><div class="ts-val" style="font-size:0.68rem;">{_band_str}</div></div>'
        if _fres:
            _fres_str = " · ".join(f"{k}:{v['hit_rate']:.0%}" for k, v in _fres.items())
            _chips += f'<div class="ts-chip"><div class="ts-label">新鲜度分层</div><div class="ts-val" style="font-size:0.68rem;">{_fres_str}</div></div>'
        if _chips:
            _layered_html = f'<div class="results-summary" style="margin-top:6px;">{_chips}</div>'

    return f"""
  <div class="section-title">赛果复盘</div>
  <div class="results-summary">
    <div class="ts-chip"><div class="ts-label">命中率</div><div class="ts-val" style="color:{'var(--green)' if hit_rate >= 0.5 else 'var(--red)'}">{hit_rate:.0%} ({hits}/{matched})</div></div>
    <div class="ts-chip"><div class="ts-label">平均Brier</div><div class="ts-val" style="color:{'var(--green)' if avg_brier < 0.5 else 'var(--amber)'}">{avg_brier:.3f}</div></div>
    <div class="ts-chip"><div class="ts-label">总盈亏</div><div class="ts-val" style="color:{'var(--green)' if total_pnl >= 0 else 'var(--red)'}">&yen;{total_pnl:+.0f}</div></div>
  </div>
  {_layered_html}
  <div class="results-table-wrap">
    <table class="results-table">
      <tr><th>比赛</th><th>比分</th><th>实际</th><th>让球</th><th>预测</th><th>命中</th><th>Brier</th><th>盈亏</th></tr>
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
    sha = bundle.get("bundle_sha256", "暂无")
    created = bundle.get("created_at", "")

    # 融合权重从实际状态文件读取，避免页面写死过期权重（曾显示"模型60/市场25"，
    # 生产实际是"模型10/市场75"）。
    fusion_display = "模型? / 市场? / DJYY?"
    try:
        _fw_path = ROOT / "data" / "state" / "fusion_weights.json"
        _fw = json.loads(_fw_path.read_text(encoding="utf-8")) if _fw_path.exists() else {}
        _ch = _fw.get("champion", {})
        if _ch:
            fusion_display = (
                f"模型{_ch.get('model', 0) * 100:.0f} / "
                f"市场{_ch.get('market', 0) * 100:.0f} / "
                f"DJYY{_ch.get('djyy', 0) * 100:.0f}"
            )
    except Exception:
        pass

    # 模型配置实时值（2026-08-30：旧版此处全部写死，与生产漂移）
    _dc_w, _mc_w, _sims, _lgbm_blend, _cal_desc = 0.6, 0.4, 50000, "关闭", "关闭"
    try:
        _pc = json.loads((ROOT / "config" / "prediction.json").read_text(encoding="utf-8"))
        _dc_w = _pc.get("ensemble", {}).get("dixon_coles_weight", 0.6)
        _mc_w = _pc.get("ensemble", {}).get("monte_carlo_weight", 0.4)
        _sims = _pc.get("prediction", {}).get("simulations", 50000)
        _pf = _pc.get("fusion", {}).get("post_fusion", {})
        _lw = _pc.get("fusion", {}).get("lgbm_weight", 0)
        if _pf.get("lgbm_blend"):
            _lgbm_blend = f"启用 ({_lw*100:.0f}%)"
        if _pf.get("isotonic"):
            _cal_desc = "Isotonic"
        elif _pf.get("temperature"):
            _cal_desc = "Temperature"
    except Exception:
        pass

    tier_cls = "safe" if tier <= 1 else "caution" if tier <= 2 else "danger"
    tier_label = f"T{tier}" + (" · " + tier_reason if tier_reason else "")

    # Shadow 模式：熔断器只展示不实际扣减注额，明确标注避免误解
    _activation = "shadow"
    try:
        _strat = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
        _activation = _strat.get("activation_mode", "shadow")
    except Exception:
        pass
    if _activation != "real":
        tier_label = f"T{tier} · Shadow" + (" · " + tier_reason if tier_reason else "")

    return f"""
  <div id="system-panel">
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
        <div class="sys-row"><span class="k">集成</span><span class="v">DC {_dc_w*100:.0f} + MC {_mc_w*100:.0f}</span></div>
        <div class="sys-row"><span class="k">融合</span><span class="v">{fusion_display}</span></div>
        <div class="sys-row"><span class="k">LGBM掺混</span><span class="v">{_lgbm_blend}</span></div>
        <div class="sys-row"><span class="k">校准</span><span class="v">{_cal_desc}</span></div>
        <div class="sys-row"><span class="k">MC模拟</span><span class="v">{_sims:,}次</span></div>
        <div class="sys-row"><span class="k">信任区间</span><span class="v">Wilson</span></div>
      </div>
    </div>
  </div>"""


def _battle_board(ticket, predictions, parlay, breaker_tier=0, breaker_mult=1.0):
    """🎯 统一决策摘要栏（2026-11-27 重构）：合并原6KPI与作战板，消弭0单红字与敞口错漏。"""
    stable = ticket.get("stable", []) if ticket else []
    value = ticket.get("value", []) if ticket else []
    lottery = ticket.get("lottery", []) if ticket else []
    total_stake = float(ticket.get("total_stake") or 0) if ticket else 0.0
    parlay_stake = float(ticket.get("parlay_stake") or 0) if ticket else 0.0
    if ticket and ticket.get("total_exposure") is not None:
        actual_exposure = float(ticket.get("total_exposure") or 0)
    else:
        actual_exposure = total_stake + parlay_stake
    exp_roi = float(ticket.get("expected_roi") or 0) if ticket else 0.0
    bankroll = float(ticket.get("bankroll") or 0) if ticket else 0.0
    n_bets = len(stable) + len(value) + len(lottery)
    n_parlay = len(parlay) if isinstance(parlay, list) else 0
    val_matches = {it.get("match", "") for it in (stable + value)}
    n_val = sum(1 for p in (predictions or []) if _is_value(p, val_matches))

    if n_bets > 0:
        roi_text = f"{exp_roi * 100:+.1f}%"
        roi_color = "var(--green)" if exp_roi > 1e-6 else ("var(--red)" if exp_roi < -1e-6 else "var(--dim)")
    else:
        roi_text = "—"
        roi_color = "var(--dim)"

    if n_bets > 0 or n_parlay > 0:
        action_val = f"{n_bets} 单" + (f" + {n_parlay} 串" if n_parlay else "")
        action_color = "var(--green)"
    else:
        action_val = "空仓观望"
        action_color = "var(--dim)"

    def _mini(name, items, cls):
        return f'<div class="bb-ticket"><span class="bb-tag {cls}">{name}</span><span class="bb-n">{len(items)} 注</span></div>'

    if n_bets == 0:
        reasons = []
        if predictions:
            reasons.append(("联赛禁投", sum(1 for p in predictions if p.get("league_forbidden"))))
            reasons.append(("方向置信不足", sum(1 for p in predictions if p.get("direction_low_confidence"))))
            reasons.append(("合成赔率", sum(1 for p in predictions if p.get("odds_synthetic"))))
            reasons.append(("无正EV", sum(1 for p in predictions if not p.get("kelly_edge"))))
        _rs = " · ".join(f"{k} {v}" for k, v in reasons if v) or "赔率/概率未达门槛"
        foot_html = (
            f'<div class="bb-empty">今日无单注价值推荐 '
            f'<span style="color:var(--dim);font-size:.72rem">（{len(predictions or [])} 场已扫描 · 拦截: {_rs}）</span>'
            f'<span style="font-size:.68rem;color:var(--dim);margin-left:8px">空仓也是决策</span></div>'
        )
    else:
        foot_html = (
            f'<div class="bb-tickets">'
            f'{_mini("稳胆60%", stable, "stable")}{_mini("搏冷30%", value, "value")}{_mini("彩票10%", lottery, "lottery")}'
            f'</div>'
        )

    provenance_summary = _market_follow_summary(predictions)
    provenance_html = (
        f'<span class="provenance-inline" title="方向是否与去水市场一致，以及本场实际用于融合的市场权重">{provenance_summary}</span>'
        if provenance_summary else ""
    )

    return f"""
  <div class="summary-wrap">
    <div class="summary-strip">
      <div class="sum-cell">
        <div class="sum-label">今日场次</div>
        <div class="sum-val">{len(predictions or [])}</div>
        <div class="sum-sub">{n_val} 场价值候选</div>
      </div>
      <div class="sum-cell">
        <div class="sum-label">今日出手</div>
        <div class="sum-val" style="color:{action_color}">{action_val}</div>
        <div class="sum-sub">{'单注+串关建议' if (n_bets or n_parlay) else '负 EV 不出手'}</div>
      </div>
      <div class="sum-cell">
        <div class="sum-label">实际总敞口</div>
        <div class="sum-val" style="color:var(--amber)">&yen;{actual_exposure:.0f}</div>
        <div class="sum-sub">单注 &yen;{total_stake:.0f} &middot; 串关 &yen;{parlay_stake:.0f}</div>
      </div>
      <div class="sum-cell">
        <div class="sum-label">预期回报</div>
        <div class="sum-val" style="color:{roi_color}">{roi_text}</div>
        <div class="sum-sub">{'三票组合预期 ROI' if n_bets else '无单注不出手'}</div>
      </div>
      <div class="sum-cell">
        <div class="sum-label">资金池 · 熔断</div>
        <div class="sum-val">&yen;{bankroll:.0f}</div>
        <div class="sum-sub" style="color:{'var(--green)' if breaker_tier == 0 else 'var(--red)'}">T{breaker_tier} &middot; x{breaker_mult:.2f}</div>
      </div>
    </div>
    <div class="summary-foot">
      {foot_html}{provenance_html}
      <span style="font-size:0.68rem;color:var(--dim)">资金与敞口统一口径（含串关）</span>
    </div>
  </div>"""


def _evo_dashboard(evo=None, state_dir=None):
    """🧬 进化仪表盘（2026-08-30）：系统自进化的四个状态面统一上页面。"""
    if evo is None:
        if state_dir is not None:
            _sd = Path(state_dir)
            evo = {
                "tracker": _load_json(_sd / "upgrade_tracker.json", None),
                "edge": _load_json(_sd / "edge_calibration.json", None),
                "calib": _load_json(_sd / "calibration_status.json", None),
                "lgbm": _load_json(_sd / "lgbm_status.json", None),
            }
        else:
            evo = _evo_state()
    cards = []

    tr = evo.get("tracker")
    if tr:
        verdict = tr.get("verdict", "WARMING_UP")
        v2 = tr.get("v2_clean") or {}
        base = tr.get("baseline_pre_upgrade") or {}
        v_map = {"IMPROVED": ("修复生效", "var(--green)"), "REGRESSED": ("效果退化", "var(--red)"),
                 "NEUTRAL": ("效果中性", "var(--amber)"), "WARMING_UP": ("样本预热中", "var(--dim)"),
                 "WAITING_DATA": ("等待数据", "var(--dim)")}
        label, color = v_map.get(verdict, (verdict, "var(--dim)"))
        _detail = ""
        if v2 and v2.get("n"):
            _detail = f"洁净组 n={v2['n']} · Brier {v2.get('brier', 0):.4f} / 命中 {v2.get('hit_rate', 0)*100:.1f}%"
            if base and base.get("brier"):
                _detail += f" · 基线 {base['brier']:.4f}"
        pvm = tr.get("paired_vs_market") or {}
        if pvm.get("n", 0) >= 2:
            m_map = {"BEATING": ("优于市场", "var(--green)"),
                     "LOSING": ("劣于市场", "var(--red)"),
                     "TIED": ("与市场持平", "var(--amber)"),
                     "WARMING_UP": ("与市场持平(样本少)", "var(--dim)"),
                     "NO_DATA": ("无市场对照", "var(--dim)")}
            m_label, m_color = m_map.get(pvm.get("verdict"),
                                          (pvm.get("verdict", "-"), "var(--dim)"))
            _detail += (f"<br><span style='color:{m_color}'>vs 市场 "
                        f"{m_label}</span>"
                        f"（Δ{pvm.get('delta_brier', 0):+.4f} · t={pvm.get('t', 0):+.2f}"
                        f" · n={pvm.get('n')}）")
        ew = tr.get("effective_fusion_weights") or {}
        if ew.get("effective"):
            e = ew["effective"]
            raw = ew.get("raw") or {}
            dj_rate = ew.get("djyy_hit_rate")
            if e.get("djyy"):
                _w_line = (f"有效权重 model {e['model']*100:.1f}% / market {e['market']*100:.1f}%"
                           f" / djyy {e['djyy']*100:.1f}%")
            elif raw.get("djyy") and dj_rate is not None:
                _w_line = (f"DJYY 覆盖 {dj_rate*100:.1f}% · 命中时 "
                           f"{raw.get('model', 0)*100:.0f}/{raw.get('market', 0)*100:.0f}/{raw.get('djyy', 0)*100:.0f}"
                           f" · 缺失时 {e['model']*100:.1f}/{e['market']*100:.1f}")
            else:
                _w_line = f"有效权重 model {e['model']*100:.1f}% / market {e['market']*100:.1f}%（DJYY未启用）"
            _detail += f"<br><span style='color:var(--dim)'>{_w_line}</span>"
        cards.append('<div class="evo-card"><div class="evo-title">升级效果（Q1·vs 旧链，不代表有 edge）</div>'
                     f'<div class="evo-verdict" style="color:{color}">{label}</div>'
                     f'<div class="evo-detail">{_detail or "chain=v2 样本随结算累积"}</div></div>')

    eg = evo.get("edge")
    if eg and eg.get("edge_buckets"):
        _rows = "".join(
            f'<span class="evo-chip">{b["bucket"]}: '
            f'<b style="color:{"var(--green)" if b.get("roi_proxy", 0) > 0 else "var(--red)"}">{b.get("roi_proxy", 0)*100:+.0f}%</b>'
            f'<i>(n={b["n"]})</i></span>'
            for b in eg["edge_buckets"] if b.get("n"))
        cards.append('<div class="evo-card"><div class="evo-title">EV 校准体检（价值幻觉监控）</div>'
                     f'<div class="evo-detail">实际ROI代理 vs 声称edge: {_rows}</div></div>')

    cb = evo.get("calib")
    if cb:
        _c_color = "var(--green)" if cb.get("enabled") else "var(--dim)"
        _c_label = "已启用" if cb.get("enabled") else "待样本"
        cards.append('<div class="evo-card"><div class="evo-title">校准层自进化</div>'
                     f'<div class="evo-verdict" style="color:{_c_color}">{_c_label}</div>'
                     f'<div class="evo-detail">洁净样本 {cb.get("clean_n", 0)} · {str(cb.get("reason", ""))[:60]}</div></div>')

    lb = evo.get("lgbm")
    if lb:
        _l_color = "var(--green)" if lb.get("ready") else "var(--dim)"
        if lb.get("ready"):
            _l_label = "可评估启用"
        elif lb.get("trained"):
            _l_label = "已训练·未达标"
        else:
            _l_label = "待样本"
        if lb.get("trained") and lb.get("holdout_n"):
            _d_v2 = lb.get("delta_vs_fusion_v2")
            _d_txt = f" · Δvs融合 {_d_v2:+.4f}" if _d_v2 is not None else ""
            _lb_detail = (f"可用 {lb.get('usable_rows', 0)} 条 · holdout n={lb.get('holdout_n')}{_d_txt}"
                          f" · {str(lb.get('reason', ''))[:60]}")
        else:
            _lb_detail = f"可用 {lb.get('usable_rows', 0)} 条 / 门槛 500 · {str(lb.get('reason', ''))[:60]}"
        cards.append('<div class="evo-card"><div class="evo-title">LGBM 影子</div>'
                     f'<div class="evo-verdict" style="color:{_l_color}">{_l_label}</div>'
                     f'<div class="evo-detail">{_lb_detail}</div></div>')

    if not cards:
        return ""
    return f'''
  <div class="section-title">🧬 进化仪表盘<span class="st-note">系统自进化四状态 · 每次结算自动更新 · 判定口径见 docs/UPGRADE2_20260829.md</span></div>
  <div class="evo-grid">{''.join(cards)}</div>'''


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


def _build_daily_brief(
    web_dir: Path,
    target_date: str,
    predictions: list,
    ticket: dict,
    review: dict | None,
    league_report: dict | None,
    parlay_report: dict | None,
    trend_report: dict | None = None,
) -> Path:
    """生成每日简报 markdown（投注决策一目了然）"""
    lines = [
        f"# 竞彩投注简报 {target_date}\n",
        f"> 生成时间：{beijing_now().strftime('%Y-%m-%d %H:%M')}（北京时间）\n",
    ]

    # 投注方案
    bets = ticket.get("stable", []) + ticket.get("value", []) + ticket.get("lottery", [])
    total = ticket.get("total_stake", 0)
    if bets:
        lines.append("## 💰 今日投注方案\n")
        lines.append(f"总投入 **¥{total:.0f}**（资金池 ¥{ticket.get('bankroll', 0):.0f}）\n")
        _hafu_name = {"HH": "胜胜", "HD": "胜平", "HA": "胜负", "DH": "平胜", "DD": "平平", "DA": "平负", "AH": "负胜", "AD": "负平", "AA": "负负"}
        for it in bets:
            mid = it.get("match", "")
            if "#" in mid:
                mid = mid.split("#", 1)[0]
            sel = it.get("sel", "")
            if sel.startswith("hcap_"):
                label = {"home": "主让胜", "draw": "让平", "away": "让负"}.get(sel[5:], sel[5:])
            elif sel.startswith("ttg_"):
                label = f"总进球{sel[4:]}+球" if int(sel[4:]) >= 7 else f"总进球{sel[4:]}球"
            elif sel.startswith("crs_"):
                p = sel[4:].split("_")
                label = f"比分{p[0]}:{p[1]}" if len(p) == 2 else sel
            elif sel.startswith("hafu_"):
                label = "半全场·" + _hafu_name.get(sel[5:], sel[5:])
            else:
                label = {"home": "主胜", "draw": "平局", "away": "客胜"}.get(sel, sel)
            lines.append(f"- {mid} [{label}] @{it.get('odds', 0):.2f} × ¥{it.get('stake', 0):.0f}\n")
    else:
        parlay_plans = list(ticket.get("parlay") or []) + list(ticket.get("score_parlay") or [])
        if parlay_plans:
            exposure = float(ticket.get("total_exposure") or ticket.get("parlay_stake") or 0)
            lines.append("## 💰 今日投注\n")
            lines.append(f"**无单场方案**，仅有 {len(parlay_plans)} 张高风险串关，合计 **¥{exposure:.0f}**。\n")
            for plan in parlay_plans:
                legs = " / ".join(
                    f"{leg.get('home', '')} vs {leg.get('away', '')} "
                    f"{('比分 ' + str(leg.get('score'))) if leg.get('score') else leg.get('sel', '')}"
                    for leg in (plan.get("legs") or [])
                )
                lines.append(
                    f"- {plan.get('type', '串关')}：{legs or '组合待更新'} "
                    f"@{float(plan.get('total_odds') or 0):.2f} × ¥{float(plan.get('stake') or 0):.0f}\n"
                )
        else:
            lines.append("## 💰 今日投注\n")
            lines.append("**空仓不出手**（无正 EV 价值注，避免送钱）\n")

    # 预测清单
    if predictions:
        lines.append("\n## 🔮 今日预测\n")
        lines.append("原始模型 = 独立统计判断；市场 = 赔率去水后的价格观点；最终 = 按实际权重融合后的倾向。\n")
        lines.append("| 场次 | 联赛 | 原始模型 | 市场 | 最终 | 最终概率 | 实际权重(模/市/D) |\n")
        lines.append("|---|---|---|---|---|---|---|\n")
        for p in predictions:
            views = _prediction_provenance(p)
            final_prob = views["final"][1] if views["final"] else 0
            weights = p.get("fusion_effective_weights") or {}
            weight_text = "—"
            if isinstance(weights, dict):
                weight_text = "/".join(
                    f"{float(weights.get(k) or 0):.0%}" for k in ("model", "market", "djyy")
                )
            lines.append(
                f"| {p.get('match_id', '')} | {p.get('competition', '')} "
                f"| {_format_direction_view(views['model'])} | {_format_direction_view(views['market'])} "
                f"| {_format_direction_view(views['final'])} | {final_prob:.0%} | {weight_text} |\n"
            )

    # 联赛状态
    if league_report and league_report.get("leagues"):
        lines.append("\n## 🏆 联赛分层（哪个联赛值得投）\n")
        for row in league_report["leagues"]:
            if row["n"] < 5:
                continue
            icon = {"价值区": "✅", "送钱区": "🚫", "谨慎": "⚠️", "观望": "👀", "回暖解禁": "🔥"}.get(row["verdict"], "·")
            _r5 = f"{row['recent_hit_rate']*100:.0f}%" if row.get("recent_n") else "—"
            _r10 = f"{row['recent10_hit_rate']*100:.0f}%" if row.get("recent10_n") else "—"
            lines.append(f"- {icon} **{row['league']}**：{row['n']}场 命中{row['hit_rate']*100:.0f}% "
                         f"均赔{row['avg_odds']:.2f} ROI {row['roi']*100:+.1f}% "
                         f"（近5 {_r5} / 近10 {_r10}）\n")

    # 串关结论
    if parlay_report and parlay_report.get("verdict"):
        lines.append(f"\n## 🎰 串关评估\n{parlay_report['verdict']}\n")

    # 上期战绩（上一日期目录的 review）
    if review and review.get("n_matches"):
        lines.append(
            f"\n## 📊 上期战绩\n"
            f"命中 {review.get('hits', 0)}/{review.get('n_matches', 0)} "
            f"({review.get('hit_rate', 0)*100:.0f}%)，盈亏 **¥{review.get('total_pnl', 0):+.0f}**\n"
        )

    # 准确率趋势（诚实回答"是否每天在提升"）
    if trend_report and trend_report.get("daily"):
        v = trend_report.get("verdict", "")
        r7 = trend_report.get("rolling7") or {}
        p7 = trend_report.get("prev7") or {}
        lines.append("\n## 📈 准确率趋势\n")
        if v:
            lines.append(f"**判定：{v}**\n")
        if r7 and p7:
            lines.append(
                f"- 最近7天：{r7.get('n', 0)}场 命中率 {r7.get('hit_rate', 0)*100:.0f}% "
                f"Brier {r7.get('brier_final', 0):.2f}\n"
                f"- 前7天：{p7.get('n', 0)}场 命中率 {p7.get('hit_rate', 0)*100:.0f}% "
                f"Brier {p7.get('brier_final', 0):.2f}\n"
            )
        lines.append("\n| 日期 | 场次 | 命中 | 命中率 | 累计命中率 |\n")
        lines.append("|---|---|---|---|---|\n")
        for d in trend_report["daily"][-7:]:
            lines.append(f"| {d['date']} | {d['n']} | {d['hits']}/{d['n']} | {d['hit_rate']*100:.0f}% | {d['cum_hit_rate']*100:.0f}% |\n")

    # 比分命中率（2026-08-05 闭环：主推前三不靠谱→前5，账本可核查）
    _slp = web_dir.parent / "data" / "state" / "review_ledger.jsonl"
    if _slp.exists():
        _recs = []
        for _line in _slp.read_text(encoding="utf-8").strip().split("\n"):
            if _line.strip():
                try:
                    _recs.append(json.loads(_line))
                except Exception:
                    continue
        if _recs:
            _n = len(_recs)
            lines.append("\n## 🎯 比分命中率（历史可核查）\n")
            lines.append("| 推荐档 | 命中 | 命中率 |\n")
            lines.append("|---|---|---|\n")
            for _label, _cond in [
                ("主推 top1", lambda r: r.get("score_rank") == 1),
                ("主推 top3", lambda r: r.get("score_top3_hit")),
                ("主推 top5（当前）", lambda r: r.get("score_top5_hit")),
                ("候选 top8", lambda r: r.get("score_top8_hit")),
            ]:
                _h = sum(1 for r in _recs if _cond(r))
                _hl = "**" if _label.startswith("主推 top5") else ""
                lines.append(f"| {_label} | {_h}/{_n} | {_hl}{_h/_n*100:.0f}%{_hl} |\n")

            # 盘口信号命中率（8/5 起累积验证，数据说话盘口信号是否有效）
            _ms_items = [r for r in _recs if r.get("market_signal_hit") is not None]
            if _ms_items:
                _msh = sum(1 for r in _ms_items if r.get("market_signal_hit"))
                lines.append("\n## 📊 盘口信号命中率（累积验证中）\n")
                lines.append(f"- 欧赔压缩方向信号: **{_msh}/{len(_ms_items)} ({_msh/len(_ms_items)*100:.0f}%)**")
                lines.append(f"- 模型方向基线: {sum(1 for r in _recs if r.get('hit'))}/{_n} ({sum(1 for r in _recs if r.get('hit'))/_n*100:.0f}%)\n")

    out = web_dir / f"daily-brief-{target_date}.md"
    out.write_text("".join(lines), encoding="utf-8")
    return out


if __name__ == "__main__":
    build_site()
