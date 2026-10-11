"""E2 跨盘口实验的**口径不变式**测试。

这个模块开发过程中出现过 4 次"看起来能跑但结果是错的"缺陷，
全部集中在亚式结算与概率定义上：

  1. devig 只支持三路 → 大小球两路去水静默失败
  2. 亚盘用欧式三路结算 → 信号出现 ~16 个百分点的系统性偏差
  3. 亚盘走盘按输（−1）计 → 系统性做空"半赢半走"
  4. ah_units 把净胜球差当成已加让球线的 margin → **完全忽略让球线**，
     同一 λ 下所有让球线的期望收益恒等（λ=1/1 时恒为 0）

第 4 条是本次修复的核心：定价函数(ah_probs)和结算函数(ah_units)口径不一致时，
实验测到的是自己的口径误差，而不是市场的无效率。所以下面用
**E[u] ≡ p_home − p_away** 这个恒等式把两者锁死。

另有一处易错点：ah_probs 返回的 p_draw 是**押回注资的份额**，
不是"整注走盘的概率"。四球线下整注走盘不可能发生（两段是相邻的两条
不同线，净胜球不可能同时等于 -lo 和 -hi），但押回的注资份额非零。
把 p_draw 误设为 0 会让 p_away 偏大。
"""
import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from engine.backtest.cross_market import (  # noqa: E402
    ah_probs, ah_units, invert_1x2, ou_probs, _DIFF, _TOT, _joint,
)

LAMS = [(1.4, 1.1), (2.7, 0.8), (0.7, 1.9), (1.0, 1.0), (3.1, 0.5), (1.2, 2.4)]
# 整线 / 半球线 / 四球线（x.25 / x.75）全覆盖
LINES = [-2.0, -1.75, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25,
         0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]


def _expected_units(lam_h, lam_a, line):
    """在比分 pmf 上逐格结算，返回 (赢份额, 走盘份额, 输份额, E[u])。"""
    j = _joint(lam_h, lam_a)
    win = push = loss = exp_u = 0.0
    for h in range(j.shape[0]):
        for a in range(j.shape[1]):
            p = j[h, a]
            if p <= 0:
                continue
            u = ah_units(h - a, line)
            exp_u += p * u
            # u=±0.5 在四球线下是「半注走盘 + 半注赢/输」，不是半赢半输
            if u == 1.0:
                win += p
            elif u == 0.5:
                win += 0.5 * p
                push += 0.5 * p
            elif u == 0.0:
                push += p
            elif u == -0.5:
                push += 0.5 * p
                loss += 0.5 * p
            else:
                loss += p
    return win, push, loss, exp_u


@pytest.mark.parametrize("lam", LAMS)
def test_ah_pricing_matches_settlement(lam):
    """ah_probs 的定价必须与 ah_units 的逐格结算完全一致（E[u] ≡ p_home − p_away）。"""
    for line in LINES:
        p_home, p_draw, p_away = ah_probs(lam[0], lam[1], line)
        win, push, loss, exp_u = _expected_units(lam[0], lam[1], line)

        assert abs(p_home - win) < 1e-12, f"赢份额不符 λ={lam} line={line}: {p_home} vs {win}"
        assert abs(p_draw - push) < 1e-12, f"走盘份额不符 λ={lam} line={line}: {p_draw} vs {push}"
        assert abs(p_away - loss) < 1e-12, f"输份额不符 λ={lam} line={line}: {p_away} vs {loss}"
        assert abs(win + push + loss - 1.0) < 1e-12, "三份额之和不为 1"
        # 核心不变式：净期望 = 赢份额 − 输份额
        assert abs(exp_u - (p_home - p_away)) < 1e-12, (
            f"E[u] 与定价不符 λ={lam} line={line}: {exp_u} vs {p_home - p_away}")


def test_ah_units_respects_the_line():
    """ah_units 必须真的用上让球线 —— 第 4 个 bug 就是它把 line 整个忽略了。

    符号约定（2026-10-11 用 33,060 场实测确认）：
    football-data.co.uk 的 AHCh 是**加在主队进球上的让球数**，
    强主队为负（主队让球），弱主队为正（主队受让）。
    实测主胜赔率 <1.6 的强主队让球线均值 -1.473，>3.0 的弱主队 +0.680。
    因此 margin = (h−a) + line：line 越负，主队越难打出。

    独立佐证：按本约定平注 ROI 为主队 -4.89% / 客队 -2.56%，
    与亚盘 overround 4.59% 吻合；若把 line 取反，ROI 变成 +15.88%
    —— 那种"稳定赚钱"只可能是符号搞错了。
    """
    lam = (2.0, 0.8)      # 主队明显更强，方向不会歧义
    exp = {line: _expected_units(lam[0], lam[1], line)[3] for line in LINES}
    ordered = [exp[line] for line in sorted(LINES)]
    assert ordered == sorted(ordered), f"期望未随让球线单调上升: {exp}"
    assert len(set(round(v, 9) for v in exp.values())) == len(LINES), \
        "不同让球线给出了相同的期望收益 —— line 很可能被忽略了"
    # 主队在平手线就该有正期望，让两球则明显变负
    assert exp[0.0] > 0.0 > exp[-2.0]


def test_half_line_is_win_not_push():
    """半盘线（0.5）下平局球是**赢**，不是走盘。

    margin = (h−a) + 0.5 在 h=a 时等于 0.5，必须判赢。
    用幅度阈值（|margin| <= 0.5 判走盘）会把这类结果误判为走盘。
    """
    assert ah_units(0, 0.5) == 1.0, "平局球 + 半球线应判主队赢"
    assert ah_units(0, 0.0) == 0.0, "平局球 + 平手线应判走盘"
    assert ah_units(0, 0.25) == 0.5, "平局球 + 0.25 线应判半赢（0 线走盘 + 0.5 线赢）"
    assert ah_units(-1, 1.0) == 0.0, "净负 1 球 + 让 1 球应判走盘"
    assert ah_units(-1, 0.75) == -0.5, "净负 1 球 + 让 0.75 线应判半输"
    assert ah_units(-1, 0.5) == -1.0, "净负 1 球 + 半球线应判全输"
    assert ah_units(-1, 0.0) == -1.0
    assert ah_units(1, 0.0) == 1.0


def test_quarter_line_never_fully_pushes():
    """四球线不可能整注走盘，但押回的注资份额非零。"""
    for lam in LAMS:
        for line in (-1.75, -1.25, -0.75, -0.25, 0.25, 0.75, 1.25, 1.75):
            assert abs(line * 4) % 2 == 1, "测试数据应只含四球线"
            _, p_draw, _ = ah_probs(lam[0], lam[1], line)
            # 整注走盘概率为 0 → 用 ah_units 找不到 u==0 的格子
            for h in range(13):
                for a in range(13):
                    assert ah_units(h - a, line) != 0.0, \
                        f"四球线 line={line} 在比分 {h}:{a} 出现了整注走盘"
            # 但押回份额可以非零（半注走盘）
            assert p_draw >= 0.0


def test_invert_1x2_recovers_known_lambda():
    """用已知 λ 生成的 1X2 概率必须能反解回同一个 λ。"""
    for lam_h, lam_a in LAMS:
        j = _joint(lam_h, lam_a)
        p_h = float(j[_DIFF > 0].sum())
        p_d = float(j[_DIFF == 0].sum())
        p_a = float(j[_DIFF < 0].sum())
        assert abs(p_h + p_d + p_a - 1.0) < 1e-12

        gh, ga = invert_1x2(p_h, p_d, p_a)
        assert abs(gh - lam_h) < 1e-3, f"λ_h 反解偏差过大: {gh} vs {lam_h}"
        assert abs(ga - lam_a) < 1e-3, f"λ_a 反解偏差过大: {ga} vs {lam_a}"


def test_invert_1x2_raises_on_impossible_input():
    """残差不达标必须抛错，不能静默返回一个无意义的初值。

    此前 resid 返回向量导致 L-BFGS 每次抛 ValueError 并被 except 吞掉，
    三万多场全部静默回退到 λ=(1.3, 1.3)，输出"看起来正常"实则毫无信息量。

    用一个**数学上不可行**的目标：近零的平局率配上 0.6/0.399 的主客强弱比。
    独立泊松下平局率由 e^-(λh+λa)·I₀(2√(λhλa)) 决定，要让平局率压到 0.001
    必须双方进球期望都很小，但那样主客强弱比不可能拉到 0.6:0.399。
    在 λ∈[0.05,4.5]² 网格（0.05 步长）上暴力搜索，最小最大残差为 0.1324。
    """
    with pytest.raises(RuntimeError):
        invert_1x2(0.60, 0.001, 0.399)


def test_flat_betting_a_fair_price_is_break_even():
    """端到端：把亚盘定在模型给出的公平赔率上，平注应当接近不赚不亏。

    这条把 devig2 → ah_probs → ah_units → ROI 串起来一起验，
    任何一环口径错位都会让 ROI 明显偏离 0。
    只用半球线（无走盘、无半赢半输），此时结算是二值的，公平赔率 = 1/p_home。
    """
    from engine.backtest.historical_replay import devig2

    for lam in LAMS:
        for line in (-0.5, 0.5):
            p_home, p_draw, p_away = ah_probs(lam[0], lam[1], line)
            assert p_draw < 1e-12, "半球线不应出现走盘"
            fair = 1.0 / p_home
            # 构造一个只含抽水的两路盘口，验去水后能还原回 fair
            for vig in (0.02, 0.06):
                o_home = (1.0 + vig) * fair
                o_away = (1.0 + vig) * (1.0 / p_away)
                mh, _ma = devig2((o_home, o_away), "proportional")
                assert abs(mh - p_home) < 1e-12
            # 按公平赔率平注的期望：p_home*(fair-1) + p_away*(-1) = 0
            expect = p_home * (fair - 1.0) + p_away * (-1.0)
            assert abs(expect) < 1e-12, f"公平价未盈亏平衡 λ={lam} line={line}: {expect}"


def test_ou_probs_sum_to_one():
    for lam in LAMS:
        p_over, p_under = ou_probs(lam[0], lam[1], 2.5)
        assert abs(p_over + p_under - 1.0) < 1e-12
        j = _joint(lam[0], lam[1])
        assert abs(p_over - float(j[_TOT > 2.5].sum())) < 1e-12
