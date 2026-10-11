"""C2：自动 PR 必须附反事实对照（不打补丁 vs 打补丁，差值 + bootstrap CI + n）。

【为什么这是硬闸门而不是建议】
一个自动补丁要证明自己有用，只说"验证通过"是不够的 —— 它验证的是
**语义**（bug 确实被修掉了），不是**效果**（打完这个补丁指标有没有变好、
变了多少、是不是噪声）。语义正确但效果为零甚至为负的补丁非常常见。

更危险的情况是循环体会朝"指标好看"的方向进化：只要没有对照，
"改点东西让数字动一下"和"真的改好了"在 PR 描述里长得一模一样。
所以这里做成 **enforcement**：没有反事实的 autofix 不许开 PR，
由 push_and_pr 调用 require_counterfactual() 强制。

【为什么用 bootstrap CI 而不是只给 t 值】
t 值回答"均值差不为零吗"，但不给"差多少、范围多宽"。
自动 PR 的读者要判断的是"这个提升值不值得合"，需要的是区间。
而且 t 值对离群点极敏感 —— 实测 E2 里亚盘 OLS 斜率 t=+2.97 看着显著，
十分位 ROI 却全为负且非单调，斜率几乎全由离群尾巴撑着。CI 不会骗人。

【纯标准库】
闭环必须能在任意 Actions runner 上跑（项目 test.yml 里就写着
`pip install ... || echo 继续`），所以不引入 numpy/scipy。
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path


class CounterfactualMissing(Exception):
    """autofix 缺少反事实对照 —— 不允许开 PR。"""


def paired_counterfactual(cand: list[dict], base: list[dict],
                          metric: str = "brier", n_boot: int = 2000,
                          seed: int = 20261011) -> dict:
    """配对反事实：候选 vs 基线，返回 差值 + bootstrap CI + n。

    输入是 evaluate()/_summarize() 产出的 per_match 列表，每项需含
    match_id / metric / has_market。只配对 has_market 相同的样本，
    与 paired_test 的口径一致，避免两处统计打架。

    seed 固定 → 结果可复现。bootstrap CI 是 percentile 法。
    """
    by_id = {r["match_id"]: r for r in base}
    diffs = []
    for r in cand:
        o = by_id.get(r["match_id"])
        if o is None or bool(o.get("has_market")) != bool(r.get("has_market")):
            continue
        diffs.append(r["metric"] - o["metric"])
    n = len(diffs)
    if n < 3:
        return {"ok": False, "n": n,
                "reason": f"配对样本仅 {n} 条，不足以做反事实（需 ≥3）"}

    base_vals = [by_id[r["match_id"]]["metric"] for r in cand
                 if r["match_id"] in by_id
                 and bool(by_id[r["match_id"]].get("has_market")) == bool(r.get("has_market"))]
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    sd = math.sqrt(var)

    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += diffs[rng.randrange(n)]
        boots.append(s / n)
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[min(n_boot - 1, int(0.975 * n_boot))]

    t = (mean / (sd / math.sqrt(n))) if sd > 0 else 0.0
    return {
        "ok": True,
        "metric": metric,
        "n": n,
        "n_boot": n_boot,
        "seed": seed,
        "base_mean": sum(base_vals) / len(base_vals),
        "cand_mean": sum(diffs) / n + sum(base_vals) / len(base_vals),
        "delta": mean,
        "ci_low": lo,
        "ci_high": hi,
        "sd": sd,
        "t": t,
        "better_rate": sum(1 for d in diffs if d < 0) / n,
        "significant": bool(lo > 0 or hi < 0),
        "note": "delta = cand - base，负值表示候选更好（brier/logloss 越小越好）",
    }


def render_counterfactual_md(cf: dict) -> str:
    """渲染成 PR 描述里的 markdown 块。"""
    if not cf.get("ok"):
        return (f"### 反事实对照\n\n❌ 缺失：{cf.get('reason', '未知原因')}\n\n"
                f"按 C2 规定，自动 PR 必须附「不打补丁 vs 打补丁」的配对对照。\n")
    ci = "跨 0（不显著）" if not cf["significant"] else (
        "全区间 < 0（显著更好）" if cf["ci_high"] < 0 else "全区间 > 0（显著更差）")
    return (
        f"### 反事实对照（不打补丁 vs 打补丁）\n\n"
        f"| 项 | 值 |\n|---|---|\n"
        f"| 指标 | `{cf['metric']}` |\n"
        f"| 配对样本 n | **{cf['n']}** |\n"
        f"| 不打补丁 | {cf['base_mean']:.6f} |\n"
        f"| 打了补丁 | {cf['cand_mean']:.6f} |\n"
        f"| 差值 delta | **{cf['delta']:+.6f}** |\n"
        f"| bootstrap 95% CI | [{cf['ci_low']:+.6f}, {cf['ci_high']:+.6f}]（{ci}） |\n"
        f"| t | {cf['t']:+.2f} |\n"
        f"| 更优比例 | {cf['better_rate']*100:.1f}% |\n\n"
        f"> {cf['note']}。bootstrap {cf['n_boot']} 次，seed={cf['seed']}（可复现）。\n"
    )


def require_counterfactual(meta: dict) -> dict:
    """C2 硬闸门：autofix 元数据里没有合规反事实就抛错。

    由 push_and_pr 在开 PR 前调用。宁可不开 PR，也不要开一个
    只说"验证通过"却没有任何对照的自动补丁。
    """
    cf = meta.get("counterfactual")
    if not isinstance(cf, dict):
        raise CounterfactualMissing(
            f"autofix {meta.get('issue')} 没有 counterfactual 字段。"
            f"C2 规定：自动 PR 必须附「不打补丁 vs 打补丁」的差值 + bootstrap CI + n。"
            f"语义验证通过不等于效果为正 —— 两者必须分开报告。")
    if not cf.get("ok"):
        raise CounterfactualMissing(
            f"autofix {meta.get('issue')} 的反事实对照不可用：{cf.get('reason')}")
    for k in ("n", "delta", "ci_low", "ci_high"):
        if k not in cf:
            raise CounterfactualMissing(
                f"autofix {meta.get('issue')} 的反事实缺少字段 {k}。"
                f"必须同时给出 n / delta / CI 两端。")
    if not isinstance(cf["n"], int) or cf["n"] < 3:
        raise CounterfactualMissing(
            f"autofix {meta.get('issue')} 的反事实 n={cf.get('n')} 太小（需 ≥3）。")
    return cf


def load_and_require(out_dir: Path) -> list[dict]:
    """读取 autofix_*.json 并逐个过 C2 闸门，返回通过的。"""
    ok, rejected = [], []
    for p in sorted(Path(out_dir).glob("autofix_*.json")):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception as ex:  # noqa: BLE001
            rejected.append((p.name, f"JSON 解析失败: {ex}"))
            continue
        if meta.get("@type") != "autofix":
            continue
        try:
            require_counterfactual(meta)
            ok.append(meta)
        except CounterfactualMissing as ex:
            rejected.append((p.name, str(ex)))
    return ok, rejected
