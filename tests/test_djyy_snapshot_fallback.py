"""DJYY 快照补位 fusion 第三通道的接线测试（2026-10-22）。

实时 DJYY 在 CI 侧被上游整体挑战（403），但仓库快照由本机 duty 抓取并提交。
快照里的 `djyy_model_prob` 必须能在实时源缺失时补进融合，否则 `djyy_weight`
继续被 fusion 重新归一化掉——第三通道名义存在、实际为零。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.sources.djyy_ssr import DJYYSSRSource  # noqa: E402


def test_snapshot_extract_exposes_model_probs():
    src = DJYYSSRSource(Path("/nonexistent"))
    out = src._extract_enrichment({
        "djyy_model_prob": {"home": 0.51, "draw": 0.27, "away": 0.22},
    })
    assert out["djyy_model_prob"] == {"home": 0.51, "draw": 0.27, "away": 0.22}


def test_snapshot_extract_rejects_empty_probs():
    src = DJYYSSRSource(Path("/nonexistent"))
    assert "djyy_model_prob" not in src._extract_enrichment(
        {"djyy_model_prob": {"home": None, "draw": None, "away": None}})


def test_main_uses_snapshot_before_fusion():
    """补位必须发生在 fuse_probabilities 之前；晚一步就还是零贡献。"""
    src = (ROOT / "engine" / "main.py").read_text(encoding="utf-8")
    enrich_at = src.index("_djyy_ssr = djyy_ssr.enrich_prediction")
    fallback_at = src.index('if not djyy_probs and _djyy_ssr.get("djyy_model_prob")')
    fusion_at = src.index("_fusion_out = fuse_probabilities")
    assert enrich_at < fallback_at < fusion_at, \
        "快照补位没有排在融合之前，djyy_probs 仍是 None"
    assert "djyy_ssr_probs += 1" in src, "补位次数没被计数，日志无法区分实时源与快照"


def test_snapshot_age_is_visible():
    """快照会过期：不暴露 extracted_at，日志里就看不出「补的是三天前的数」。"""
    import json
    import tempfile
    from datetime import datetime, timedelta, timezone

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "djyy_matches.json"
        old = datetime.now(timezone.utc) - timedelta(hours=30)
        p.write_text(json.dumps({"extracted_at": old.isoformat(), "matches": []}),
                     encoding="utf-8")
        src = DJYYSSRSource(p)
        assert src.extracted_at == old.isoformat()
        assert 29 < src.age_hours() < 31

        p.write_text(json.dumps({"matches": []}), encoding="utf-8")
        assert DJYYSSRSource(p).age_hours() is None


def test_main_warns_and_reports_snapshot_status():
    """过期要吼；补位事实要写进 djyy_status，否则 health-check 继续误报。"""
    src = (ROOT / "engine" / "main.py").read_text(encoding="utf-8")
    assert "SNAPSHOT_MAX_AGE_H" in src and "快照已" in src, "过期快照没有告警"
    assert '"snapshot_probs"' in src and '"snapshot_age_h"' in src, \
        "快照补位没有回写 djyy_status，健康检查只能看到实时源 403"


def test_health_check_does_not_alarm_when_snapshot_covers():
    """实时 403 + 快照补位 >0 → warning；只有既被拦又没补位才 error。"""
    wf = (ROOT / ".github" / "workflows" / "health-check.yml").read_text(encoding="utf-8")
    assert '.snapshot_probs // 0' in wf
    assert 'SNAP' in wf and '"$SNAP" -gt 0' in wf
    assert "快照补位 ${SNAP} 场生效" in wf


def test_selfcheck_counts_effective_channel_not_live_flag():
    """自检口径：有 djyy_model_prob（实时或快照）就算存活。"""
    from engine.selfcheck import _djyy_alive

    assert _djyy_alive({"djyy_enriched": False,
                        "djyy_model_prob": {"home": 0.5, "draw": 0.3, "away": 0.2}})
    assert _djyy_alive({"djyy_enriched": True})
    assert not _djyy_alive({"djyy_enriched": False, "djyy_model_prob": None})
    assert not _djyy_alive({})
