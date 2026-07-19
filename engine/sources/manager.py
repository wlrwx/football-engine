"""数据源管理器 - fallback 链调度"""
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from .base import DataSource, Fixture, MatchResult, ImportManifest
from .sporttery import SportterySource
from .espn import EspnSource


class SourceManager:
    """管理多个数据源，按优先级 fallback"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.sources: list[DataSource] = sorted(
            [SportterySource(), EspnSource()],
            key=lambda s: s.priority,
        )

    def fetch_fixtures(self, target_date: date) -> tuple[list[Fixture], ImportManifest]:
        """按优先级尝试获取赛程，返回数据和导入清单"""
        for source in self.sources:
            try:
                fixtures = source.fetch_fixtures(target_date)
                if fixtures:
                    manifest = self._create_manifest(target_date, source, fixtures)
                    return fixtures, manifest
            except Exception as e:
                print(f"[WARN] 数据源 {source.name} 获取失败: {e}")
                continue

        raise RuntimeError(f"所有数据源均无法获取 {target_date} 的赛程数据")

    def fetch_results(self, target_date: date) -> list[MatchResult]:
        """按优先级获取比赛结果"""
        for source in self.sources:
            try:
                results = source.fetch_results(target_date)
                if results:
                    return results
            except Exception:
                continue
        return []

    def health_check(self) -> dict[str, bool]:
        """检查所有数据源健康状态"""
        status = {}
        for source in self.sources:
            try:
                status[source.name] = source.health_check()
            except Exception:
                status[source.name] = False
        return status

    def _create_manifest(
        self, target_date: date, source: DataSource, fixtures: list[Fixture]
    ) -> ImportManifest:
        """创建不可变导入清单"""
        content = json.dumps(
            [{"match_id": f.match_id, "home": f.home_team, "away": f.away_team}
             for f in fixtures],
            ensure_ascii=False,
            sort_keys=True,
        )
        sha = hashlib.sha256(content.encode()).hexdigest()

        manifest = ImportManifest(
            import_date=target_date.isoformat(),
            source=source.name,
            fixture_count=len(fixtures),
            sha256=sha,
            timestamp=datetime.now().isoformat(),
            fallback_used=source.priority > 1,
        )

        # 持久化清单
        manifest_dir = self.data_dir / "daily" / target_date.isoformat()
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "import_manifest.json"
        if not manifest_path.exists():
            manifest_path.write_text(
                json.dumps(manifest.__dict__, ensure_ascii=False, indent=2)
            )

        return manifest
