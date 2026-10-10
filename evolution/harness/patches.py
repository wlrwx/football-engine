"""已知代码缺陷的**可执行修复补丁**。

与 known_issues.py 的分工：
  - known_issues  = 问题是什么、严重度多少、证据是什么（诊断）
  - 本文件        = 怎么修（精确到行的文本替换）+ 怎么验证（真实源码执行）

闭环流程：
  known_issues → patches.py 取出 Patch → patch_engine.apply 在真实源码上应用
  → patch_engine.run_extracted 在真实源码上执行验证 → 通过才产出 PR

【关键约束：验证必须在「修复前」的源码上先失败】
若一个补丁在已经被修好的源码上也能"验证通过"，那验证就是自证的。
因此 test_* 函数分成两类：
  - fails_before: 断言 bug 存在（在原始源码上必须失败）
  - passes_after: 断言 bug 修好（打补丁后必须通过）
只有 fails_before 先失败、passes_after 后通过，才算真的修好了。
"""

from __future__ import annotations

from .patch_engine import (Edit, Patch, compiles, extract_class_methods,
                           run_extracted)


# --------------------------------------------------------------- 桩件

_TEAM_STUB = '''
import math
class TeamRating:
    def __init__(self, name="X", elo=1500.0, attack=1.0, defense=1.0,
                 form=0.0, injury=0.0, rest_days=3):
        self.name=name; self.elo=elo; self.attack=attack; self.defense=defense
        self.form=form; self.injury=injury; self.rest_days=rest_days

class _Cfg:
    base_goals=1.35; elo_goal_weight=0.62
    attack_weight=1.0; defense_weight=0.9; form_weight=0.65
    injury_weight=1.0; rest_weight=0.035; home_adv_weight=1.0

class _Self:
    def __init__(self, cfg): self.cfg=cfg
'''

# DC 修复前应失败的断言：防守方向反了
_DC_BODY_BEFORE = '''
_h = TeamRating(elo=1500, attack=1.0, defense=1.0)
_good = TeamRating(elo=1500, attack=1.0, defense=0.7)   # 防守好
_bad  = TeamRating(elo=1500, attack=1.0, defense=1.4)   # 防守差
_m = _Self(_Cfg())
x_good, _ = _expected_goals(_m, _h, _good, False)
x_bad,  _ = _expected_goals(_m, _h, _bad,  False)
_RESULT = (x_good < x_bad,
           f"defense 0.7->{x_good:.3f} vs 1.4->{x_bad:.3f}")
'''

# 修复后应通过
_DC_BODY_AFTER = '''
_h = TeamRating(elo=1500, attack=1.0, defense=1.0)
_good = TeamRating(elo=1500, attack=1.0, defense=0.7)
_bad  = TeamRating(elo=1500, attack=1.0, defense=1.4)
_m = _Self(_Cfg())
x_good, _ = _expected_goals(_m, _h, _good, False)
x_bad,  _ = _expected_goals(_m, _h, _bad,  False)
ok1 = x_good < x_bad          # 防守方向正确
xw, _ = _expected_goals(_m, TeamRating(attack=0.6), TeamRating(), False)
xs, _ = _expected_goals(_m, TeamRating(attack=1.6), TeamRating(), False)
ok2 = xw < xs                 # 进攻单调递增
xb, ya = _expected_goals(_m, TeamRating(), TeamRating(), False)
ok3 = (xb + ya) < 3.0          # 均衡场不应被抬到 3+ 球
_RESULT = (ok1 and ok2 and ok3,
           f"defense方向={ok1} attack单调={ok2} 均衡总进球={xb+ya:.3f}<3.0={ok3}")
'''


def verify_dc_before(src: str, path: str) -> tuple[bool, str]:
    """修复前：bug 必须存在（即 ok 应为 False）。返回 (bug_present, detail)"""
    ok, detail, _ = run_extracted(
        src, "DixonColesModel", ["_expected_goals"],
        _TEAM_STUB, _DC_BODY_BEFORE)
    if not detail:
        return False, "无法验证（提取/执行失败）—— 不能假定 bug 存在"
    # 这里 ok 是「方向正确」；若 ok=False 说明 bug 存在（符合预期）
    return (not ok), f"原始行为: {detail}"


def verify_dc_after(src: str, path: str) -> tuple[bool, str]:
    """修复后：断言必须全过。"""
    ok, detail, _ = run_extracted(
        src, "DixonColesModel", ["_expected_goals"],
        _TEAM_STUB, _DC_BODY_AFTER)
    if not detail:
        return False, "验证执行失败"
    return ok, detail


# --------------------------------------------------------------- 补丁定义

DC_LOG_DIMENSION = Patch(
    issue_id="DC-ATTACK-LOG-DIMENSION",
    target="engine/prediction/dixon_coles.py",
    edits=[
        Edit(
            old="""        # Elo 差项
        elo_term = (home.elo - away.elo) / 400 * cfg.elo_goal_weight

        # 主队期望进球
        log_home = (
            base
            + elo_term * 0.5
            + home.attack * cfg.attack_weight
            - away.defense * cfg.defense_weight""",
            new="""        # Elo 差项
        elo_term = (home.elo - away.elo) / 400 * cfg.elo_goal_weight

        # attack/defense 是以 1.0 为中心的比例因子（elo_updater 里
        # attack≈实际进球/1.35），进入对数域必须以 log() 进入；且防守好
        # （defense<1）意味着对手 xG 更低，相乘转对数即加法、符号为正。
        # 修复前：+home.attack*w - away.defense*w —— 量纲错 + 符号反。
        home_atk = math.log(max(0.3, home.attack)) * cfg.attack_weight
        away_def = math.log(max(0.3, away.defense)) * cfg.defense_weight
        away_atk = math.log(max(0.3, away.attack)) * cfg.attack_weight
        home_def = math.log(max(0.3, home.defense)) * cfg.defense_weight

        # 主队期望进球
        log_home = (
            base
            + elo_term * 0.5
            + home_atk
            + away_def""",
            why="attack/defense 比例因子改用 log 进入对数域，防守符号纠正",
        ),
        Edit(
            old="""        log_away = (
            base
            - elo_term * 0.5
            + away.attack * cfg.attack_weight
            - home.defense * cfg.defense_weight""",
            new="""        log_away = (
            base
            - elo_term * 0.5
            + away_atk
            + home_def""",
            why="客队侧同样修正",
        ),
    ],
    reverse_note="git revert 即可；无数据迁移",
    verify=verify_dc_after,
)


PATCHES: dict[str, Patch] = {
    DC_LOG_DIMENSION.issue_id: DC_LOG_DIMENSION,
}


def get(issue_id: str) -> Patch | None:
    return PATCHES.get(issue_id)
