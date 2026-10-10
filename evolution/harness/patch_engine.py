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
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field


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
