"""回归测试：MC 种子跨进程确定性 + H2H 队名别名方向。

锁定 docs/REVIEW_20261023.md 的 P0#4 与 P1#17：
- build_seed 原用内置 hash()，受 PYTHONHASHSEED 随机化影响，"固定 seed 可复现"
  只在单进程内成立；
- head_to_head_factor 原用精确字符串比较，别名不一致时净胜球算反。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from engine.prediction.enhanced import build_seed, head_to_head_factor

ROOT = Path(__file__).resolve().parent.parent


def test_build_seed_same_input_same_output() -> None:
    a = build_seed("m-001", 1.5, 1.2, 1000)
    b = build_seed("m-001", 1.5, 1.2, 1000)
    assert a == b
    # 落在合法种子区间
    assert 0 <= a < 2**63


def test_build_seed_changes_with_input() -> None:
    base = build_seed("m-001", 1.5, 1.2, 1000)
    assert build_seed("m-002", 1.5, 1.2, 1000) != base
    assert build_seed("m-001", 1.6, 1.2, 1000) != base
    assert build_seed("m-001", 1.5, 1.2, 5000) != base


def test_build_seed_stable_across_processes() -> None:
    """核心回归：不同 PYTHONHASHSEED 的独立进程必须得到同一个种子。

    内置 hash(str) 在这种条件下会不同（除非 PYTHONHASHSEED 固定），
    这正是原实现的问题。
    """
    code = (
        "from engine.prediction.enhanced import build_seed;"
        "print(build_seed('2026-10-23_周一001', 1.7345, 1.1234, 50000))"
    )
    outputs = []
    for hashseed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=hashseed)
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60, env=env,
        )
        assert r.returncode == 0, f"subprocess 失败: {r.stderr}"
        outputs.append(r.stdout.strip())
    assert len(set(outputs)) == 1, f"跨进程种子不一致: {outputs}"
    # 与当前进程内计算结果一致
    assert int(outputs[0]) == build_seed("2026-10-23_周一001", 1.7345, 1.1234, 50000)


def test_h2h_alias_orientation() -> None:
    """主队 3:0 大胜，但历史里记的是全名、调用方传的是简称。

    修复前：字符串不等 → 走 else 分支 → 净胜球 -3 → 因子 <1（方向反了）。
    修复后：别名归一 → 净胜球 +3 → 因子 1.12 被夹到 1.10。
    """
    hist = [
        {
            "home_team": "皇家马德里",
            "away_team": "巴塞罗那",
            "home_score": 3,
            "away_score": 0,
            "days_ago": 10,
            "match_type": "OFFICIAL",
        }
    ]
    factor = head_to_head_factor("皇马", "巴萨", hist)
    assert factor > 1.0, f"别名场景下方向算反了: factor={factor}"
    assert factor == 1.10  # 1.0 + 3*0.04 = 1.12 → clamp 到 1.10


def test_h2h_away_side_history() -> None:
    """历史里主队是本次的客队时，净胜球要换向。"""
    hist = [
        {
            "home_team": "巴塞罗那",
            "away_team": "皇家马德里",
            "home_score": 0,
            "away_score": 2,
            "days_ago": 30,
            "match_type": "OFFICIAL",
        }
    ]
    factor = head_to_head_factor("皇马", "巴萨", hist)
    assert factor > 1.0, f"客队视角的历史应换算为 +2 净胜球: factor={factor}"


def test_h2h_unknown_team_is_skipped() -> None:
    """两队都认不出来的历史不能猜方向：跳过 → 无有效样本 → 1.0。"""
    hist = [
        {
            "home_team": "某不存在球队",
            "away_team": "另一个不存在球队",
            "home_score": 5,
            "away_score": 0,
            "days_ago": 5,
            "match_type": "OFFICIAL",
        }
    ]
    assert head_to_head_factor("皇马", "巴萨", hist) == 1.0


def test_h2h_empty_history() -> None:
    assert head_to_head_factor("皇马", "巴萨", []) == 1.0
