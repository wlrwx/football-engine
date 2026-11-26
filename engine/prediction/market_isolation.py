from __future__ import annotations
"""市场隔离审计（Market Isolation Audit）

背景
----
JetQiao/football-prediction-skill 的核心方法论之一：**市场隔离**。
- reference 市场（如 Pinnacle 收盘）：允许进入特征/概率形成
- target 市场（竞彩官方）：预测对象，**禁止**参与概率形成
- benchmark 市场（竞彩收盘）：只用于评估（CLV），不参与形成

如果模型把"目标市场赔率"直接当特征（如 lgbm 的 odds_home_impl），
等于用庄家的价格预测庄家的价格——预测会向市场收敛、价值发现退化。
本模块提供:
  1) 审计：给定一场预测的特征字典，判断哪些字段来自目标市场赔率
  2) 隔离建议：标记应删除/应替换的特征
  3) 输出审计报告，供 walk_forward / build_site 展示
"""

import json
from dataclasses import dataclass, field
from typing import Any


# 目标市场派生特征的黑名单（这些特征直接由竞彩赔率算出来）
# 出现在模型特征里 = 违反市场隔离
TARGET_MARKET_FEATURES = {
    # lgbm_model.build_features 里由 odds 派生的字段
    "odds_home_impl",
    "odds_draw_impl",
    "odds_away_impl",
    "odds_overround",
    # 其他常见目标市场派生字段
    "target_home_impl",
    "target_draw_impl",
    "target_away_impl",
    "target_overround",
    "sporttery_home_impl",
    "sporttery_draw_impl",
    "sporttery_away_impl",
    "sina_home_impl",  # 若 sina 是目标市场副本则同样违规
    "sina_draw_impl",
    "sina_away_impl",
}

# 允许进入特征的参考市场派生字段（reference，如 Pinnacle/亚盘参考）
REFERENCE_MARKET_FEATURES = {
    "pinnacle_home_impl",
    "pinnacle_draw_impl",
    "pinnacle_away_impl",
    "closing_home_impl",
    "closing_draw_impl",
    "closing_away_impl",
    "asian_handicap_line",  # 亚盘让球线（参考信号）
    "asian_handicap_odds",
    "over_under_line",
    # cross_market 参考市场特征（来自 Pinnacle / 首发信息，非目标竞彩盘）
    "pin_implied_home",
    "pin_implied_draw",
    "pin_implied_away",
    "pin_total_xg",
    "pin_xg_diff",
    "pin_move_home",
    "pin_move_draw",
    "pin_move_away",
    "pin_totals_shift",
    "pin_ah_shift",
    "lineup_strength_diff",
    "cross_market_missing",
}

# 仅用于评估、绝不入特征的字段
BENCHMARK_ONLY_FEATURES = {
    "benchmark_closing_home_impl",
    "benchmark_closing_draw_impl",
    "benchmark_closing_away_impl",
}


@dataclass
class IsolationViolation:
    feature: str
    reason: str
    severity: str  # high / medium / low


@dataclass
class IsolationReport:
    date: str = ""
    match_id: str = ""
    model_name: str = ""
    violations: list[IsolationViolation] = field(default_factory=list)
    used_reference_features: list[str] = field(default_factory=list)
    score: float = 1.0  # 1.0 = 完全隔离

    @property
    def status(self) -> str:
        return "PASS" if not self.violations else "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "match_id": self.match_id,
            "model_name": self.model_name,
            "violations": [
                {"feature": v.feature, "reason": v.reason, "severity": v.severity}
                for v in self.violations
            ],
            "used_reference_features": self.used_reference_features,
            "isolation_score": round(self.score, 3),
            "status": self.status,
        }


def audit_features(
    features: dict[str, Any],
    *,
    date: str = "",
    match_id: str = "",
    model_name: str = "",
) -> IsolationReport:
    """审计一个特征字典是否存在目标市场泄漏。

    用法::

        from engine.prediction.market_isolation import audit_features
        report = audit_features(features, model_name="lgbm")
        if report.violations:
            # 移除违规特征后再喂模型
            clean = {k: v for k, v in features.items()
                     if k not in {v.feature for v in report.violations}}
    """
    report = IsolationReport(date=date, match_id=match_id, model_name=model_name)

    for key in features:
        if key in TARGET_MARKET_FEATURES:
            report.violations.append(
                IsolationViolation(
                    feature=key,
                    reason="目标市场（竞彩）赔率派生特征进入模型 = 用庄家价格预测庄家价格",
                    severity="high",
                )
            )
        elif key in BENCHMARK_ONLY_FEATURES:
            report.violations.append(
                IsolationViolation(
                    feature=key,
                    reason="基准市场特征只能用于评估（CLV），不应参与概率形成",
                    severity="medium",
                )
            )
        elif key in REFERENCE_MARKET_FEATURES:
            report.used_reference_features.append(key)

    if report.violations:
        report.score = max(0.0, 1.0 - 0.5 * len(report.violations))
    return report


def strip_target_market_features(
    features: dict[str, Any],
    *,
    date: str = "",
    match_id: str = "",
    model_name: str = "",
) -> tuple[dict[str, Any], IsolationReport]:
    """返回 (隔离后特征, 审计报告)。自动移除违规特征。"""
    report = audit_features(
        features, date=date, match_id=match_id, model_name=model_name
    )
    banned = {v.feature for v in report.violations}
    clean = {k: v for k, v in features.items() if k not in banned}
    return clean, report


def audit_prediction_json(
    payload: dict[str, Any],
    *,
    feature_key: str = "features",
) -> IsolationReport:
    """审计一条预测 JSON 里的特征（供批处理/回测调用）。"""
    features = payload.get(feature_key) or {}
    return audit_features(
        features,
        date=payload.get("date", ""),
        match_id=payload.get("match_id", ""),
        model_name=payload.get("model_name", ""),
    )


def summarize(reports: list[IsolationReport]) -> dict[str, Any]:
    """汇总多条审计报告（供周报/页面展示）。"""
    if not reports:
        return {"count": 0, "status": "PASS"}
    violations = [v for r in reports for v in r.violations]
    by_severity: dict[str, int] = {}
    by_feature: dict[str, int] = {}
    for v in violations:
        by_severity[v.severity] = by_severity.get(v.severity, 0) + 1
        by_feature[v.feature] = by_feature.get(v.feature, 0) + 1
    return {
        "count": len(reports),
        "violation_count": len(violations),
        "by_severity": by_severity,
        "by_feature": by_feature,
        "status": "FAIL" if violations else "PASS",
        "avg_score": round(
            sum(r.score for r in reports) / len(reports), 3
        ),
    }


if __name__ == "__main__":
    # 自检演示
    demo = {
        "elo_home": 1500.0,
        "elo_away": 1430.0,
        "odds_home_impl": 0.45,   # ← 违规
        "odds_draw_impl": 0.28,   # ← 违规
        "pinnacle_home_impl": 0.42,  # ← 允许
        "home_form_pts": 6.0,
    }
    clean, rep = strip_target_market_features(demo, model_name="lgbm_demo")
    print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    print("clean keys:", sorted(clean))
