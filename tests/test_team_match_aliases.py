"""sources 层队名别名回归（2026-10-22）。

与 tests/test_team_aliases.py 的分工：
  - engine/team_aliases.py 是 team_ratings 的键空间，合并=合并 Elo，动它要单独评估；
  - engine/sources/team_match.py 只服务于跨源富化匹配，可以更宽松。

实测缺口：竞彩用「萨迪纳摩」，DJYY 快照用全称「萨格勒布迪纳摩」。快照里
200/200 场都有 djyy_model_prob，唯独这一场因为译名没进 fusion 第三通道。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.sources.djyy_ssr import DJYYSSRSource  # noqa: E402
from engine.sources.team_match import match_pair, match_team  # noqa: E402
from engine.team_aliases import normalize_team  # noqa: E402


def test_dinamo_alias_matches_both_directions():
    assert match_team("萨迪纳摩", "萨格勒布迪纳摩")
    assert match_team("萨格勒布迪纳摩", "萨迪纳摩")
    assert match_pair("萨迪纳摩", "奈梅亨", "萨格勒布迪纳摩", "奈梅亨")


def test_dinamo_alias_does_not_touch_ratings_key_space():
    """别名只在 sources 层：team_aliases 若也合并，会改写 team_ratings 的键。"""
    assert normalize_team("萨迪纳摩") == "萨迪纳摩"
    assert normalize_team("萨格勒布迪纳摩") == "萨格勒布迪纳摩"


def test_dinamo_alias_does_not_swallow_other_zagreb_club():
    assert not match_team("萨迪纳摩", "萨格勒布火车头")
    assert not match_team("萨迪纳摩", "萨格勒布")


def test_snapshot_enrichment_hits_dinamo_match():
    """端到端：这条别名存在的意义就是让这场拿得到快照概率。"""
    snap = ROOT / "data" / "djyy_matches.json"
    src = DJYYSSRSource(snap)
    out = src.enrich_prediction("萨迪纳摩", "奈梅亨")
    prob = out.get("djyy_model_prob")
    assert prob and prob.get("home"), f"萨迪纳摩 vs 奈梅亨 仍未命中快照: {out}"
    assert out.get("home_odds_djyy"), "赔率字段也该一起回来"
