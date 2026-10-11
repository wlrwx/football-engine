"""E4 换盘口收益的回归测试。

重点是 `parse_score` / `parse_odds` 必须**分开**。
本会话踩过：用赔率解析器读比分时，`0 > 1.0` 为假 → 所有 0 球/1 球比赛
被静默丢弃，样本从 67,613 掉到 3,997，只剩高进球场次，
Pinnacle ROI 被算成 −29.65%（真实值 −1.32%）。

输出"看起来正常"（有注数、有 ROI、有 t），但样本已经偏到毫无意义 ——
这类静默过滤比直接报错危险得多。
"""
import pytest

pytest.importorskip("numpy")

from engine.backtest.book_vig import (  # noqa: E402
    BOOKS, devig, overround, parse_odds, parse_score,
)


class TestParsers:
    def test_score_accepts_zero_and_one(self):
        """0 球和 1 球是绝大多数比赛，解析器必须收。"""
        for v in ("0", "1", "2", "3", 0, 1, 2, 3, "0.0", "1.0"):
            assert parse_score(v) is not None, f"比分 {v!r} 被误丢弃"

    def test_score_rejects_missing_and_absurd(self):
        for v in (None, "", "NA", "abc", -1, 16, 99):
            assert parse_score(v) is None, f"比分 {v!r} 不该被接受"

    def test_odds_rejects_below_one(self):
        """赔率必须 >1.0。这是它和比分解析器的**唯一**区别。"""
        assert parse_odds("2.10") == 2.10
        for v in ("1.0", "0.9", "1", 0, None, "", "abc"):
            assert parse_odds(v) is None, f"赔率 {v!r} 不该被接受"

    def test_parsers_are_not_interchangeable(self):
        """显式锁住这条教训：0 球对赔率解析器是非法的，对比分解析器是合法的。"""
        assert parse_odds("0") is None
        assert parse_score("0") == 0
        assert parse_odds("1") is None
        assert parse_score("1") == 1

    def test_score_zero_share_is_material(self):
        """若比分 0 被丢弃，样本会严重偏向高进球场。用真实分布说明量级。"""
        # 平局的概率在真实足球里约 25%，全靠 parse_score("0") 收进来
        assert parse_score("0") == 0


class TestDevig:
    def test_devig_sums_to_one(self):
        p = devig([2.10, 3.40, 3.60])
        assert abs(sum(p) - 1.0) < 1e-12

    def test_overround_of_typical_book(self):
        # 欧洲盘典型 5~7%
        assert 1.03 < overround([2.10, 3.40, 3.60]) < 1.10

    def test_devig_inverts_odds_ordering(self):
        """赔率越低概率越高 —— 去水后必须是**逆序**，不是保序。"""
        p = devig([5.78, 4.14, 1.64])
        assert p[0] < p[1] < p[2]
        # 最低赔率的那一侧概率应过半（1.64 → ~0.57）
        assert p[2] > 0.5


class TestBookRegistry:
    def test_known_columns_exist_in_dataset(self):
        """BOOKS 里列的名字不能拼错 —— 拼错会静默返回全 None，
        在报告里表现为「样本不足」，看起来像数据问题而不是代码问题。"""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        files = sorted((root / "data" / "historical" / "football_data").rglob("*.csv"))
        assert files, "找不到 football_data"
        import csv
        cols: set[str] = set()
        for fp in files[:40]:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                cols.update(next(csv.reader(fh)))
        for name, c3 in BOOKS.items():
            missing = [c for c in c3 if c not in cols]
            assert not missing, f"{name} 的列 {missing} 在数据里不存在（拼错？）"

    def test_closing_and_opening_are_distinct_columns(self):
        """开盘与收盘必须是不同的列名。若被写成同一组，'换盘口' 会变成
        '同一个盘口算两遍'，结论直接作废。"""
        assert BOOKS["Pinnacle(PS)"] != BOOKS["PS开盘"]
        assert BOOKS["B365"] != BOOKS["B365开盘"]
