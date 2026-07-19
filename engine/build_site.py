"""静态报告生成器 - 生成 GitHub Pages 仪表盘"""
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent


def build_site():
    """生成静态 HTML 报告"""
    web_dir = ROOT / "web"
    web_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    daily_dir = ROOT / "data" / "daily" / today

    # 加载预测数据
    predictions = []
    pred_path = daily_dir / "predictions.json"
    if pred_path.exists():
        predictions = json.loads(pred_path.read_text())

    # 加载决策包
    bundle = {}
    bundle_path = daily_dir / f"decision_bundle_{today}.json"
    if bundle_path.exists():
        bundle = json.loads(bundle_path.read_text())

    # 加载健康状态
    health = {"healthy": True, "issues": []}
    health_path = web_dir / "health-status.json"
    if health_path.exists():
        health = json.loads(health_path.read_text())

    html = _render_html(today, predictions, bundle, health)
    (web_dir / "index.html").write_text(html, encoding="utf-8")

    # 写入报告状态
    status = {
        "date": today,
        "generated_at": date.today().isoformat(),
        "prediction_count": len(predictions),
        "bundle_hash": bundle.get("bundle_sha256", "")[:16],
        "healthy": health.get("healthy", True),
    }
    (web_dir / "report-status.json").write_text(json.dumps(status, indent=2))

    print(f"[build_site] 报告已生成: web/index.html ({len(predictions)} 场比赛)")


def _render_html(today: str, predictions: list, bundle: dict, health: dict) -> str:
    """渲染 HTML"""
    # 比赛卡片
    match_cards = ""
    for p in predictions:
        hp = p.get("home_win_prob", 0) * 100
        dp = p.get("draw_prob", 0) * 100
        ap = p.get("away_win_prob", 0) * 100
        conf = p.get("confidence", 0)
        conf_class = "high" if conf > 0.6 else "medium" if conf > 0.45 else "low"

        match_cards += f"""
        <div class="card">
            <div class="match-header">
                <span class="league">{p.get('competition', '')}</span>
                <span class="conf {conf_class}">{conf:.0%}</span>
            </div>
            <div class="teams">
                <span class="home">{p.get('home_team', '')}</span>
                <span class="vs">vs</span>
                <span class="away">{p.get('away_team', '')}</span>
            </div>
            <div class="prob-bar">
                <div class="seg home" style="width:{hp:.1f}%">主 {hp:.0f}%</div>
                <div class="seg draw" style="width:{dp:.1f}%">平 {dp:.0f}%</div>
                <div class="seg away" style="width:{ap:.1f}%">客 {ap:.0f}%</div>
            </div>
            <div class="meta">
                xG: {p.get('home_xg', 0):.2f} - {p.get('away_xg', 0):.2f}
                {'| 赔率: ' + str(p.get('home_odds', '')) + '/' + str(p.get('draw_odds', '')) + '/' + str(p.get('away_odds', '')) if p.get('home_odds') else ''}
            </div>
        </div>"""

    # 健康状态
    health_badge = "✓ 正常" if health.get("healthy") else "⚠ 异常"
    health_class = "ok" if health.get("healthy") else "warn"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>竞彩分析 - {today}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1419; color: #e7e9ea; padding: 20px; }}
.container {{ max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 1.5rem; margin-bottom: 8px; }}
.subtitle {{ color: #71767b; font-size: 0.9rem; margin-bottom: 24px; }}
.health {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 0.8rem; margin-bottom: 16px; }}
.health.ok {{ background: #0d3320; color: #4ade80; }}
.health.warn {{ background: #3d2000; color: #fbbf24; }}
.card {{ background: #1a1f26; border-radius: 12px; padding: 16px; margin-bottom: 12px; border: 1px solid #2f3336; }}
.match-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
.league {{ font-size: 0.75rem; color: #71767b; background: #2f3336; padding: 2px 8px; border-radius: 4px; }}
.conf {{ font-size: 0.75rem; font-weight: 600; }}
.conf.high {{ color: #4ade80; }}
.conf.medium {{ color: #fbbf24; }}
.conf.low {{ color: #71767b; }}
.teams {{ display: flex; justify-content: center; align-items: center; gap: 12px; font-size: 1.1rem; font-weight: 600; margin: 8px 0; }}
.vs {{ color: #71767b; font-size: 0.8rem; }}
.prob-bar {{ display: flex; height: 28px; border-radius: 6px; overflow: hidden; margin: 8px 0; font-size: 0.7rem; }}
.seg {{ display: flex; align-items: center; justify-content: center; min-width: 30px; }}
.seg.home {{ background: #1d4ed8; }}
.seg.draw {{ background: #4b5563; }}
.seg.away {{ background: #dc2626; }}
.meta {{ font-size: 0.75rem; color: #71767b; }}
.footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #2f3336; font-size: 0.75rem; color: #71767b; }}
.empty {{ text-align: center; padding: 60px 20px; color: #71767b; }}
</style>
</head>
<body>
<div class="container">
    <h1>竞彩足球概率分析</h1>
    <div class="subtitle">{today} | {len(predictions)} 场比赛 | Dixon-Coles + Monte Carlo 集成模型</div>
    <span class="health {health_class}">{health_badge}</span>

    {''.join(match_cards) if match_cards else '<div class="empty">暂无今日预测数据<br>等待每日流水线运行...</div>'}

    <div class="footer">
        <p>模型: Dixon-Coles (60%) + Monte Carlo 50K (40%) | 市场混合: 28%</p>
        <p>决策包: {bundle.get('bundle_sha256', 'N/A')[:32]}...</p>
        <p>仅供研究学习，不构成投注建议。</p>
    </div>
</div>
</body>
</html>"""


if __name__ == "__main__":
    build_site()
