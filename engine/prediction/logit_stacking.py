from __future__ import annotations
"""Log-Odds Stacking 概率融合器（替代简单线性池化，保留极端概率锐度）。"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from scipy.optimize import minimize


def logit_pool(
    probs_list: Sequence[tuple[float, float, float]],
    weights: Sequence[float],
    intercept: Sequence[float] | None = None,
    eps: float = 1e-6,
) -> tuple[float, float, float]:
    """在 log-odds 空间加权池化多路三分类概率并经 softmax 归一化。

    log p_out[c] = b[c] + sum_k w_k * log(clip(p_k[c], eps, 1 - eps))
    """
    if not probs_list:
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)

    logits = np.zeros(3, dtype=float)
    if intercept is not None and len(intercept) >= 3:
        logits += np.asarray(intercept[:3], dtype=float)

    w_sum = 0.0
    for probs, w in zip(probs_list, weights):
        wf = float(w)
        if wf == 0.0:
            continue
        arr = np.clip(np.asarray(probs[:3], dtype=float), eps, 1.0 - eps)
        s = float(arr.sum())
        if s > 0:
            arr = arr / s
        logits += wf * np.log(arr)
        w_sum += wf

    if w_sum == 0.0 and (intercept is None):
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)

    logits -= float(np.max(logits))
    exp_v = np.exp(logits)
    tot = float(exp_v.sum())
    if tot <= 0.0 or not math.isfinite(tot):
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
    out = exp_v / tot
    return (float(out[0]), float(out[1]), float(out[2]))


@dataclass
class LogitStacker:
    """带 L2 正则的有序/多分类 Log-Odds 融合器（model / market / djyy）。"""

    weights: list[float] = field(default_factory=lambda: [0.1, 0.7, 0.2])
    intercept: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    l2_reg: float = 1.0
    eps: float = 1e-6
    loss: str = "nll"  # "nll" | "rps"
    fitted: bool = False
    n_samples: int = 0

    def fit(
        self,
        sources_list: Sequence[dict[str, tuple[float, float, float] | None]],
        actuals: Sequence[int],
        bounds: Sequence[tuple[float, float]] | None = None,
    ) -> "LogitStacker":
        """用 L-BFGS-B 在 (model, market, djyy) 通道上拟合非负权重与截距。"""
        n = len(sources_list)
        if n == 0 or n != len(actuals):
            return self

        keys = ("model", "market", "djyy")
        # 预计算每场 log 概率矩阵 (n, 3_sources, 3_classes) 与可用掩码
        log_mats = np.zeros((n, 3, 3), dtype=float)
        masks = np.zeros((n, 3), dtype=float)
        for i, src in enumerate(sources_list):
            for k_idx, k_name in enumerate(keys):
                p = src.get(k_name)
                if p and len(p) >= 3:
                    arr = np.clip(np.asarray(p[:3], dtype=float), self.eps, 1.0 - self.eps)
                    s = float(arr.sum())
                    if s > 0:
                        arr = arr / s
                        log_mats[i, k_idx, :] = np.log(arr)
                        masks[i, k_idx] = 1.0

        y = np.asarray(actuals, dtype=int)
        y_onehot = np.zeros((n, 3), dtype=float)
        y_onehot[np.arange(n), y] = 1.0
        y_cum = np.cumsum(y_onehot, axis=1)

        w0 = np.asarray(self.weights[:3], dtype=float)
        if w0.sum() > 0:
            w0 = w0 / w0.sum()
        else:
            w0 = np.array([1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0])
        x0 = np.concatenate([w0, np.zeros(3, dtype=float)])

        w_bounds = list(bounds[:3]) if bounds else [(0.0, 2.0), (0.0, 2.0), (0.0, 2.0)]
        b_bounds = [(-1.5, 1.5), (-1.5, 1.5), (-1.5, 1.5)]
        all_bounds = w_bounds + b_bounds

        def _obj(params: np.ndarray) -> float:
            w = params[:3]
            b = params[3:6]
            # 对缺失源按可用权重重归一，保持全样本尺度一致
            active_w = masks * w[None, :]
            row_sum = np.maximum(active_w.sum(axis=1, keepdims=True), 1e-8)
            norm_w = active_w / row_sum
            logits = np.einsum("ns,nsc->nc", norm_w, log_mats) + b[None, :]
            logits -= logits.max(axis=1, keepdims=True)
            exp_v = np.exp(logits)
            probs = np.clip(exp_v / exp_v.sum(axis=1, keepdims=True), 1e-12, 1.0 - 1e-12)
            if self.loss == "rps":
                p_cum = np.cumsum(probs, axis=1)
                core = float(np.mean(np.sum((p_cum - y_cum) ** 2, axis=1) / 2.0))
            else:
                core = float(-np.mean(np.log(probs[np.arange(n), y])))
            reg = (self.l2_reg / max(1, n)) * float(np.sum((w - 1.0 / 3.0) ** 2) + np.sum(b ** 2))
            return core + reg

        res = minimize(_obj, x0, method="L-BFGS-B", bounds=all_bounds)
        opt = res.x if res.success or res.x is not None else x0
        w_fit = np.maximum(0.0, opt[:3])
        if w_fit.sum() > 0:
            w_fit = w_fit / w_fit.sum()
        b_fit = opt[3:6] - float(np.mean(opt[3:6]))

        self.weights = [float(x) for x in w_fit]
        self.intercept = [float(x) for x in b_fit]
        self.fitted = True
        self.n_samples = n
        return self

    def predict_probs(
        self,
        model_probs: tuple[float, float, float],
        market_probs: tuple[float, float, float] | None = None,
        djyy_probs: tuple[float, float, float] | None = None,
    ) -> tuple[float, float, float]:
        active_p: list[tuple[float, float, float]] = [model_probs]
        active_w: list[float] = [self.weights[0]]
        if market_probs is not None:
            active_p.append(market_probs)
            active_w.append(self.weights[1])
        if djyy_probs is not None:
            active_p.append(djyy_probs)
            active_w.append(self.weights[2])
        tot = sum(active_w)
        if tot > 0:
            active_w = [w / tot for w in active_w]
        else:
            active_w = [1.0 / len(active_p)] * len(active_p)
        return logit_pool(active_p, active_w, intercept=self.intercept, eps=self.eps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": [round(float(x), 6) for x in self.weights],
            "intercept": [round(float(x), 6) for x in self.intercept],
            "l2_reg": self.l2_reg,
            "eps": self.eps,
            "loss": self.loss,
            "fitted": self.fitted,
            "n_samples": self.n_samples,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "LogitStacker":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            weights=[float(x) for x in payload.get("weights", [0.1, 0.7, 0.2])],
            intercept=[float(x) for x in payload.get("intercept", [0.0, 0.0, 0.0])],
            l2_reg=float(payload.get("l2_reg", 1.0)),
            eps=float(payload.get("eps", 1e-6)),
            loss=str(payload.get("loss", "nll")),
            fitted=bool(payload.get("fitted", False)),
            n_samples=int(payload.get("n_samples", 0)),
        )
