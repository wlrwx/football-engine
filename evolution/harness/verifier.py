"""候选裁决器 —— 判断一个改动是否真的更好。

裁决纪律（本文件是整套 harness 的核心，规则必须硬）：
  1. walk-forward 切分：选择只用 train，验证只用其后的 val，绝不混用。
  2. 配对检验：候选与基线在同一批比赛上逐场比较。
  3. BH-FDR 校正：候选是批量产生的，裸 p 值不可用。
  4. 最小效应量门槛：|Δ| 太小即使显著也不合入（防止统计显著但无业务意义）。
  5. 稳健性检查：val 段不得反向恶化。
  6. **必须能否决**：若没有任何候选通过，输出 ACCEPT_NONE 是合法且常见的结果。

任何一条放宽，闭环就会退化成"随机扰动 + 挑选好看的数字"。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, asdict
from typing import Any

from . import evaluator as E
from . import fusion_core as S


# ---------------------------------------------------------------- 裁决门槛

@dataclass
class VerdictPolicy:
    """裁决策略。所有阈值集中在此，便于主线 agent 审计与调整。"""

    metric: str = "brier"
    alpha: float = 0.10            # BH-FDR 的目标水平
    min_delta: float = 0.0015      # 最小有意义改善（ΔBrier 绝对值下限）
    max_val_regression: float = 0.001  # val 段最大允许恶化
    min_val_n: int = 100           # val 段最小样本
    min_train_n: int = 150
    # 安全护栏：候选若让某个子集显著变差，直接否决
    guardrail_metrics: tuple = ("logloss",)
    require_hit_non_regression: bool = True


@dataclass
class CandidateVerdict:
    cid: str
    family: str
    title: str
    rationale: str
    decision: str                       # ACCEPT / REJECT / REJECT_GUARDRAIL / INSUFFICIENT
    reason: str
    train_delta: float = 0.0
    val_delta: float = 0.0
    t: float = 0.0
    p_raw: float = 1.0
    p_bh: float = 1.0
    train_n: int = 0
    val_n: int = 0
    hit_delta_train: float = 0.0
    hit_delta_val: float = 0.0
    guardrail: dict = field(default_factory=dict)
    prior: str = "neutral"
    touches_code: bool = False
    files: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 应用候选

def apply_candidate(cand, base_cfg: dict, base_post: dict) -> tuple[dict, dict]:
    """把候选的 patch 应用到配置上，返回 (cfg, post)。"""
    cfg = copy.deepcopy(base_cfg)
    post = copy.deepcopy(base_post)
    p = getattr(cand, "patch", cand)
    if "cfg" in p:
        cfg.update(p["cfg"])
    if "post" in p:
        post.update(p["post"])
    # code 类候选在 fusion_core 中有对应扩展点
    if "code" in p:
        post.setdefault("_code", [])
        post["_code"] = list(post["_code"]) + [p["code"]]
    return cfg, post


# ---------------------------------------------------------------- 裁决

def judge(cand, base_cfg: dict, base_post: dict, train: list[dict],
          val: list[dict], base_train: dict, base_val: dict,
          policy: VerdictPolicy, bh_pass: bool = False) -> CandidateVerdict:
    """对单个候选做裁决。"""
    cid = cand.cid
    cfg, post = apply_candidate(cand, base_cfg, base_post)
    metric = policy.metric

    # --- train 段 ---
    cand_train = E.evaluate(S, train, cfg, post, metric)
    pt = E.paired_test(cand_train["per_match"], base_train["per_match"], metric)

    hit_tr = cand_train["hit"] - base_train["hit"]

    v = CandidateVerdict(
        cid=cid, family=cand.family, title=cand.title,
        rationale=cand.rationale, decision="REJECT", reason="",
        train_delta=pt["delta"], t=pt["t"], p_raw=pt.get("p_one_sided", 1.0),
        train_n=pt["n"], hit_delta_train=hit_tr,
        prior=cand.prior, touches_code=cand.touches_code, files=list(cand.files),
    )

    # --- 样本量门槛 ---
    if pt["n"] < policy.min_train_n:
        v.decision = "INSUFFICIENT"
        v.reason = f"train n={pt['n']} < {policy.min_train_n}"
        return v

    # --- 护栏：val 段不得反向恶化 ---
    cand_val = E.evaluate(S, val, cfg, post, metric)
    pv = E.paired_test(cand_val["per_match"], base_val["per_match"], metric)
    v.val_delta = pv["delta"]
    v.val_n = pv["n"]
    v.hit_delta_val = cand_val["hit"] - base_val["hit"]

    if v.val_n < policy.min_val_n:
        v.decision = "INSUFFICIENT"
        v.reason = f"val n={v.val_n} < {policy.min_val_n}"
        return v

    # val 显著恶化 → 直接否决（稳健性硬门槛）
    if v.val_delta > policy.max_val_regression:
        v.decision = "REJECT_GUARDRAIL"
        v.reason = (f"val 段恶化 Δ={v.val_delta:+.5f} 超过上限 "
                    f"{policy.max_val_regression:+.5f}")
        return v

    # --- 效果门槛 ---
    improves = v.train_delta <= -policy.min_delta
    if not improves:
        v.decision = "REJECT"
        v.reason = (f"train Δ={v.train_delta:+.5f} 未达最小效应量 "
                    f"{-policy.min_delta:+.5f}")
        return v

    # --- 显著性 ---
    if v.p_raw > policy.alpha:
        v.decision = "REJECT"
        v.reason = (f"train Δ={v.train_delta:+.5f} 但 p={v.p_raw:.3f} > "
                    f"α={policy.alpha}，改进不显著（多重比较下更不可信）")
        return v

    if not bh_pass:
        v.decision = "REJECT"
        v.reason = (f"未通过 BH-FDR 校正（p_raw={v.p_raw:.3f}，批量候选下"
                    f"裸 p 值不足以支撑合入）")
        return v

    # --- 命中率护栏（可选） ---
    if policy.require_hit_non_regression and v.hit_delta_val < -0.02:
        v.decision = "REJECT_GUARDRAIL"
        v.reason = (f"val 命中率下降 {v.hit_delta_val:+.4f}，"
                    f"虽 Brier 改善但业务口径恶化")
        return v

    v.decision = "ACCEPT"
    v.reason = (f"train Δ={v.train_delta:+.5f} (t={v.t:.2f}, p={v.p_raw:.3f}, "
                f"BH通过) | val Δ={v.val_delta:+.5f} | "
                f"命中 Δtrain={v.hit_delta_train:+.4f} Δval={v.hit_delta_val:+.4f}")
    return v


def judge_issue(c, base_cfg: dict, base_post: dict, ctx: dict) -> dict:
    """裁决 known_issues 类候选。

    与 judge() 的关键区别：**不是所有 issue 都能用 Brier 配对检验裁决**。

      - detect_by="backtest"    → 跑 model_backtest，比较变体
      - detect_by="invariant"   → 跑不变式检查（结构正确性）
      - detect_by="audit"       → 事实性错误，依据 evidence 直接判定
      - auto_mergeable=True     → 事实性缺陷，绕过统计门槛（量纲错了就是错了）

    这条通道存在的理由：如果强行用 Brier 裁决「DC 量纲错误」，
    系统会因为「市场权重占大头，改动看不出效果」而永远不修一个已确认的 bug。
    """
    from . import known_issues as KI
    issue = KI.by_id(c.cid)
    verdict = {
        "cid": c.cid,
        "title": c.title,
        "family": c.family,
        "is_issue": True,
        "severity": issue.get("severity") if issue else "medium",
        "layer": issue.get("layer") if issue else "unknown",
        "detect_by": c.detect_by,
        "auto_mergeable": c.auto_mergeable,
        "rationale": c.rationale,
        "evidence": c.evidence,
        "verify": c.verify,
        "side_effects": c.side_effects,
        "prior": c.prior,
        "files": c.files,
    }

    # ---- 通道 1：backtest ----
    if c.detect_by == "backtest" and ctx.get("backtest_rows"):
        res = ctx["backtest_rows"]
        verdict["backtest"] = res
        if c.cid == "DC-ATTACK-LOG-DIMENSION":
            a = res["results"]["dc_buggy_linear"]["brier"]
            b = res["results"]["fixed_log_form"]["brier"]
            gain = a - b
            verdict["backtest_gain"] = gain
            if gain > 0:
                verdict["decision"] = "ACCEPT"
                verdict["reason"] = (
                    f"log_form 修复在 {res['cut']} 时间切分上使 Brier 改善 {gain:+.4f}，"
                    f"且机制上修复了量纲与符号两个定义错误")
            else:
                verdict["decision"] = "REJECT"
                verdict["reason"] = f"backtest 未确认修复有效（gain={gain:+.4f}）"
            return verdict

    # ---- 通道 2b：真实源码补丁（可自动修复的代码缺陷）----
    # 这条通道是闭环从「发现问题」走到「修复问题」的关键：
    # 在工作副本上应用补丁 → 编译检查 → 提取被改函数执行验证 →
    # 失败则不出 PR。验证的是真实源码文本，不是另写的副本。
    if c.detect_by == "patch" and ctx.get("repo_path"):
        rep = apply_code_patch(c, ctx)
        verdict["patch_result"] = rep
        verdict["decision"] = "ACCEPT" if rep["ok"] else "REJECT"
        verdict["reason"] = rep["reason"]
        return verdict

    # ---- 通道 2：invariant ----
    if c.detect_by == "invariant" and ctx.get("invariants"):
        # known_issues 的 issue id 与 invariant id 不一定相同，
        # 用 invariant_id 字段显式映射（见 known_issues）
        inv_key = c.invariant_id if c.invariant_id else c.cid
        inv = ctx["invariants"].get(inv_key)
        if inv is None:
            for k, v in ctx["invariants"].items():
                if v.get("issue_id") == c.cid:
                    inv = v
                    break
        if inv is not None:
            verdict["invariant"] = inv
            if inv.get("violated"):
                verdict["decision"] = "ACCEPT"
                verdict["reason"] = (
                    f"检测到不变式违反：{inv.get('detail', '')}。"
                    "这是结构性错误，非效果调优。")
                return verdict
            verdict["decision"] = "REJECT"
            verdict["reason"] = "未检测到不变式违反（可能已修复或观测不足）"
            return verdict

    # ---- 通道 3：audit + 事实性错误 ----
    if c.auto_mergeable:
        verdict["decision"] = "ACCEPT"
        verdict["reason"] = (
            "事实性错误，依据静态证据判定，不依赖统计显著性："
            + "；".join(f"{k}={v}" for k, v in list(c.evidence.items())[:3]))
        return verdict

    # ---- 通道 4：fallback 交给标准 Brier 门槛 ----
    verdict["decision"] = "NEEDS_DATA"
    verdict["reason"] = (
        "该问题无法用现有数据裁决（需要工程改造、前瞻实盘或新数据源），"
        "已转为人工评审项。")
    return verdict


def apply_code_patch(c, ctx: dict) -> dict:
    """在工作副本上应用补丁并验证 —— 闭环的「自动修复」执行器。

    流程（任一步失败即放弃，不产出 PR）：
      1. 从 git 导出干净快照（不碰用户工作区）
      2. 若补丁已生效 → 说明上游已修，跳过
      3. 应用补丁；替换未命中/命中多次 → 失败
      4. py_compile 语法检查
      5. 提取被改函数执行 fails_before/验证（验证真实源码文本）
      6. 通过 → 在工作副本建分支 + commit，返回 commit 信息供 PR 使用
      7. 无论成败都清理工作副本

    返回 dict(ok, reason, commit, diff, cleanup_note)
    """
    from . import patches as PA
    from . import patch_engine as PE
    from . import git_writer as GW
    from pathlib import Path

    repo = Path(ctx["repo_path"])
    patch = PA.get(c.cid)
    if patch is None:
        return {"ok": False, "reason": f"没有为 {c.cid} 声明可执行补丁",
                "commit": None, "diff": ""}

    wt = None
    try:
        wt = GW.make_worktree(repo, ref=ctx.get("repo_ref", "main"))
        target = wt / patch.target
        if not target.exists():
            return {"ok": False, "reason": f"目标文件不存在: {patch.target}",
                    "commit": None, "diff": ""}
        src = target.read_text(encoding="utf-8")

        # 幂等：已修复的不重复提
        if PE.is_already_applied(src, patch):
            return {"ok": False,
                    "reason": f"补丁已生效（{patch.issue_id} 疑似已被修复），不重复提",
                    "commit": None, "diff": ""}

        new_src = PE.apply_patch(src, patch)

        ok_c, err_c = PE.compiles(new_src, patch.target)
        if not ok_c:
            return {"ok": False, "reason": f"补丁后语法错误：{err_c}",
                    "commit": None, "diff": ""}

        # 补丁自带的验证器（真实源码执行）
        if patch.verify is not None:
            v_ok, v_detail = patch.verify(new_src, patch.target)
            if not v_ok:
                return {"ok": False, "reason": f"补丁验证未通过：{v_detail}",
                        "commit": None, "diff": ""}
            verify_detail = v_detail
        else:
            verify_detail = "（该补丁未声明验证器）"

        diff = PE.unified_summary(src, new_src, patch.target)
        GW.apply_to_file(wt, patch.target, new_src)

        branch = f"evolution/auto/{patch.issue_id.lower().replace('_','-')}"
        msg = (f"fix({patch.issue_id}): 自动进化闭环修复\n\n"
               f"{c.title}\n\n"
               f"补丁指纹 {patch.fingerprint()}\n"
               f"验证: {verify_detail}\n\n"
               f"本改动由 evolution 闭环提出并自动验证，待主线 agent 审核。")
        info = GW.commit_branch(wt, branch, msg, [patch.target])

        # 把可复核的 patch 文件与分支元数据落盘到 out_dir，
        # 供主线 agent 用 git apply 复现或核对（不 push，不代主决策）。
        patch_path = None
        out_dir = ctx.get("out_dir")
        if out_dir:
            from pathlib import Path as _P
            od = _P(out_dir); od.mkdir(parents=True, exist_ok=True)
            slug = patch.issue_id.lower().replace("_", "-")
            patch_path = od / f"patch_{slug}.patch"
            patch_path.write_text(PE.unified_summary(src, new_src, patch.target),
                                  encoding="utf-8")
            meta = {"@type": "autofix", "branch": branch,
                    "commit": info["commit"], "issue": patch.issue_id,
                    "target": patch.target, "verify": verify_detail,
                    "patch_file": str(patch_path)}
            (od / f"autofix_{slug}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return {"ok": True,
                "reason": (f"补丁已应用并通过验证（{verify_detail}）；"
                           f"分支 {branch} @ {info['commit'][:8]}"),
                "commit": info, "diff": diff, "branch": branch,
                "patch_file": str(patch_path) if patch_path else None}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "reason": f"补丁流程异常：{type(ex).__name__}: {ex}",
                "commit": None, "diff": ""}
    finally:
        if wt is not None:
            GW.cleanup(wt)


def judge_all(candidates, base_cfg: dict, base_post: dict, ledger: list[dict],
              policy: VerdictPolicy = None, ctx: dict | None = None) -> dict:
    """裁决整轮候选。返回含排序结果与统计摘要的报告。

    前置：ledger 必须已 scope_current_chain + filter_trusted 处理，
    否则裁决的是 replay 误差而非改动效果。
    """
    policy = policy or VerdictPolicy()
    ctx = ctx or {}
    train, val = E.walk_forward_split(ledger, min_train=policy.min_train_n,
                                      val_frac=0.4)

    base_train = E.evaluate(S, train, base_cfg, base_post, policy.metric)
    base_val = E.evaluate(S, val, base_cfg, base_post, policy.metric)

    # 第一遍：算出所有候选的 p 值（BH 需要全体 p）
    # known_issues 类候选走 judge_issue 专用通道（backtest/invariant/audit），
    # 不混入 BH 校正 —— 它们不是同一类假设检验。
    verdicts = []
    stat_verdicts = []
    for c in candidates:
        if c.is_issue:
            verdicts.append(judge_issue(c, base_cfg, base_post, ctx))
        else:
            v = judge(c, base_cfg, base_post, train, val, base_train, base_val, policy)
            verdicts.append(v)
            stat_verdicts.append(v)

    pmap = {v.cid: v.p_raw for v in stat_verdicts if v.train_n >= policy.min_train_n}
    bh = E.benjamini_hochberg(pmap, alpha=policy.alpha)

    # 第二遍：应用 BH 结论
    final = []
    for v in verdicts:
        is_issue = getattr(v, "is_issue", False) or (isinstance(v, dict) and v.get("is_issue"))
        if is_issue:
            # issue 类统一为 dict；statistic 类统一为 CandidateVerdict 对象
            final.append(v if isinstance(v, dict) else v.to_dict())
            continue
        entry = bh.get(v.cid)
        if entry:
            v.p_bh = entry["p"]
            if v.decision == "REJECT" and "未通过 BH" in v.reason:
                if entry["pass_bh"]:
                    v.decision = "ACCEPT"
                    v.reason = (f"train Δ={v.train_delta:+.5f} (t={v.t:.2f}, "
                                f"p_raw={v.p_raw:.3f}) 通过 BH-FDR α={policy.alpha} | "
                                f"val Δ={v.val_delta:+.5f}")
        final.append(v.to_dict())

    accepted = [v for v in final if v["decision"] == "ACCEPT"]
    accepted.sort(key=lambda v: (v.get("severity", "zzz"), v.get("train_delta", 0.0)))

    counts = {}
    for v in final:
        counts[v["decision"]] = counts.get(v["decision"], 0) + 1

    # 分联赛基线：整体指标会淡化单联赛的严重问题
    # （实测巴甲 final 命中率 0.316 vs 市场 0.632，但整体仅体现为小缺口）
    per_league = {}
    for row in val:
        lg = row.get("league")
        if not lg:
            continue
        d = per_league.setdefault(lg, {"n": 0, "hits": 0, "brier_sum": 0.0})
        d["n"] += 1
        d["hits"] += 1 if E.argmax(row["market_fair"]) == row["actual_idx"] else 0
        d["brier_sum"] += E.brier(row["market_fair"], row["actual_idx"])
    league_view = {
        k: {"n": v["n"], "market_hit": round(v["hits"] / v["n"], 4),
            "market_brier": round(v["brier_sum"] / v["n"], 4)}
        for k, v in sorted(per_league.items(), key=lambda kv: -kv[1]["n"])
    }

    return {
        "policy": {
            "metric": policy.metric, "alpha": policy.alpha,
            "min_delta": policy.min_delta,
            "max_val_regression": policy.max_val_regression,
        },
        "split": {"train_n": len(train), "val_n": len(val)},
        "replay_scope": {
            "ledger_in": len(ledger),
            "note": "已限定当前链且通过 replay 忠实性检验",
        },
        "baseline": {
            "train": {k: base_train[k] for k in ("n", "mean", "hit")},
            "val": {k: base_val[k] for k in ("n", "mean", "hit")},
        },
        "counts": counts,
        "accepted": accepted,
        "rejected": [v for v in final if v["decision"] != "ACCEPT"],
        "needs_data": [v for v in final if v["decision"] == "NEEDS_DATA"],
        "bh_table": bh,
        "market_reference_val": league_view,
    }
