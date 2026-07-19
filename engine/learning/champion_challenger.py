"""Champion/Challenger 模型自动晋升机制"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ModelMetrics:
    """模型评估指标"""
    brier_score: float = 1.0
    log_loss: float = 1.0
    roi: float = 0.0
    clv: float = 0.0  # closing line value
    sample_count: int = 0
    bet_count: int = 0
    max_drawdown: float = 0.0
    shadow_days: int = 0


@dataclass
class ModelSlot:
    """模型槽位"""
    model_name: str
    metrics: ModelMetrics = field(default_factory=ModelMetrics)
    promoted_at: str = ""
    config_snapshot: dict = field(default_factory=dict)


class ChampionChallenger:
    """
    Champion/Challenger 自动晋升。
    新模型必须在影子评估中全面超越冠军才能上位。
    借鉴 sporttery-prediction 的严格晋升标准。
    """

    # 晋升条件
    MIN_SHADOW_DAYS = 28
    MIN_SAMPLES = 200
    MIN_BETS = 100
    MIN_BRIER_IMPROVEMENT = 0.02  # 2%
    MIN_LOGLOSS_IMPROVEMENT = 0.02

    def __init__(self, registry_path: Path):
        self.registry_path = registry_path
        self.registry = self._load()

    def _load(self) -> dict:
        if self.registry_path.exists():
            return json.loads(self.registry_path.read_text())
        return {
            "champion": None,
            "challenger": None,
            "previous_champion": None,
            "history": [],
        }

    def save(self):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(self.registry, indent=2))

    def register_challenger(self, model_name: str, config: dict):
        """注册挑战者"""
        self.registry["challenger"] = {
            "model_name": model_name,
            "metrics": {},
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "config_snapshot": config,
        }
        self.save()

    def update_metrics(self, slot: str, metrics: ModelMetrics):
        """更新指定槽位的指标"""
        if self.registry.get(slot):
            self.registry[slot]["metrics"] = {
                "brier_score": metrics.brier_score,
                "log_loss": metrics.log_loss,
                "roi": metrics.roi,
                "clv": metrics.clv,
                "sample_count": metrics.sample_count,
                "bet_count": metrics.bet_count,
                "max_drawdown": metrics.max_drawdown,
                "shadow_days": metrics.shadow_days,
            }
            self.save()

    def evaluate_promotion(self) -> tuple[bool, str]:
        """评估是否晋升挑战者"""
        champion = self.registry.get("champion")
        challenger = self.registry.get("challenger")

        if not champion or not challenger:
            return False, "缺少 champion 或 challenger"

        c_metrics = champion.get("metrics", {})
        ch_metrics = challenger.get("metrics", {})

        # 检查最低样本量
        if ch_metrics.get("shadow_days", 0) < self.MIN_SHADOW_DAYS:
            return False, f"影子天数不足 ({ch_metrics.get('shadow_days', 0)}/{self.MIN_SHADOW_DAYS})"
        if ch_metrics.get("sample_count", 0) < self.MIN_SAMPLES:
            return False, f"样本不足 ({ch_metrics.get('sample_count', 0)}/{self.MIN_SAMPLES})"
        if ch_metrics.get("bet_count", 0) < self.MIN_BETS:
            return False, f"投注数不足 ({ch_metrics.get('bet_count', 0)}/{self.MIN_BETS})"

        # Brier 改进
        c_brier = c_metrics.get("brier_score", 1.0)
        ch_brier = ch_metrics.get("brier_score", 1.0)
        if c_brier > 0:
            brier_improvement = (c_brier - ch_brier) / c_brier
        else:
            brier_improvement = 0
        if brier_improvement < self.MIN_BRIER_IMPROVEMENT:
            return False, f"Brier 改进不足 ({brier_improvement:.3f} < {self.MIN_BRIER_IMPROVEMENT})"

        # Log-loss 改进
        c_ll = c_metrics.get("log_loss", 1.0)
        ch_ll = ch_metrics.get("log_loss", 1.0)
        if c_ll > 0:
            ll_improvement = (c_ll - ch_ll) / c_ll
        else:
            ll_improvement = 0
        if ll_improvement < self.MIN_LOGLOSS_IMPROVEMENT:
            return False, f"Log-loss 改进不足 ({ll_improvement:.3f} < {self.MIN_LOGLOSS_IMPROVEMENT})"

        # Brier skill > 0（必须打败市场）
        if ch_brier >= 0.25:  # 市场基准约 0.25
            return False, "Brier skill <= 0（未超越市场）"

        # ROI > 0
        if ch_metrics.get("roi", 0) <= 0:
            return False, "ROI <= 0"

        # 最大回撤不超过冠军
        if ch_metrics.get("max_drawdown", 1.0) > c_metrics.get("max_drawdown", 1.0):
            return False, "最大回撤超过冠军"

        return True, "所有条件满足，可以晋升"

    def promote(self):
        """执行晋升"""
        if self.registry.get("champion"):
            self.registry["previous_champion"] = self.registry["champion"]
        self.registry["champion"] = self.registry["challenger"]
        self.registry["challenger"] = None
        self.registry["history"].append({
            "action": "promote",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": self.registry["champion"]["model_name"],
        })
        self.save()

    def rollback(self):
        """回滚到上一个冠军"""
        if self.registry.get("previous_champion"):
            self.registry["champion"] = self.registry["previous_champion"]
            self.registry["previous_champion"] = None
            self.registry["history"].append({
                "action": "rollback",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            self.save()

    def check_rollback_needed(self, recent_metrics: ModelMetrics) -> bool:
        """检查是否需要回滚（冠军最近表现恶化）"""
        champion = self.registry.get("champion")
        prev = self.registry.get("previous_champion")
        if not champion or not prev:
            return False

        c_brier = champion.get("metrics", {}).get("brier_score", 0)
        p_brier = prev.get("metrics", {}).get("brier_score", 0)

        # 如果冠军比前任差 2% 以上，回滚
        if p_brier > 0 and (recent_metrics.brier_score - p_brier) / p_brier > 0.02:
            return True
        return False
