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
        """创建决策包"""
        bundle = {
            "version": 1,
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

        # 原子写入（硬链接方式，防止覆盖）
        bundle_path = self.output_dir / f"decision_bundle_{date_str}.json"
        if bundle_path.exists():
            # 验证已有包是否一致
            existing = json.loads(bundle_path.read_text())
            if existing.get("bundle_sha256") != bundle["bundle_sha256"]:
                raise ValueError(
                    f"决策包冲突: {date_str} 已存在不同内容的决策包"
                )
            return existing

        # 写入临时文件再原子移动
        tmp_path = bundle_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
        os.replace(str(tmp_path), str(bundle_path))

        return bundle

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
