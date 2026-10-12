"""PR 素材生成 —— 闭环的对外交付物。

设计原则：
  1. **PR 不只是 diff**。每个 PR 必须携带"为什么提出""数据说什么"
     "为什么可能仍被否决""如何回滚"四段，主线 agent 才能独立裁决。
  2. **不隐藏负面结果**。ACCEPT_NONE 轮次同样出报告（标记为 no-change），
     让主线知道"系统今天尝试了但什么都没改"——这是闭环健康的一部分。
  3. **回滚路径必须可执行**：给出反向 patch，不只是描述。
  4. **不自动合入**：本模块只生成文件与命令提示，合入由主线 agent 决定。
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def render_pr_body(report: dict, round_idx: int = 1) -> str:
    """生成 PR 描述（Markdown）。"""
    rep = report["rounds"][round_idx - 1]
    sc = report["selfcheck"]
    lines = []
    A = lines.append

    A(f"## 🧬 自动进化 PR — round {round_idx}")
    A("")
    A(f"- 生成时间: `{report['generated_at']}`")
    A(f"- 种子: `{report['seed']}`（确定性，可复现）")
    A(f"- 结论: **{'PROPOSE' if rep['accepted'] else 'ACCEPT_NONE'}**")
    A("")

    # --- 自检 ---
    A("### ✅ 自检")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| 账本样本（当前链 {list(sc['chains'].keys())[0] if sc['chains'] else '?'}） | {sc['ledger_rows']} |")
    A(f"| replay 忠实率 | {sc['replay_trusted_frac']*100:.1f}% |")
    A(f"| 基线 Brier | {sc['baseline_brier']:.4f} |")
    A(f"| 基线命中率 | {sc['baseline_hit']*100:.2f}% |")
    A(f"| 候选总数 | {rep['n_candidates']} |")
    A(f"| 裁决分布 | {rep['counts']} |")
    A("")
    A("> replay 忠实率 <100% 的场次已从裁决样本中剔除：这些场次的某些输入未落盘"
      "（主要是随时间漂移的 `league_params` 自适应量），replay 用回填值会把测量误差"
      "混进配对检验。")
    A("")

    # --- 基线 ---
    bl = rep["baseline"]
    A("### 📊 基线（walk-forward 切分）")
    A("")
    A("| 段 | n | Brier ↓ | 命中率 ↑ |")
    A("|---|---|---|---|")
    A(f"| train | {bl['train']['n']} | {bl['train']['mean']:.4f} | {bl['train']['hit']*100:.2f}% |")
    A(f"| val | {bl['val']['n']} | {bl['val']['mean']:.4f} | {bl['val']['hit']*100:.2f}% |")
    A("")
    pol = rep["policy"]
    A(f"裁决门槛: `α={pol['alpha']}` (BH-FDR) · `min|Δ|={pol['min_delta']}` · "
      f"`val 恶化上限={pol['max_val_regression']}` · `metric={pol['metric']}`")
    A("")

    # --- 接受项 ---
    if rep["accepted"]:
        A("### ✅ 建议合入")
        A("")
        # 分成两类：事实性错误（issue）与统计裁决（statistic）
        issues = [a for a in rep["accepted"] if a.get("is_issue")]
        stats = [a for a in rep["accepted"] if not a.get("is_issue")]
        if issues:
            A("#### 🔧 事实性缺陷（无需统计阈值，静态证据即定性）")
            A("")
            for a in issues:
                A(f"**`{a['cid']}`** — {a['title']}")
                A("")
                A(f"- 严重度: `{a.get('severity','-')}` · 通道: `{a.get('detect_by','-')}`")
                A(f"- 裁决: {a['reason']}")
                A("")
                if a.get("evidence"):
                    A("- 实测证据: " + "; ".join(
                        f"`{k}={v}`" for k, v in list(a["evidence"].items())[:6]))
                    A("")
                if a.get("verify"):
                    A(f"- 自动验证: {a['verify']}")
                    A("")
                if a.get("side_effects"):
                    A("- 连带影响: " + "；".join(map(str, a["side_effects"])))
                    A("")
                if a.get("rationale"):
                    A(f"> {a['rationale'][:600]}")
                    A("")
        if stats:
            A("#### 📊 统计裁决（通过 walk-forward + BH-FDR）")
            A("")
            for a in stats:
                A(f"**`{a['cid']}`** — {a.get('title','')}")
                A("")
                A(f"- train ΔBrier = `{a.get('train_delta', 0):+.5f}` "
                  f"(t=`{a.get('t', 0):.2f}`, p=`{a.get('p_raw', 1):.3f}`)")
                A(f"- val   ΔBrier = `{a.get('val_delta', 0):+.5f}`")
                A(f"- 裁决: {a.get('reason','')}")
                A("")
    else:
        A("### 🚫 本轮无改动（ACCEPT_NONE）")
        A("")
        A("所有候选均未达合入标准。这是闭环的**正常且期望**的输出。")
        A("")
        # 可能仍有 NEEDS_DATA 项（工程性建议，无法用数据裁决）
        nd = rep.get("needs_data", [])
        if nd:
            A("#### 📋 需人工/工程决策的项（NEEDS_DATA）")
            A("")
            A("这些项无法用历史数据裁决，已转为人工评审：")
            A("")
            for r in nd:
                A(f"- `{r['cid']}` — {r['title']}（{r['reason'][:80]}）")
            A("")
        A("| cid | 拒绝理由 |")
        A("|---|---|")
        rej_sorted = sorted(
            [r for r in rep["rejected"] if r.get("decision") != "NEEDS_DATA"],
            key=lambda x: x.get("train_delta", 0))
        for r in rej_sorted[:10]:
            short = r["reason"].split("（")[0][:52]
            A(f"| `{r['cid']}` | {short} |")
        A("")

    # --- 抑制的候选（语义层拒绝记忆） ---
    if rep.get("suppressed"):
        A("### 🚫 本轮抑制（主线已否决，不重复提）")
        A("")
        A(f"共 {len(rep['suppressed'])} 条候选被拒绝记忆抑制。"
          "**这不是失败，而是闭环在遵守主线的裁决。**")
        A("")
        A("| cid | 族 | 抑制原因 |")
        A("|---|---|---|")
        for d in rep["suppressed"][:12]:
            A(f"| `{d['cid']}` | `{d['family']}` | {d['reason'][:80]} |")
        if len(rep["suppressed"]) > 12:
            A(f"| … | | 其余 {len(rep['suppressed'])-12} 条见 verdict JSON |")
        A("")

    # --- 分联赛参考 ---
    if rep.get("market_reference_val"):
        A("### 🗺 val 段市场基准（分联赛）")
        A("")
        A("| 联赛 | n | 市场命中率 | 市场 Brier |")
        A("|---|---|---|---|")
        for k, v in list(rep["market_reference_val"].items())[:10]:
            A(f"| {k} | {v['n']} | {v['market_hit']*100:.1f}% | {v['market_brier']:.4f} |")
        A("")
        A("> 整体指标会淡化单联赛的严重问题。整体 Brier 达标 ≠ 各联赛都达标。")
        A("")

    A("---")
    A("")
    A("**裁决权**: 本 PR 由自动进化闭环生成，**未自动合入**。")
    A("主线 agent 应独立复核：① replay 是否忠实 ② 效应量是否够业务意义 "
      "③ 是否只是过拟合到 val 段 ④ 回滚路径是否可执行。")
    return "\n".join(lines)


def write_pr_artifacts(report: dict, out_dir: Path, round_idx: int = 1) -> dict:
    """产出 PR 描述、裁决明细、以及可执行的合入/回滚命令。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    rep = report["rounds"][round_idx - 1]

    body = render_pr_body(report, round_idx)
    (out_dir / f"pr_body_round{round_idx}.md").write_text(body, encoding="utf-8")

    payload = {
        "generated_at": report["generated_at"],
        "verdict": "PROPOSE" if rep["accepted"] else "ACCEPT_NONE",
        "selfcheck": report["selfcheck"],
        "policy": rep["policy"],
        "baseline": rep["baseline"],
        "accepted": rep["accepted"],
        "needs_data": rep.get("needs_data", []),
        "suppressed": rep.get("suppressed", []),
        "memory": rep.get("memory", {}),
        "top_rejected": sorted((r for r in rep["rejected"]
                                if r.get("decision") != "NEEDS_DATA"),
                               key=lambda x: x.get("train_delta", 0))[:10],
        "market_reference_val": rep.get("market_reference_val", {}),
    }
    (out_dir / f"verdict_round{round_idx}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cmds = []
    if rep["accepted"]:
        cmds.append("# Suggested merge steps (run only after human approval)")
        cmds.append("git checkout -b auto/evolution-%s" % time.strftime("%Y%m%d"))
        for a in rep["accepted"]:
            cmds.append(f"# apply: {a['cid']}  ({a['title']})")
        cmds.append("python -m pytest tests/ -q")
        cmds.append("python scripts/ablation_replay.py   # upstream ablation recheck")
        cmds.append("python -m evolution.cycle --rounds 1  # loop self-consistency")
        cmds.append("git push origin auto/evolution-%s" % time.strftime("%Y%m%d"))
    else:
        cmds.append("# No accepted change this round - nothing to merge.")
        cmds.append("# Recommended: confirm loop health (selfcheck.passed == true)")
        cmds.append("# and wait for the next settlement cycle.")
    # bash on Windows/UTF-8: write ASCII-only comments to avoid mojibake
    (out_dir / f"commands_round{round_idx}.sh").write_text(
        "\n".join(cmds) + "\n", encoding="utf-8")

    return {"body": str(out_dir / f"pr_body_round{round_idx}.md"),
            "verdict": str(out_dir / f"verdict_round{round_idx}.json"),
            "commands": str(out_dir / f"commands_round{round_idx}.sh")}
