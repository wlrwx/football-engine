"""数据源抽象基类 - 所有数据源必须实现此接口"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class Fixture:
    """一场比赛"""
    match_id: str
    competition: str
    home_team: str
    away_team: str
    kickoff: str  # ISO datetime
    home_odds: Optional[float] = None
    draw_odds: Optional[float] = None
    away_odds: Optional[float] = None
    handicap: Optional[float] = None
    handicap_home_odds: Optional[float] = None
    handicap_draw_odds: Optional[float] = None
    handicap_away_odds: Optional[float] = None
    total_goals_line: Optional[float] = None
    source: str = ""


@dataclass
class MatchResult:
    """比赛结果"""
    match_id: str
    home_score: int
    away_score: int
    home_team: str
    away_team: str
    competition: str
    match_date: str


@dataclass
class OddsSnapshot:
    """赔率快照"""
    match_id: str
    timestamp: str
    home_odds: float
    draw_odds: float
    away_odds: float
    source: str


@dataclass
class ImportManifest:
    """导入清单 - 记录数据来源和完整性"""
    import_date: str
    source: str
    fixture_count: int
    sha256: str
    timestamp: str
    fallback_used: bool = False


class DataSource(ABC):
    """数据源抽象接口"""

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源名称"""
        ...

    @property
    @abstractmethod
    def priority(self) -> int:
        """优先级，数字越小越优先"""
        ...

    @abstractmethod
    def fetch_fixtures(self, target_date: date) -> list[Fixture]:
        """获取指定日期的赛程和赔率"""
        ...

    @abstractmethod
    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """获取指定日期的比赛结果"""
        ...

    def fetch_odds_snapshot(self, target_date: date) -> list[OddsSnapshot]:
        """获取赔率快照（可选实现）"""
        return []

    def health_check(self) -> bool:
        """健康检查（可选实现）"""
        return True
