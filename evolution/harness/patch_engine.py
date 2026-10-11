"""补丁引擎：把已知代码缺陷变成可应用、可验证的真实源码 patch。

【为什么需要这个模块】
原闭环只能把代码缺陷写成 `{"__code_fix__": "<issue_id>"}` 占位符 —— 它
**发现**了 bug，但**修不了**。所有源码修复都是我手动 git 改的，闭环本身
对源码零贡献。

本模块补上这一段：已知 issue 的修复方案以「精确文本替换」的形式声明在
KNOWN_ISSUES 中，闭环可以
  1. 在真实源码上应用补丁（apply）
  2. 提取被改函数并在无第三方依赖的环境里执行验证（verify）
  3. 补丁应用不上 / 验证不过 → 不产出 PR

【设计约束：为什么验证不靠 import 真实项目代码】
项目依赖 numpy/scipy/lightgbm，而闭环必须能在任意 Actions runner 上跑
（项目自己的 test.yml 就写了 `pip install ... || echo 继续`）。因此验证
采用「AST 提取 + 独立命名空间执行」：把被改函数连同它依赖的极小桩件
（TeamRating、config 属性访问）抽出来直接跑。不需要 numpy，且**验的是
真实源码文本**，不是我另写的一份副本 —— 这是它比原 harness 强的地方。

【安全护栏】
  - 只允许「精确文本替换」，不允许 AST 重写（后者太容易静默改变语义）
  - 每条替换必须唯一命中；命中 0 次或多次都判失败
  - 补丁后的文件必须 py_compile 通过
  - 绝不动 git：只改工作副本，commit 由 git_writer 显式调用
  - **白名单路径禁触**（2026-10-11 新增，见 FORBIDDEN / assert_patchable）
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field


# --------------------------------------------------------------- 路径白名单

# 闭环会朝"指标好看"的方向进化。若它能改指标本身，这个闭环就是个奖杯机：
# 把 brier 的计算改错、把账本改干净、把基线重算一遍，任何"改进"都能凭空造出来。
# 所以评估层的文件对自动补丁**永久禁触**，改它们只能走人工 review。
#
# 注意这里的条目刻意写成**仓库里的真实路径**而不是抽象概念名。workbuddy 的
# 原始表述是「禁触 metrics/ backtest/ holdout/ ledger/ expectations.json」，
# 但本仓库根本没有 metrics/ holdout/ ledger/ 这几个目录，也没有
# expectations.json —— 照字面实现会得到一把**锁住空集**的假锁，
# 看起来有防护，实际一条都拦不住。这里逐条映射到真实文件。
FORBIDDEN: tuple[str, ...] = (
    # backtest/ —— 评估与回放
    "engine/backtest/",
    # metrics/ —— 基线与校准状态（等价于「期望值」的存放处）
    "data/state/market_baseline.json",
    "data/state/football_data_baselines.json",
    "data/state/edge_calibration.json",
    "data/state/calibration_status.json",
    "engine/prediction/calibration.py",
    "engine/learning/calibration_auto.py",
    "engine/learning/score_calibration.py",
    # ledger/ —— 账本即事实来源，改账本等于改历史
    "data/state/review_ledger.jsonl",
    "data/state/ledger_reconcile.json",
)

# 被禁触的代码符号（文件不在上面，但改了等于绕过）
FORBIDDEN_SYMBOLS: tuple[str, ...] = (
    "def brier", "def log_loss", "def rps", "def paired_test",
    "def evaluate", "def _summarize", "def p_one_sided_from_t",
    "def benjamini_hochberg", "def load_ledger", "def filter_trusted",
    "def min_sample",
)


# ForbiddenTarget 继承 PatchError 的实现在文件后段（PatchError 定义处），
# 这样调用方 catch PatchError 就能同时兜住「补丁失败」和「补丁越权」。


def _norm(rel: str) -> str:
    return str(rel).replace("\\", "/").lstrip("./").lstrip("/")


def forbidden_reason(rel: str) -> str:
    """rel 命中白名单则返回原因，否则返回空串。

    同时覆盖：目录前缀（engine/backtest/）与精确文件（data/state/…json）。
    """
    r = _norm(rel)
    for f in FORBIDDEN:
        if f.endswith("/"):
            if r.startswith(f):
                return f"路径前缀 {f} 属评估层，自动补丁禁触"
        elif r == f:
            return f"文件 {f} 属评估层，自动补丁禁触"
    return ""


def forbidden_symbol_reason(src: str) -> str:
    """源码文本里若出现被保护的指标函数定义则返回原因，否则空串。

    文件不在 FORBIDDEN 名单内、但函数被改名搬走的情况由这道锁兜住。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""          # 语法错误由 compiles() 负责报，不在这里越权判死
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig = f"def {node.name}"
            for bad in FORBIDDEN_SYMBOLS:
                if sig == bad:
                    return (f"源码重定义了受保护的指标函数 {bad}()，"
                            f"自动补丁禁触（行 {node.lineno}）")
    return ""


def assert_patchable(rel: str, new_text: str | None = None) -> None:
    """补丁落盘前的总闸。命中任一白名单即抛 ForbiddenTarget。"""
    why = forbidden_reason(rel)
    if why:
        raise ForbiddenTarget(why)
    if new_text is not None:
        why = forbidden_symbol_reason(new_text)
        if why:
            raise ForbiddenTarget(f"{rel}: {why}")



# --------------------------------------------------------------- 补丁声明

@dataclass
class Edit:
    """一处精确文本替换。"""
    old: str
    new: str
    # 该替换的作用说明（写进 PR）
    why: str = ""

    def fingerprint(self) -> str:
        return hashlib.sha256(
            (self.old + "\x00" + self.new).encode("utf-8")).hexdigest()[:16]


@dataclass
class Patch:
    """一个可应用、可验证的源码补丁。"""
    issue_id: str
    target: str                 # 相对仓库根的路径，如 engine/prediction/fusion.py
    edits: list[Edit]
    # 验证函数：接收 patch 后的源码文本与文件路径，返回 (ok, detail)。
    # 必须在**真实源码文本**上执行（AST 提取 + 独立命名空间），
    # 不能用 harness 里的副本 —— 否则验的是另写的一份代码。
    verify: object = None
    reverse_note: str = ""
    _applied: list = None
    _skipped: list = None

    def fingerprint(self) -> str:
        parts = "|".join(e.fingerprint() for e in self.edits)
        return hashlib.sha256((self.target + "|" + parts).encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        return f"{self.issue_id} -> {self.target} ({len(self.edits)} 处编辑, fp={self.fingerprint()})"


# --------------------------------------------------------------- 应用

class PatchError(Exception):
    pass


class ForbiddenTarget(PatchError):
    """补丁试图改评估层 —— 自动闭环不得触碰，必须人工 review。"""


def apply_patch(src: str, patch: Patch) -> str:
    """把补丁应用到源码文本上。

    每处替换必须唯一命中。命中 0 次说明上游代码已变（可能已修复），
    命中多次说明 old 写得太宽松 —— 两者都必须失败，不能猜。
    """
    out = src
    applied, skipped = [], []
    for e in patch.edits:
        n = out.count(e.old)
        if n == 0:
            # 可能是已经打过补丁了
            if out.count(e.new) >= 1:
                skipped.append(("already_applied", e.old[:60]))
                continue
            raise PatchError(
                f"[{patch.issue_id}] 替换未命中（上游可能已变更）：\n"
                f"  old: {e.old[:120]!r}")
        if n > 1:
            raise PatchError(
                f"[{patch.issue_id}] 替换命中 {n} 次，拒绝模糊匹配：\n"
                f"  old: {e.old[:120]!r}")
        out = out.replace(e.old, e.new, 1)
        applied.append(e.old[:60])
    patch._applied = applied
    patch._skipped = skipped
    return out


def is_already_applied(src: str, patch: Patch) -> bool:
    """判断补丁是否已经全部生效（用于幂等：已修复的 bug 不重复提）。"""
    missing = 0
    for e in patch.edits:
        if e.old in src:
            missing += 1
    return missing == 0


# --------------------------------------------------------------- 验证

def compiles(src: str, label: str = "") -> tuple[bool, str]:
    try:
        compile(src, label or "<patched>", "exec")
        return True, ""
    except SyntaxError as ex:
        return False, f"语法错误 line {ex.lineno}: {ex.msg}"


def extract_class_methods(src: str, class_name: str,
                          methods: list[str]) -> str:
    """从源码里提取某个类的若干方法，去掉缩进并转成顶层函数。

    用途：把被改的私有方法（如 DixonColesModel._expected_goals）抽出来，
    在无第三方依赖的环境里直接执行验证。
    """
    tree = ast.parse(src)
    cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            cls = node
            break
    if cls is None:
        raise PatchError(f"找不到类 {class_name}")

    lines = src.split("\n")
    chunks, found = [], []
    for fn in cls.body:
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in methods:
            body = lines[fn.lineno - 1: fn.end_lineno]
            # 去掉一层缩进（类体缩进为 4）
            dedented = [ln[4:] if ln.startswith("    ") else ln for ln in body]
            chunks.append("\n".join(dedented))
            found.append(fn.name)
    missing = set(methods) - set(found)
    if missing:
        raise PatchError(f"{class_name} 缺少方法 {sorted(missing)}")
    return "\n\n".join(chunks)


def run_extracted(src: str, class_name: str, methods: list[str],
                  preamble: str, body: str) -> tuple[bool, str, dict]:
    """提取方法 → 在独立命名空间执行 preamble+methods+body → 返回结果。

    preamble 用来注入桩件（TeamRating、cfg 等）。这样验证的是**真实源码
    里的那个函数**，而不是 harness 里另写的副本。
    """
    try:
        extracted = extract_class_methods(src, class_name, methods)
    except (PatchError, SyntaxError) as ex:
        return False, f"提取失败: {ex}", {}

    ns: dict = {}
    code = preamble + "\n\n" + extracted + "\n\n" + body
    try:
        exec(compile(code, f"<{class_name}>", "exec"), ns)
    except Exception as ex:  # noqa: BLE001
        return False, f"执行失败: {type(ex).__name__}: {ex}", {}
    result = ns.get("_RESULT")
    if result is None:
        return False, "验证代码未设置 _RESULT", {}
    ok, detail = result
    return bool(ok), str(detail), ns


# --------------------------------------------------------------- diff 摘要

def unified_summary(src_old: str, src_new: str, path: str) -> str:
    """产出简洁 diff 摘要（写进 PR 正文）。"""
    import difflib
    diff = list(difflib.unified_diff(
        src_old.split("\n"), src_new.split("\n"),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=1))
    return "\n".join(diff)
