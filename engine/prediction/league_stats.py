from __future__ import annotations
"""联赛先验：场均总进球 / 主胜率。

数据源 `data/league_matrix.json`（DJYY league-matrix 落盘），竞彩联赛名先经
`engine.learning.freshness.LEAGUE_NAME_MAP` 归一。这两个值一直是 LGBM 的
`league_avg_goals` / `league_home_win_rate` 特征，但生产调用点从未传过，
特征恒为 2.5 / 0.45。缺失联赛回落同一组默认值，保证老样本不被强行改写。
"""
import json
from functools import lru_cache
from pathlib import Path

from engine.learning.freshness import LEAGUE_NAME_MAP
from engine.pipeline.helpers import _canon_league

MATRIX_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "league_matrix.json"
DEFAULT_AVG_GOALS = 2.5
DEFAULT_HOME_WIN_RATE = 0.45


@lru_cache(maxsize=4)
def _matrix(path_str: str) -> dict[str, tuple[float, float]]:
    try:
        raw = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for lg in raw.get("leagues") or []:
        goals, home_win = lg.get("avg_goals"), lg.get("home_win_pct")
        if goals is None:
            continue
        goals = float(goals)
        if home_win is None:
            home_win = DEFAULT_HOME_WIN_RATE * 100.0
        else:
            home_win = float(home_win)
        if not 0.5 <= goals <= 8.0 or not 0.0 <= home_win <= 100.0:
            continue
        for key in (lg.get("name_zh"), lg.get("short_zh")):
            if key:
                out[key] = (goals, home_win / 100.0)
    return out


def league_stats(competition: str | None,
                 path: Path | None = None) -> tuple[float, float]:
    """(场均总进球, 主胜率)；没有该联赛时返回 build_features 的默认值。"""
    tbl = _matrix(str(path or MATRIX_PATH))
    raw_comp = competition or ""
    canon_comp = _canon_league(raw_comp)
    for cand in (
        LEAGUE_NAME_MAP.get(raw_comp, ""),
        LEAGUE_NAME_MAP.get(canon_comp, ""),
        canon_comp,
        raw_comp,
    ):
        if cand and cand in tbl:
            return tbl[cand]
    return DEFAULT_AVG_GOALS, DEFAULT_HOME_WIN_RATE
