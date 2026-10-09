"""活体复现门控：闭环的收敛性保障。

【为什么需要这个文件】

闭环的输入是**账本历史数据**，而 bug 的载体是**当前代码**。二者之间有时间差，
这个时间差会让闭环永不收敛：

    巴甲 draw_baseline bug 于 2026-10-09 修复
    但账本里 2026-08-30 ~ 10-08 的 19 场记录，draw 仍恒为 0.5025
    → 诊断引擎每次扫描账本都会重新发现「巴甲 draw 被钉死」
    → 闭环每次都提同一个已修好的问题
    → 永远不收敛

这不是数据问题，是**架构问题**：用历史记录断言当前代码状态，逻辑上不成立。

【解法】

事实性错误（auto_mergeable）在被裁决前，必须在**当前代码**上复现出来。
复现不了 → 判定为「已修复」→ 不产出候选，并在报告中显式说明原因。

与 known_issues 的分工：
  - known_issues  : 问题是什么、严重度、证据（诊断 + 静态描述）
  - 本文件       : 在当前代码上如何复现/证伪它（活体验证）
  - patches       : 怎么修

【原则】
探针失败（报错）≠ bug 已修复。探针无法判定时应保守地**不提**，
并在 reason 里写明「探针不可用」，避免把工具故障误报成「系统健康」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path


# --------------------------------------------------------------- 工具

def _read(repo: Path, rel: str) -> str | None:
    p = repo / rel
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def _json(repo: Path, rel: str):
    p = repo / rel
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------- 探针

def _probe_draw_baseline(repo: Path) -> tuple[bool, str]:
    """巴甲 draw_baseline 是否仍存着「判平精度」这种错值。

    修复后 league_params.json 会被迁移成真实平局率（0.266 附近）。
    若当前值落在真实平局率经验区间 [0.18, 0.36]，视为已修复。
    """
    lp = _json(repo, "data/league_params.json") or _json(repo, "data/state/league_params.json")
    if lp is None:
        return False, "无法读取 data/league_params.json，探针不可用"
    bra = lp.get("巴甲")
    if bra is None:
        return False, "league_params.json 中无巴甲条目，探针不可用"
    v = bra.get("draw_baseline")
    if v is None:
        return False, "巴甲无 draw_baseline 字段，探针不可用"
    if 0.18 <= v <= 0.36:
        return False, (f"巴甲 draw_baseline={v} 已落在真实平局率区间 "
                       f"[0.18,0.36]，判定为已修复")
    return True, (f"巴甲 draw_baseline={v} 仍在真实平局率区间 [0.18,0.36] 之外，"
                  f"疑似仍存判平精度错值")


def _probe_combo_boost(repo: Path) -> tuple[bool, str]:
    """combo_boost 熵减器是否仍在配置里启用。"""
    cfg = _json(repo, "config/prediction.json")
    if cfg is None:
        return False, "无法读取 config/prediction.json，探针不可用"
    post = (cfg.get("fusion") or {}).get("post_fusion") or cfg.get("post_fusion") or {}
    on = post.get("combo_boost")
    if on is False:
        return False, "config 中 combo_boost=false，已关闭，判定为已修复"
    if on is True:
        return True, "config 中 combo_boost=true 仍启用（该步骤只给 argmax 加分）"
    return False, f"config 中 combo_boost={on!r}，语义不明，探针不可用"


def _probe_league_draw_switch(repo: Path) -> tuple[bool, str]:
    """联赛平局基线抬升开关是否仍启用（历史错值的传导路径）。"""
    cfg = _json(repo, "config/prediction.json")
    if cfg is None:
        return False, "无法读取 config/prediction.json，探针不可用"
    post = (cfg.get("fusion") or {}).get("post_fusion") or cfg.get("post_fusion") or {}
    on = post.get("league_draw_baseline")
    if on is False:
        return False, "config 中 league_draw_baseline=false，已关闭"
    return True, f"config 中 league_draw_baseline={on!r} 仍启用"


def _probe_dead_features(repo: Path) -> tuple[bool, str]:
    """injury / rest_days 是否仍是常量（从未生效的权重）。"""
    tr = _json(repo, "data/models/team_ratings.json")
    if tr is None:
        return False, "无法读取 data/models/team_ratings.json，探针不可用"
    inj = set()
    rest = set()
    for v in tr.values():
        if isinstance(v, dict):
            if "injury" in v:
                inj.add(round(float(v["injury"]), 6))
            if "rest_days" in v:
                rest.add(round(float(v["rest_days"]), 6))
    if not inj or not rest:
        return False, "team_ratings 缺少 injury/rest_days 字段，探针不可用"
    # 常量 = 只有一种取值 → 该特征对所有球队相同 → 权重形同虚设
    dead = (len(inj) == 1) or (len(rest) == 1)
    if dead:
        return True, (f"injury 取值种数={len(inj)} {sorted(inj)[:3]}；"
                      f"rest_days 取值种数={len(rest)} {sorted(rest)[:3]}；"
                      f"存在常量特征 → 对应权重从未产生区分度")
    return False, (f"injury 取值种数={len(inj)}、rest_days 取值种数={len(rest)}，"
                   f"均非常量，判定为已修复")


def _probe_model_replay_fields(repo: Path) -> tuple[bool, str]:
    """predictions.json 是否已落盘 per-match attack/defense/form。"""
    daily = repo / "data" / "daily"
    if not daily.exists():
        return False, "data/daily 不存在，探针不可用"
    dates = sorted([d.name for d in daily.iterdir() if d.is_dir()])
    if not dates:
        return False, "data/daily 下无任何日期目录，探针不可用"
    # 只看最近 7 天：老目录是修复前生成的，不能作为判据
    for day in reversed(dates[-7:]):
        pf = daily / day / "predictions.json"
        if not pf.exists():
            continue
        try:
            preds = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(preds, dict):
            preds = preds.get("predictions", [])
        if not preds:
            continue
        need = {"attack_home", "defense_home", "form_home"}
        have = need & set(preds[0].keys())
        if len(have) == len(need):
            return False, (f"{day} 的 predictions.json 已含 "
                           f"{sorted(need)}，判定为已修复")
    return True, ("最近 7 天的 predictions.json 均未落盘 "
                  "attack/defense/form，模型层无法离线重放")


def _probe_dc_dimension(repo: Path) -> tuple[bool, str]:
    """Dixon-Coles 的 attack/defense 是否仍以比例因子直接相加进对数域。"""
    src = _read(repo, "engine/prediction/dixon_coles.py")
    if src is None:
        return False, "无法读取 dixon_coles.py，探针不可用"
    bad = re.search(r"\+\s*\w+\.attack\s*\*\s*cfg\.attack_weight", src)
    if bad:
        return True, "dixon_coles 仍把 attack 比例因子直接相加进对数域（量纲错）"
    if "math.log(max(0.3" in src:
        return False, "dixon_coles 已改用 log 形式，判定为已修复"
    return False, "dixon_coles 未检出预期的 log 形式，探针不可用"


def _probe_ledger_chain(repo: Path) -> tuple[bool, str]:
    """ablation_replay 是否已按 chain 过滤（避免重放旧链行）。"""
    src = _read(repo, "scripts/ablation_replay.py")
    if src is None:
        return False, "无法读取 ablation_replay.py，探针不可用"
    if 'chain") == "v2"' in src or "chain') == 'v2'" in src:
        return False, "ablation_replay 已按 chain=v2 过滤，判定为已修复"
    return True, "ablation_replay 未按 chain 过滤，重放会混入旧链行"


PROBES = {
    "DRAW-ANCHOR-JUDGMENT-AS-RATE": _probe_draw_baseline,
    "LEAGUE-DRAW-ANCHOR-HARDCODED": _probe_draw_baseline,
    "COMBO-BOOST-ENTROPY-REDUCTOR": _probe_combo_boost,
    "DEAD-FEATURES-INJURY-REST": _probe_dead_features,
    "MODEL-REPLAY-UNREPRODUCIBLE": _probe_model_replay_fields,
    "DC-ATTACK-LOG-DIMENSION": _probe_dc_dimension,
    "LEDGER-MULTI-CHAIN-CONTAMINATION": _probe_ledger_chain,
    "PREDICTION-TIMING-BUCKET-NO-ASOF": _probe_model_replay_fields,
    "SYNTHETIC-ODDS-NO-MARKET-ANCHOR": None,  # 需 scan 上下文，见 diagnostics
}


def probe(issue_id: str, repo_path) -> tuple[bool, str] | None:
    """在当前代码上复现 issue。返回 (still_broken, detail)。

    返回 None 表示「没有探针」——调用方应保守地不提这个候选。
    """
    fn = PROBES.get(issue_id)
    if fn is None:
        return None
    return fn(Path(repo_path))


def gate(issue_id: str, repo_path) -> tuple[bool, str]:
    """收敛性门控：决定一个事实性 issue 本轮是否还该提。

    返回 (allow, reason)。
      - allow=True  : 在当前代码上仍能复现 → 该提
      - allow=False : 已修复 / 探针不可用 → 不提

    注意区分「已修复」与「探针不可用」：前者是健康的静默，
    后者是工具故障，必须在 reason 里暴露出来。
    """
    r = probe(issue_id, repo_path)
    if r is None:
        return False, "无活体探针，保守不提（工具限制，不代表系统健康）"
    still_broken, detail = r
    if still_broken:
        return True, f"当前代码仍可复现：{detail}"
    return False, f"当前代码已不复现：{detail}"


def gate_many(issue_ids, repo_path) -> dict[str, tuple[bool, str]]:
    return {i: gate(i, repo_path) for i in issue_ids}