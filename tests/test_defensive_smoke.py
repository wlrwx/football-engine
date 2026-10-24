"""回归测试：导入冒烟 + 防御性修复（PR-B）。

锁定 docs/REVIEW_20261023.md 的 P1#6 / P1#7 / P1#10 / P1#15：
- plan_lock 顶层 import fcntl 让 Windows 上 engine.main 完全无法导入
- fusion 的 sina_data["compression"] 为 None 时崩溃
- djyy-only 分支缺置信门槛、缺键 KeyError
- circuit_breaker 状态文件损坏/未知键时崩溃
"""
from __future__ import annotations

import importlib
import json

import pytest

from engine.prediction.fusion import FusionInput, fuse_probabilities
from engine.strategy.circuit_breaker import BreakerState, CircuitBreaker


# ---------------------------------------------------------------- 导入冒烟

def test_import_engine_main() -> None:
    """整条主管线必须可导入。

    修复前：plan_lock.py 顶层 `import fcntl` → Windows 上
    ModuleNotFoundError，engine.main 完全不可用（本机实测）。
    这条测试在 Windows 上就是该 bug 的哨兵。
    """
    mod = importlib.import_module("engine.main")
    assert hasattr(mod, "main")


def test_import_integrity_modules() -> None:
    importlib.import_module("engine.integrity.plan_lock")
    importlib.import_module("engine.integrity.decision_bundle")
    importlib.import_module("engine.learning.elo_updater")
    importlib.import_module("engine.learning.calibration_auto")


# ---------------------------------------------------------------- fusion 防御

def _base_inp(**kw) -> FusionInput:
    cfg = {
        "model_weight": 0.10,
        "market_weight": 0.60,
        "djyy_weight": 0.30,
        "djyy_min_confidence": 0.50,
    }
    cfg.update(kw.pop("cfg", {}))
    return FusionInput(
        model_probs=(0.40, 0.30, 0.30),
        cfg=cfg,
        **kw,
    )


def test_fusion_compression_none_does_not_crash() -> None:
    """sina_data["compression"] 为 None 时不能 AttributeError（原实现必崩）。"""
    inp = _base_inp(
        market_probs=(0.45, 0.28, 0.27),
        sina_data={"movement": "up", "compression": None},
    )
    res = fuse_probabilities(inp)
    total = sum(res.probs)
    assert abs(total - 1.0) < 1e-6


def test_fusion_compression_non_dict_does_not_crash() -> None:
    inp = _base_inp(
        market_probs=(0.45, 0.28, 0.27),
        sina_data={"movement": "up", "compression": "oops"},
    )
    res = fuse_probabilities(inp)
    assert abs(sum(res.probs) - 1.0) < 1e-6


def test_fusion_djyy_only_low_confidence_current_contract() -> None:
    """固定**当前契约**：djyy-only 分支没有 djyy_min_confidence 门槛。

    这是评审 P1#7 指出的已知缺口，但补门槛会改变低置信 DJYY 场的输出
    概率（预测语义变更），需作者裁决——届时同步修改本测试。
    """
    inp = _base_inp(
        market_probs=None,
        djyy_probs={"home": 0.40, "draw": 0.25, "away": 0.35},  # max=0.40 < 0.50
    )
    res = fuse_probabilities(inp)
    assert res.source == "fuse_djyy_only"  # 现状：仍然参与融合（待决策）
    assert abs(sum(res.probs) - 1.0) < 1e-6


def test_fusion_djyy_only_high_confidence_keeps_djyy() -> None:
    inp = _base_inp(
        market_probs=None,
        djyy_probs={"home": 0.60, "draw": 0.25, "away": 0.15},  # max=0.60 >= 0.50
    )
    res = fuse_probabilities(inp)
    assert res.source == "fuse_djyy_only"
    assert res.effective_weights is not None
    assert res.effective_weights["djyy"] > 0


def test_fusion_djyy_missing_keys_does_not_crash() -> None:
    """上游只给 home 时不能 KeyError（原实现直接 djyy_probs["draw"]）。"""
    inp = _base_inp(
        market_probs=None,
        djyy_probs={"home": 0.62},
    )
    res = fuse_probabilities(inp)
    assert abs(sum(res.probs) - 1.0) < 1e-6


def test_fusion_djyy_none_value_does_not_crash() -> None:
    """{"draw": None} 时 max(values) 不能 TypeError。"""
    inp = _base_inp(
        market_probs=(0.45, 0.28, 0.27),
        djyy_probs={"home": 0.55, "draw": None, "away": 0.20},
    )
    res = fuse_probabilities(inp)
    assert abs(sum(res.probs) - 1.0) < 1e-6


# ---------------------------------------------------------------- 熔断状态

def test_circuit_breaker_corrupt_state_falls_back(tmp_path) -> None:
    """损坏的 state 文件不能炸掉实例化（原实现 JSONDecodeError）。"""
    p = tmp_path / "circuit_breaker.json"
    p.write_text("{ not json", encoding="utf-8")
    cb = CircuitBreaker(state_path=p)
    assert cb.state == BreakerState()


def test_circuit_breaker_unknown_keys_ignored(tmp_path) -> None:
    """schema 演进新增/改名键时不能 TypeError（原实现 **raw 直接炸）。"""
    p = tmp_path / "circuit_breaker.json"
    raw = {**BreakerState().__dict__, "future_field_v99": 42}
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    cb = CircuitBreaker(state_path=p)
    assert isinstance(cb.state, BreakerState)


def test_circuit_breaker_save_is_atomic(tmp_path) -> None:
    """保存后不应留下 .tmp 残留，且内容可读回。"""
    p = tmp_path / "circuit_breaker.json"
    cb = CircuitBreaker(state_path=p)
    cb.save()
    assert p.exists()
    assert not (tmp_path / "circuit_breaker.json.tmp").exists()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert "consecutive_losses" in raw or isinstance(raw, dict)
