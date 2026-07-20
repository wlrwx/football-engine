from __future__ import annotations
"""不可变决策包 - 密码学绑定每一步输入输出"""
import hashlib
import json
import os
import time
from pathlib import Path


class DecisionBundle:
    """
    不可变决策包。
    将一次预测决策的所有输入（数据、配置、模型代码哈希）和输出（预测、计划）
    绑定为一个 SHA-256 哈希链，任何篡改都会破坏验证。
    借鉴 sporttery-prediction 的 decision_bundle 设计。
    """

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        date_str: str,
        import_manifest: dict,
        predictions: list[dict],
        betting_plan: dict,
        config_prediction: dict,
        config_strategy: dict,
        model_code_hashes: dict[str, str] | None = None,
    ) -> dict:
        """
        创建决策包。同日多次运行自动递增版本号（v1, v2, ...）。
        每个版本一旦写入不可篡改，但允许同一天有多个版本
        （如 11:00 初判 = v1, 17:00 临场更新 = v2）。
        """
        # 确定版本号
        version = self._next_version(date_str)

        bundle = {
            "version": version,
            "date": date_str,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "inputs": {
                "import_manifest": import_manifest,
                "config_prediction": self._canonical_hash(config_prediction),
                "config_strategy": self._canonical_hash(config_strategy),
                "model_code_hashes": model_code_hashes or {},
            },
            "outputs": {
                "predictions_hash": self._hash_data(predictions),
                "predictions_count": len(predictions),
                "betting_plan_hash": self._hash_data(betting_plan),
                "total_stake": betting_plan.get("total_stake", 0),
            },
        }

        # 计算整体哈希
        bundle_content = json.dumps(bundle, sort_keys=True, ensure_ascii=False)
        bundle["bundle_sha256"] = hashlib.sha256(bundle_content.encode()).hexdigest()

        # 写入版本文件（不可变）
        bundle_path = self.output_dir / f"decision_bundle_{date_str}_v{version}.json"
        if bundle_path.exists():
            # 同版本同内容 → 幂等返回
            existing = json.loads(bundle_path.read_text())
            if existing.get("bundle_sha256") == bundle["bundle_sha256"]:
                return existing
            # 同版本不同内容 → 递增版本
            version += 1
            bundle["version"] = version
            bundle_content = json.dumps(bundle, sort_keys=True, ensure_ascii=False)
            bundle["bundle_sha256"] = hashlib.sha256(bundle_content.encode()).hexdigest()
            bundle_path = self.output_dir / f"decision_bundle_{date_str}_v{version}.json"

        # 原子写入
        tmp_path = bundle_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
        os.replace(str(tmp_path), str(bundle_path))

        # 更新 latest 指针
        latest_path = self.output_dir / f"decision_bundle_{date_str}.json"
        latest_tmp = latest_path.with_suffix(".tmp")
        latest_tmp.write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
        os.replace(str(latest_tmp), str(latest_path))

        return bundle

    def _next_version(self, date_str: str) -> int:
        """查找当前日期的下一个版本号"""
        version = 1
        while (self.output_dir / f"decision_bundle_{date_str}_v{version}.json").exists():
            version += 1
        return version

    def verify(self, date_str: str) -> tuple[bool, str]:
        """验证决策包完整性"""
        bundle_path = self.output_dir / f"decision_bundle_{date_str}.json"
        if not bundle_path.exists():
            return False, "决策包不存在"

        bundle = json.loads(bundle_path.read_text())
        stored_hash = bundle.pop("bundle_sha256", "")
        content = json.dumps(bundle, sort_keys=True, ensure_ascii=False)
        computed_hash = hashlib.sha256(content.encode()).hexdigest()

        if computed_hash != stored_hash:
            return False, f"哈希不匹配: 存储={stored_hash[:16]}... 计算={computed_hash[:16]}..."

        return True, "验证通过"

    @staticmethod
    def _canonical_hash(obj) -> str:
        """规范化 JSON 哈希"""
        content = json.dumps(obj, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def _hash_data(data) -> str:
        """数据哈希"""
        content = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def hash_file(path: Path) -> str:
        """文件内容哈希"""
        if not path.exists():
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()
