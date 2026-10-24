"""回归测试：别名 loose 规则收紧（P2#16）+ Wilson 信任度用真实命中率（P2#22）。"""
from __future__ import annotations

import json

from engine.learning.wilson_trust import load_trust_stats
from engine.team_aliases import loose_normalize, normalize_team


def test_loose_does_not_merge_distinct_clubs() -> None:
    """子串删字已停用：不同球队不得被归一成同键。"""
    assert loose_normalize("国际米兰") != loose_normalize("AC米兰")
    assert loose_normalize("米内罗竞技") == "米内罗竞技", (
        "竞技 后缀不再被无条件下削（原来会变成'米内罗'，与其它同名队混淆）"
    )


def test_loose_keeps_safe_exact_mapping() -> None:
    """白名单式精确映射保留（迈阿密国际 → 迈阿密）。"""
    assert loose_normalize("迈阿密国际") == "迈阿密"


def test_normalize_team_still_handles_known_aliases() -> None:
    assert normalize_team("皇马") == "皇家马德里"
    assert normalize_team("LASK林茨") == "林茨"


def test_load_trust_stats_counts_by_tier(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    rows = [
        {"confidence_tier": "high", "hit": True},
        {"confidence_tier": "high", "hit": True},
        {"confidence_tier": "high", "hit": False},
        {"confidence_tier": "mid", "hit": False},
        {"confidence_tier": "", "hit": True},        # 无档位 → 忽略
        {"confidence_tier": "low"},                  # 缺 hit → 计为未命中
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stats = load_trust_stats(p)
    assert stats["high"] == (2, 3)
    assert stats["mid"] == (0, 1)
    assert stats["low"] == (0, 1)
    assert "" not in stats


def test_load_trust_stats_missing_file(tmp_path) -> None:
    assert load_trust_stats(tmp_path / "nope.jsonl") == {}


def test_load_trust_stats_tolerates_bad_lines(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    p.write_text('{not json\n{"confidence_tier": "high", "hit": true}\n', encoding="utf-8")
    assert load_trust_stats(p) == {"high": (1, 1)}
