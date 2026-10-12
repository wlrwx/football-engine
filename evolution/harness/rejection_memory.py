"""语义层拒绝记忆 —— 让闭环不会反复提同一个被否决的改动。

【为什么需要】
原先闭环只做 cid 去重（"这轮提过了，下轮不提"）。但主线 agent 的否决是
**跨轮、跨 run** 的：
    - run 1：闭环提 `w_m0.10_k0.75` → 主线否决（理由：样本不足）
    - run 2（次日）：闭环又提 `w_m0.10_k0.75` → 主线再否决
    - ...无限循环，每次都消耗主线 agent 的裁决带宽

更糟的情况：主线否决的是**一类改动**而非单个 cid。例如否决
「所有 xg_calibration 调整」，那闭环连其他 calibration 候选都不该再提。

【本模块的机制】
1. **指纹去重**：候选内容算 SHA256 指纹。指纹相同 = 同一个改动，
   无论 cid 怎么变（改了 seed 也会命中）。
2. **语义族否决**：主线可否决一个 `family`（如 `fusion_weight`）或一个
   `rationale` 关键词，闭环自动抑制同族候选。
3. **抑制原因可追溯**：每条否决记录 why + when + who，
   PR 里会显示「本轮抑制了 N 条候选，原因：...」，
   避免"静默不干活"——主线需要知道闭环为什么沉默。
4. **可过期**：否决不是永久的。新证据出现（如样本量翻倍）后，
   `ttl_rounds` 到期自动解禁。这防止否决把好改动永远锁死。

【数据格式】rejection_memory.json
{
  "version": 1,
  "rejections": [
    {"fingerprint": "sha256...", "cid": "w_m0.10_k0.75", "family": "fusion_weight",
     "title": "...", "reason": "样本量不足", "decided_by": "mainline",
     "round": 3, "date": "2026-10-09", "ttl_rounds": 10, "suppress_family": false}
  ]
}
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

VERSION = 1


def fingerprint(candidate) -> str:
    """候选内容指纹。

    只对**语义内容**取哈希，不含 cid / rationale 措辞 —— 否则同一改动换个
    说法就绕过了去重，那就没有意义了。
    """
    patch = getattr(candidate, "patch", {}) or {}
    parts = [getattr(candidate, "family", ""), json.dumps(patch, sort_keys=True,
                                                          ensure_ascii=False)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


class RejectionMemory:
    """跨轮拒绝记忆。"""

    def __init__(self, path: Path, ttl_rounds: int = 10):
        self.path = Path(path)
        self.ttl_rounds = ttl_rounds
        self.entries: list[dict] = []
        self._round = 0
        self.load()

    # ---------------------------------------------------------- 持久化
    def load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") == VERSION:
                self.entries = raw.get("rejections", [])
                self._round = raw.get("current_round", 0)
        except Exception:
            self.entries = []

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "version": VERSION,
            "current_round": self._round,
            "rejections": self.entries,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------- 记录
    def reject(self, candidate, reason: str, decided_by: str = "mainline",
               suppress_family: bool = False, ttl_rounds: int | None = None):
        """记录一条否决。"""
        fp = fingerprint(candidate)
        if any(e["fingerprint"] == fp for e in self.entries):
            return fp          # 幂等：同一条不重复记
        self.entries.append({
            "fingerprint": fp,
            "cid": getattr(candidate, "cid", "?"),
            "family": getattr(candidate, "family", "?"),
            "title": getattr(candidate, "title", "")[:120],
            "reason": reason[:400],
            "decided_by": decided_by,
            "round": self._round,
            "date": time.strftime("%Y-%m-%d", time.gmtime()),
            "ttl_rounds": self.ttl_rounds if ttl_rounds is None else ttl_rounds,
            "suppress_family": suppress_family,
        })
        self.save()
        return fp

    def start_round(self, r: int):
        self._round = r
        self.save()

    # ---------------------------------------------------------- 查询
    def is_expired(self, entry: dict) -> bool:
        ttl = entry.get("ttl_rounds")
        if ttl is None or ttl <= 0:
            return False
        return (self._round - entry.get("round", 0)) >= ttl

    def suppressed(self, candidate) -> tuple[bool, str]:
        """候选是否应被抑制？返回 (是否抑制, 原因)。"""
        fp = fingerprint(candidate)
        fam = getattr(candidate, "family", "")
        cid = getattr(candidate, "cid", "")
        for e in self.entries:
            if self.is_expired(e):
                continue
            if e["fingerprint"] == fp:
                return True, (f"指纹命中（{e['cid']}，{e['decided_by']} 于 "
                              f"round {e['round']} 否决：{e['reason'][:80]}）")
            if e.get("suppress_family") and e["family"] == fam:
                return True, (f"同族被否决（family={fam}，"
                              f"{e['decided_by']} 于 round {e['round']}："
                              f"{e['reason'][:80]}）")
            # issue 类按 cid 直接抑制（语义修复不该反复提）
            if fam.startswith("issue/") and e["cid"] == cid:
                return True, (f"该问题已被否决（{e['decided_by']} 于 "
                              f"round {e['round']}：{e['reason'][:80]}）")
        return False, ""

    def prune(self):
        """清掉过期条目。"""
        before = len(self.entries)
        self.entries = [e for e in self.entries if not self.is_expired(e)]
        if len(self.entries) != before:
            self.save()
        return before - len(self.entries)

    # ---------------------------------------------------------- 报告
    def summary(self) -> dict:
        by_family: dict[str, int] = {}
        for e in self.entries:
            by_family[e["family"]] = by_family.get(e["family"], 0) + 1
        return {
            "total": len(self.entries),
            "by_family": by_family,
            "current_round": self._round,
            "ttl_rounds": self.ttl_rounds,
        }


def apply_suppression(candidates, memory: RejectionMemory):
    """过滤候选，返回 (保留的, 被抑制的)。"""
    kept, dropped = [], []
    for c in candidates:
        hit, why = memory.suppressed(c)
        if hit:
            dropped.append({"cid": c.cid, "family": c.family, "reason": why})
        else:
            kept.append(c)
    return kept, dropped
