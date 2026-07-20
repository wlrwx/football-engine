from __future__ import annotations
"""计划锁定 - 防止已锁定的决策被重写"""
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path


class PlanLock:
    """
    计划锁定机制。
    一旦锁定，任何工作流都不能重新生成该日期的计划。
    使用 OS 级文件锁防止并发写入。
    """

    def __init__(self, lock_dir: Path):
        self.lock_dir = lock_dir
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def lock(
        self,
        date_str: str,
        plan_hash: str,
        bundle_hash: str,
        odds_snapshot_hash: str = "",
    ) -> dict:
        """锁定计划"""
        lock_path = self._lock_path(date_str)

        if lock_path.exists():
            existing = json.loads(lock_path.read_text())
            if existing.get("plan_hash") == plan_hash:
                return existing  # 已锁定且一致
            raise ValueError(f"计划已锁定且内容不同，拒绝覆盖: {date_str}")

        lock_data = {
            "date": date_str,
            "locked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "plan_hash": plan_hash,
            "bundle_hash": bundle_hash,
            "odds_snapshot_hash": odds_snapshot_hash,
            "pid": os.getpid(),
        }

        # 原子写入
        tmp_path = lock_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(lock_data, indent=2))
        os.replace(str(tmp_path), str(lock_path))

        return lock_data

    def is_locked(self, date_str: str) -> bool:
        """检查是否已锁定"""
        return self._lock_path(date_str).exists()

    def read_lock(self, date_str: str) -> dict | None:
        """读取锁信息"""
        lock_path = self._lock_path(date_str)
        if not lock_path.exists():
            return None
        return json.loads(lock_path.read_text())

    def verify_lock(self, date_str: str, plan_hash: str, bundle_hash: str) -> tuple[bool, str]:
        """验证锁是否有效"""
        lock = self.read_lock(date_str)
        if not lock:
            return False, "未锁定"
        if lock.get("plan_hash") != plan_hash:
            return False, "计划哈希不匹配"
        if lock.get("bundle_hash") != bundle_hash:
            return False, "决策包哈希不匹配"
        return True, "锁有效"

    def acquire_file_lock(self, date_str: str, timeout: float = 5.0):
        """获取 OS 级文件锁（用于并发保护）"""
        lock_file_path = self.lock_dir / f".flock_{date_str}"
        lock_file_path.touch(exist_ok=True)
        self._lock_fd = open(lock_file_path, "w")

        start = time.time()
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (IOError, OSError):
                if time.time() - start > timeout:
                    self._lock_fd.close()
                    raise TimeoutError(f"获取文件锁超时: {date_str}")
                time.sleep(0.1)

    def release_file_lock(self):
        """释放文件锁"""
        if hasattr(self, "_lock_fd") and self._lock_fd:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()
            self._lock_fd = None

    def _lock_path(self, date_str: str) -> Path:
        return self.lock_dir / f"plan_lock_{date_str}.json"
