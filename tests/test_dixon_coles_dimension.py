"""回归测试：Dixon-Coles 攻防项量纲与符号（2026-10-09 修复）。\n\n背景：team_ratings.json 的 attack/defense 是以 1.0 为中心的比例因子，\n必须用 log() 进入对数域，且防守好（defense<1）应压低对手 xG。\n此前实现直接把比例因子相加并取负号，导致量纲错 + 符号反，\nDC 总进球均值被系统性高估到 3.22（五大联赛真实约 2.8）。\n"""

from engine.prediction.dixon_coles import DixonColesModel
from engine.prediction.base import TeamRating


def _home(attack=1.0, defense=1.0, elo=1500.0, form=0.0):
    return TeamRating(name="H", elo=elo, attack=attack, defense=defense,
                      form=form, injury=0.0, rest_days=3)


def _away(attack=1.0, defense=1.0, elo=1500.0, form=0.0):
    return TeamRating(name="A", elo=elo, attack=attack, defense=defense,
                      form=form, injury=0.0, rest_days=3)


def test_defense_sign_direction():
    """防守好（defense 值小）必须压低对手 xG，而非抬高。"""
    m = DixonColesModel()
    good_def = _away(defense=0.7)
    bad_def = _away(defense=1.4)
    xg_vs_good, _ = m._expected_goals(_home(), good_def, False)
    xg_vs_bad, _ = m._expected_goals(_home(), bad_def, False)
    assert xg_vs_good < xg_vs_bad, (
        f"防守方向反了：defense 0.7→{xg_vs_good:.3f} 应 < defense 1.4→{xg_vs_bad:.3f}"
    )


def test_attack_monotonic():
    """进攻强（attack 值大）的主队 xG 必须更高。"""
    m = DixonColesModel()
    weak = m._expected_goals(_home(attack=0.6), _away(), False)[0]
    strong = m._expected_goals(_home(attack=1.6), _away(), False)[0]
    assert weak < strong, f"attack 0.6→{weak:.3f} 应 < 1.6→{strong:.3f}"


def test_total_goals_not_systematically_inflated():
    """修复后，典型强弱对总进球不应被无脑抬到 3+。"""
    m = DixonColesModel()
    # 均衡场：双方各 attack=1.0, defense=1.0, elo 相同
    hx, ax = m._expected_goals(_home(), _away(), False)
    assert hx + ax < 3.0, f"均衡场总进球 {hx+ax:.2f} 不应 >3.0（此前被高估）"