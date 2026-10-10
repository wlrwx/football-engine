"""进化循环入口：提出 → 自检 → 裁决 → 出 PR 素材。

用法（GitHub Actions 每日运行）:
    python -m evolution.cycle --rounds 1

设计约束：
  - 幂等：同一 ledger 快照 + 同一种子 → 同一报告。
  - 无外部依赖：纯标准库，Actions 上零安装。
  - 永不自动合入：本模块只产出裁决与 PR 素材，合入由主线 agent 决定。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .harness import evaluator as E
from .harness import fusion_core as S
from .harness import proposer as P
from .harness import verifier as V
from .harness import invariants as IV
from .harness import pred_inputs as PI
from .harness import model_backtest as MB
from .harness import rejection_memory as RM
from .harness import diagnostics as DG
from .harness import diagnostic_candidates as DC
from . import pr_writer


def build_ctx(ledger: list[dict], cfg: dict, base_dir: Path) -> dict:
    """为 verifier.judge_all 准备裁决上下文。

    包含 known_issues 各通道所需的东西：
      - invariants: 结构不变式结果
      - backtest_rows: 模型层 walk-forward（供 code_fix 类 issue 裁决）
    """
    ctx: dict = {}
    # 不变式（只依赖已在内存中的评分/联赛参数/账本）
    try:
        ratings = json.loads((base_dir / "data" / "models" / "team_ratings.json")
                              .read_text(encoding="utf-8"))
    except Exception:
        ratings = None
    try:
        lp = json.loads((base_dir / "data" / "league_params.json")
                         .read_text(encoding="utf-8"))
    except Exception:
        lp = None
    inv = IV.run_all(ratings=ratings, league_params=lp, ledger=ledger)
    # 补充 synthetic-odds 标记：从 daily/*/predictions.json 合并进来
    # （账本本身不存 odds_synthetic，但 invariant 需要它判断“合成赔率场次是否丢失市场镂”）
    try:
        pin = PI.load_prediction_inputs(str(base_dir / "data" / "daily"))
        for r in ledger:
            p = pin.get(r.get("match_id"))
            if p:
                r["_odds_synthetic"] = bool(p.get("odds_synthetic"))
    except Exception:
        pass
    inv = IV.run_all(ratings=ratings, league_params=lp, ledger=ledger)
    ctx["invariants"] = inv

    # 模型层 backtest（若历史数据存在）
    odds_path = base_dir / "data" / "historical" / "odds.csv"
    if odds_path.exists():
        try:
            rows = MB.load_odds_csv(str(odds_path))
            ctx["backtest_rows"] = MB.walk_forward_eval(
                rows, "2024-08-01",
                {"dc_buggy_linear": {"log_form": False},
                 "fixed_log_form": {"log_form": True}})
        except Exception as ex:  # noqa: BLE001
            ctx["backtest_error"] = str(ex)
    return ctx


def selfcheck(system, ledger, cfg, post, tol=0.01):
    """自检：replay 忠实性 + 基线合理性。

    自检不通过时不应产出任何 PR —— 在错误的测量基础上做自进化，
    比不自进化更危险。
    """
    out = {
        "ledger_rows": len(ledger),
        "has_final_prob": sum(1 for r in ledger if r.get("final_prob")),
        "has_market": sum(1 for r in ledger if r["_has_market"]),
        "chains": {},
    }
    chains = {}
    for r in ledger:
        chains[r.get("chain", "?")] = chains.get(r.get("chain", "?"), 0) + 1
    out["chains"] = chains

    flags = E.replay_trusted(system, ledger, cfg, post, tol=tol)
    out["replay_trusted_n"] = sum(flags)
    out["replay_trusted_frac"] = round(sum(flags) / max(1, len(flags)), 4)

    trusted = E.filter_trusted(ledger, flags)
    res = E.evaluate(system, trusted, cfg, post, "brier")
    out["baseline_brier"] = round(res["mean"], 6)
    out["baseline_hit"] = round(res["hit"], 4)

    # 护栏：Brier 应落在合理区间。超出说明 replay 或账本有严重问题
    ok = 0.3 < res["mean"] < 0.9 and 0.2 < res["hit"] < 0.9
    out["passed"] = ok
    out["fail_reason"] = "" if ok else (
        f"baseline out of range: brier={res['mean']:.4f} hit={res['hit']:.4f}")
    return out, trusted


def run_cycle(ledger_path: Path, cfg_path: Path, out_dir: Path,
              rounds: int = 1, max_candidates: int = 60,
              seed: int = 20261009, memory_path: Path | None = None,
              rejection_file: Path | None = None,
              repo_path: Path | None = None,
              repo_ref: str = "main") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = E.load_ledger(ledger_path)
    full_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg = full_cfg["fusion"]
    post = cfg["post_fusion"]

    chain = "v2"
    scoped = E.scope_current_chain(raw, chain=chain)
    scoped_market = [r for r in scoped if r["_has_market"]]

    check, trusted = selfcheck(S, scoped_market, cfg, post)
    print("[selfcheck]", json.dumps(check, ensure_ascii=False, indent=2))

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": seed,
        "selfcheck": check,
        "rounds": [],
    }

    if not check["passed"]:
        report["verdict"] = "ABORT_SELFCHECK_FAILED"
        (out_dir / "evolution_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[abort] selfcheck failed — no PR will be produced")
        return report

    base_cfg, base_post = cfg, post
    tried: list[str] = []
    ctx = build_ctx(trusted, cfg, ledger_path.parent if ledger_path.parent.name != "data"
                    else ledger_path.parent.parent)
    # 补足 ctx 所需的 base_dir（数据根目录）
    ctx.setdefault("_base_dir", str(ledger_path.parent))
    # 点亮「自动修复」通道：传入真实仓库路径（可选）。
    # 不传则闭环只裁决配置/权重类，代码缺陷仍走 backtest/invariant 通道。
    if repo_path is not None:
        ctx["repo_path"] = str(repo_path)
        ctx["repo_ref"] = repo_ref
        ctx["out_dir"] = str(out_dir)
        print(f"[auto-fix] 已点亮代码级自动修复通道：repo={repo_path} ref={repo_ref}")

    # ---- 拒绝记忆：载入主线 agent 的历史裁决 ----
    memory_path = memory_path or (out_dir / "rejection_memory.json")
    memory = RM.RejectionMemory(memory_path)
    # 本次运行要记录的主线否决（由 mainline 写好 rejection_file 传进来）
    # 接受两种格式：裸数组 [...] 或带包裹的 {"rejections": [...]}
    pending = []
    if rejection_file and Path(rejection_file).exists():
        raw = json.loads(Path(rejection_file).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            pending = raw.get("rejections", [])
        else:
            pending = raw
    all_cands_for_pending = P.generate(base_cfg, base_post, max_candidates=999,
                                       seed=seed)
    by_cid = {c.cid: c for c in all_cands_for_pending}
    for rec in pending:
        c = by_cid.get(rec.get("cid"))
        if c is not None:
            memory.reject(c, rec.get("reason", ""),
                          decided_by=rec.get("decided_by", "mainline"),
                          suppress_family=bool(rec.get("suppress_family")),
                          ttl_rounds=rec.get("ttl_rounds"))
            print(f"[rejection] 记录否决 {rec.get('cid')}: {rec.get('reason','')[:70]}")
        else:
            # 候选已不在当前池中（如代码改动后该候选消失）—— 仍记录指纹抑制，
            # 否则主线否决过的语义修复会在候选池变化后重新冒出。
            fp = rec.get("fingerprint")
            if fp:
                memory.entries.append({
                    "fingerprint": fp, "cid": rec.get("cid"),
                    "family": rec.get("family", "?"), "title": rec.get("cid", "")[:120],
                    "reason": rec.get("reason", "")[:400],
                    "decided_by": rec.get("decided_by", "mainline"),
                    "round": memory._round,
                    "date": time.strftime("%Y-%m-%d", time.gmtime()),
                    "ttl_rounds": rec.get("ttl_rounds") or memory.ttl_rounds,
                    "suppress_family": bool(rec.get("suppress_family")),
                })
                memory.save()
            else:
                print(f"[rejection] 未找到候选 {rec.get('cid')}，且无 fingerprint —— 跳过")

    for r in range(rounds):
        memory.start_round(r)
        cands_all = P.generate(base_cfg, base_post, max_candidates=max_candidates,
                               seed=seed, round_idx=r, previous=tried)
        # ---- 动态诊断：从当前账本里主动挖出新问题，转成候选 -------
        # 这是「自己发现问题」的来源，与 known_issues 静态清单互补。
        # 诊断用**全量 v2 链**（scoped），不用 trusted：
        #   - 裁决要 trusted（replay 忠实），否则把 replay 误差当改动效果
        #   - 诊断要看既成事实（draw 钉死/概率非法/xG 漂移都是生产落盘值），
        #     replay 是否忠实与「它是不是真的发生了」无关。
        #     用 trusted 会把巴甲 20 场 replay 失配样本排除 → 恰好把
        #     「巴甲 draw=0.5025 钉死」这个最严重的问题藏起来。
        # 诊断失败不阻塞闭环，但会显式打印，避免「没发现」被误读为「健康」。
        try:
            pin = PI.load_prediction_inputs(ctx.get("_base_dir", "") + r"\data\daily")
        except Exception:
            pin = None
        diag = DG.run_all(scoped, pin)
        diag_cands = DC.diagnostics_to_candidates(diag, previous=tried)
        if diag_cands:
            cands_all = diag_cands + cands_all
        report.setdefault("diagnostics", []).append(DG.summary(diag))
        # 语义层抑制：主线否决过的（含指纹/同族/同 issue）不再提
        cands, dropped = RM.apply_suppression(cands_all, memory)
        tried.extend(c.cid for c in cands)
        rep = V.judge_all(cands, base_cfg, base_post, trusted, ctx=ctx)
        rep["round"] = r + 1
        rep["n_candidates"] = len(cands)
        rep["suppressed"] = dropped
        rep["memory"] = memory.summary()
        report["rounds"].append(rep)
        print(f"\n[round {r+1}] candidates={len(cands)} "
              f"(suppressed {len(dropped)}) {rep['counts']}")
        for a in rep["accepted"]:
            print(f"   ACCEPT {a.get('cid')}  {(a.get('reason') or '')[:90]}")

        # 下一轮的基线 = 本轮已合入的改动（若上线 agent 确认）
        for a in rep["accepted"]:
            c = next((x for x in cands if getattr(x, "cid", None) == a.get("cid")), None)
            if c is not None and not c.is_issue:
                base_cfg, base_post = V.apply_candidate(c, base_cfg, base_post)

    total_acc = sum(len(r["accepted"]) for r in report["rounds"])
    report["verdict"] = "PROPOSE" if total_acc else "ACCEPT_NONE"

    artifacts = []
    for i in range(len(report["rounds"])):
        artifacts.append(pr_writer.write_pr_artifacts(report, out_dir, i + 1))
    report["artifacts"] = artifacts

    (out_dir / "evolution_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[verdict] {report['verdict']}  (accepted {total_acc})")
    for a in artifacts:
        print(f"[artifact] {a['body']}")
        print(f"[artifact] {a['verdict']}")
    print(f"[saved]   {out_dir / 'evolution_report.json'}")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="evolution/demo/data/review_ledger.jsonl")
    ap.add_argument("--config", default="evolution/demo/base_prediction.json")
    ap.add_argument("--out", default="evolution/demo/out")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--max-candidates", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--memory", default=None,
                    help="拒绝记忆文件路径（默认 <out>/rejection_memory.json）")
    ap.add_argument("--rejection-file", default=None,
                    help="主线 agent 写的否决清单 JSON，格式 [{cid,reason,suppress_family}]")
    ap.add_argument("--repo", default=None,
                    help="真实仓库路径。传入则点亮代码级自动修复通道（建分支+commit，不 push）")
    ap.add_argument("--repo-ref", default="main",
                    help="打补丁的基准 ref（默认 main）")
    a = ap.parse_args()
    run_cycle(Path(a.ledger), Path(a.config), Path(a.out),
              rounds=a.rounds, max_candidates=a.max_candidates, seed=a.seed,
              memory_path=Path(a.memory) if a.memory else None,
              rejection_file=Path(a.rejection_file) if a.rejection_file else None,
              repo_path=Path(a.repo) if a.repo else None,
              repo_ref=a.repo_ref)


if __name__ == "__main__":
    main()
