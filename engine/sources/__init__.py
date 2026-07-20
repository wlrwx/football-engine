from __future__ import annotations
"""数据源插件层

已注册数据源（按优先级）:
  1. sporttery  - 竞彩官方（核心权威，国内IP）
  2. sina       - 新浪竞彩（海外IP可用，数据与体彩一致）
  3. wancai500  - 500万机构赔率（Bet365/平博）
  4. djyy       - DJYY足球数据（Cloudflare托管，海外可用，含模型概率+xG）
  9. espn       - ESPN（最终兜底）
"""
from .base import DataSource, Fixture, MatchResult, OddsSnapshot, ImportManifest
from .sporttery import SportterySource
from .sina import SinaSource
from .wancai500 import Wancai500Source
from .djyy import DJYYSource
from .manager import SourceManager

__all__ = [
    "DataSource", "Fixture", "MatchResult", "OddsSnapshot", "ImportManifest",
    "SportterySource", "SinaSource", "Wancai500Source", "DJYYSource",
    "SourceManager",
]
