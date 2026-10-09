"""把诊断引擎的发现转成候选，让「发现问题」这一步自动化。

diagnostics.run_all 返回的是结构化发现（id/severity/detail），
本模块把它们包成 Candidate，与 known_issues 的候选走同一条裁决通道。

区别：
  - known_issues = 静态审计，一次性，主线 agent 也要看
  - diagnostics   = 动态扫描，每轮都跑，发现的是「当前数据里正在发生的问题」

事实性错误（blocking 的常数钉死、概率非法）标 auto_mergeable，走
invariant/audit 通道绕过统计门槛；效果类（校准反转、xG 漂移）走统计裁决。
"""

from __future__ import annotations

from .proposer import Candidate


_DIAG_TO_FIX = {
    # 诊断 id → 已知的修复方向（为 code_fix 或 config 候选提供 hint）
    "output_constant": {
        "kind": "code_fix",
        "target": "engine/prediction/fusion.py",
        "hint": "平局基线抬升把 draw 钉成常数，见 DRAW-BASELINE 语义修复",
    },
    "worse_than_market": {
        "kind": "config",
        "hint": "融合层输出劣于其市场输入，考虑降低 model_weight 或关停破坏性后处理",
    },
    "calibration_inversion": {
        "kind": "config",
        "hint": "高概率段过度自信，考虑 temperature/isotonic 或关 combo_boost",
    },
    "dead_feature": {
        "kind": "config",
        "hint": "常量特征对应的权重应显式归零",
    },
    "xg_drift": {
        "kind": "config",
        "hint": "分联赛 xg_calibration 需重拟合",
    },
    "prob_not_summing": {
        "kind": "code_fix",
        "target": "engine/main.py",
        "hint": "落盘 rounding 不守恒，需归一化后再写",
    },
    "pnl_negative": {
        "kind": "process",
        "hint": "负 EV 空间，北极星指标应从命中率换成 EV/ROI",
    },
    "market_anchor_missing": {
        "kind": "code_fix",
        "target": "engine/main.py",
        "hint": "合成赔率场次 market_fair=None，融合绕过市场",
    },
    "entropy_collapse": {
        "kind": "config",
        "hint": "输出过度集中，检查后处理是否在压熵",
    },
}

_FACTUAL = {"output_constant", "prob_not_summing", "pnl_negative",
            "market_anchor_missing", "dead_feature"}


def diagnostics_to_candidates(findings: dict, previous: list[str] | None = None
                              ) -> list[Candidate]:
    """把 diagnostics.run_all 的结果转成候选。

    previous: 已尝试过的 cid，跳过重复。
    """
    prev = set(previous or [])
    out: list[Candidate] = []
    for kind, fs in findings.items():
        if kind.endswith(":ERROR"):
            continue  # 诊断器出错不是候选
        hint = _DIAG_TO_FIX.get(kind, {})
        for f in fs:
            if not isinstance(f, dict):
                continue
            cid = f.get("id") or kind
            # 同一种诊断的多个发现（如 xg_drift 各联赛）要能共存，
            # 用 find 里的 scope/league 字段做后缀
            scope = f.get("league") or f.get("scope") or f.get("field") or ""
            if scope:
                cid = f"{cid}:{scope}"
            if cid in prev:
                continue
            auto = f.get("severity") == "blocking" or kind in _FACTUAL
            # 事实性错误走 audit 通道（auto_mergeable → 直接 ACCEPT），
            # 效果类走 backtest 通道（需统计裁决）。
            # 不能用 detect_by="invariant"：诊断发现的 id 与 invariants.py
            # 里的结构不变式 id 不是同一套命名空间。
            detect_by = "audit" if auto else "backtest"
            # 候选的 patch：事实性错误往往有 code_fix 目标；效果类先空 patch
            patch = {}
            target = hint.get("target")
            if hint.get("kind") == "code_fix" and target:
                patch = {"__code_fix__": target}
            out.append(Candidate(
                cid=cid,
                title=f.get("detail", cid)[:120],
                family=f"diagnostic/{f.get('severity', 'medium')}",
                patch=patch,
                rationale=f.get("detail", ""),
                evidence=f,
                detect_by=detect_by,
                auto_mergeable=auto,
                verify="diagnostics 动态扫描发现，供主线复核",
            ))
    return out