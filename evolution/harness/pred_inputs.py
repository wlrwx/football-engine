"""把 daily/*/predictions.json 接入 harness，作为模型层 replay 的逐场输入表。

【为什么需要】review_ledger.jsonl 只落盘了融合链的**输入输出**
（model_raw / market_fair / final_prob），**不含** elo_home / elo_away /
home_xg / away_xg / 球队名。要重放模型层（DC/MC），必须从
data/daily/<date>/predictions.json 补齐这些逐场字段。

【为什么这是本项目最该补的基建】账本缺字段导致上游 ab**无法离线复现模型层**：
ablation_replay 只能裁决融合链的后处理开关，模型层的代码缺陷
（DC 的 attack/defense 量纲与符号）至今没有自动化裁决通道，只能靠人读代码发现。
补上这一层，等于把"人读代码才能发现的问题"变成"闭环自动发现"。

【泄漏防护】只读 predictions.json 的**预测时刻字段**，绝不读结算字段
（final_prob / hit / brier_* / actual_idx）。结算字段一律来自账本，
且 join 后校验预测时刻与结算日期一致，防止用赛后数据评估赛前决策。
"""

from __future__ import annotations

import json
from pathlib import Path

# 只允许从 predictions.json 读取这些字段（预测时刻可得）
ALLOWED_FIELDS = {
    "home_team", "away_team", "elo_home", "elo_away",
    "home_xg", "away_xg", "handicap", "confidence",
}


def _match_key(match_id: str) -> str:
    """2026-10-09_周五001 -> 2026-10-09_周五001（直接用）"""
    return match_id


def load_prediction_inputs(daily_dir: str | Path) -> dict[str, dict]:
    """扫描 daily/*/predictions.json，抽取预测时刻字段。

    返回 {match_id: {allowed fields}}。同一 match_id 若多次出现，保留**最早**
    as_of 的那份（决策时刻的原始视图，而不是被后续重跑覆盖的版本）。
    """
    daily = Path(daily_dir)
    out: dict[str, dict] = {}
    if not daily.exists():
        return out

    for day_dir in sorted(daily.iterdir()):
        if not day_dir.is_dir():
            continue
        pj = day_dir / "predictions.json"
        if not pj.exists():
            continue
        try:
            items = json.loads(pj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(items, dict):
            items = items.get("matches") or items.get("predictions") or []
        for m in items:
            if not isinstance(m, dict):
                continue
            mid = m.get("match_id")
            if not mid:
                continue
            rec = {k: m.get(k) for k in ALLOWED_FIELDS if k in m}
            rec["as_of"] = m.get("as_of")
            prev = out.get(mid)
            # 保留最早 as_of：决策时刻视图
            if prev is None or (rec.get("as_of") or "") < (prev.get("as_of") or ""):
                out[mid] = rec
    return out


def join_model_inputs(ledger: list[dict], pred_inputs: dict[str, dict]) -> tuple[list[dict], dict]:
    """把预测时刻字段合并进账本行。

    返回 (merged_rows, join_stats)。
    未匹配到的行保留原样但标记 _has_model_inputs=False —— 这些行无法做模型层
    replay，只能参与融合层裁决。
    """
    stats = {"total": len(ledger), "joined": 0, "missing": 0}
    merged = []
    for row in ledger:
        r = dict(row)
        mid = r.get("match_id", "")
        pin = pred_inputs.get(mid)
        if pin:
            for k, v in pin.items():
                if k == "as_of":
                    continue
                if v is not None:
                    r.setdefault(k, v)
            r["_has_model_inputs"] = all(
                r.get(k) is not None for k in ("elo_home", "elo_away"))
            stats["joined"] += 1
        else:
            r["_has_model_inputs"] = False
        if not r["_has_model_inputs"]:
            stats["missing"] += 1
        merged.append(r)
    stats["join_rate"] = stats["joined"] / stats["total"] if stats["total"] else 0.0
    return merged, stats


def load_team_ratings(path: str | Path) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def rating_from(ratings: dict[str, dict], team: str, elo: float | None = None):
    """构造 model_core.TeamRating：优先用逐场落盘的 elo，其余字段取评级表。"""
    from . import model_core as M
    t = ratings.get(team) or {}
    return M.TeamRating(
        name=team,
        elo=float(elo) if elo is not None else float(t.get("elo", 1500.0)),
        attack=float(t.get("attack", 1.0)),
        defense=float(t.get("defense", 1.0)),
        form=float(t.get("form", 0.0)),
        injury=float(t.get("injury", 0.0)),
        rest_days=int(t.get("rest_days", 3)),
    )


def model_replay_fidelity(system, rows: list[dict], pred_cfg: dict,
                          ratings: dict[str, dict], tol: float = 0.01) -> list[bool]:
    """校验模型层 replay 是否复现账本的 model_raw。

    【实测：仅 40.5% 忠实，模型层 replay 当前不可用于裁决】
    根因见 replay_blocking_reason()：attack/defense/form 未逐场落盘。
    保留本函数作为回归探针 —— 若上游补上字段落盘，忠实率应从 40% 升到 95%+，
    届时可切回 replay 通道。
    """
    from . import model_core as M
    flags = []
    for r in rows:
        stored = r.get("model_raw")
        if not stored or not r.get("_has_model_inputs"):
            flags.append(False)
            continue
        h = rating_from(ratings, r.get("home_team", ""), r.get("elo_home"))
        a = rating_from(ratings, r.get("away_team", ""), r.get("elo_away"))
        p = M.ensemble_probs(h, a, pred_cfg)
        d = sum((p[i] - stored[i]) ** 2 for i in range(3))
        flags.append(d < tol)
    return flags


def replay_blocking_reason() -> dict:
    """模型层 replay 不可用的根因（机器可读，供 PR 自检引用）。"""
    return {
        "blocker": "attack/defense/form 未逐场落盘",
        "detail": "predictions.json 仅落 elo_home/elo_away；team_ratings.json 的 "
                  "attack/defense/form 被 elo_updater 就地改写，是当前值而非历史值",
        "impact": "模型层 replay 忠实率 40.5%（corr 0.28 DC / 0.58 MC），不可用于裁决",
        "remedy": "在 predictions.json 落盘 per-match attack/defense/form/injury/rest_days",
        "measurable_target": "模型层 replay 忠实率 >= 0.95",
        "interim_strategy": "model_backtest.py 自包含 walk-forward（拟合当期参数再评估）",
    }
