#!/usr/bin/env python3
"""复核：DC 层 market_blend 是否把竞彩（target）赔率拼进模型概率 + devig 口径。

    .venv/bin/python -m pytest tests/test_dc_market_blend_review.py -q

对应报告：docs/proposals/dc-market-blend-and-devig-review.md
来源：scripts/leak_audit_check.py 的 UNVERIFIED_NOTES 两条「未复核观察点」。

写法（与 tests/test_leak_audit_independent_review.py 同口径）：
- 断言**判定结论与判定规则**，不钉硬编码计数、不钉行号；
- 代码事实用 AST 按结构判定（挪行、改名无关变量不影响）；
- 行为测试只喂假数据 / 只写 pytest tmp_path，不改仓库任何文件；
- 相对路径一律 as_posix()。

判定纪律（原 issue 要求）：只有「来源是 target + 路径在产 + 可复现」三条同时
成立才是 high/violation；路径不活封顶 info。本文件的断言即按这三条组织。
"""
from __future__ import annotations

import ast
import csv
import dataclasses
import sys
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.backtest.historical_replay import devig as replay_devig  # noqa: E402
from engine.backtest.historical_replay import load_football_data  # noqa: E402
from engine.pipeline.helpers import load_config  # noqa: E402
from engine.prediction.base import TeamRating  # noqa: E402
from engine.prediction.calibration import devig_shin, select_devig_method  # noqa: E402
from engine.prediction.dixon_coles import DixonColesConfig, DixonColesModel  # noqa: E402
from engine.prediction.ensemble import EnsembleModel  # noqa: E402
from engine.prediction.monte_carlo import MonteCarloConfig, MonteCarloModel  # noqa: E402

# 三组「明显不同」的目标盘赔率：均衡盘 / 极端热门 / 客队热门。
ODDS_BALANCED = (2.10, 3.40, 3.80)
ODDS_HEAVY_FAV = (1.13, 6.30, 12.0)
ODDS_AWAY_FAV = (4.80, 3.90, 1.72)
ODDS_SAMPLES = (ODDS_BALANCED, ODDS_HEAVY_FAV, ODDS_AWAY_FAV)

# 非默认评级：elo≠1500 或 attack≠1.0，MC 的「赔率反推 xG」分支不触发。
HOME = TeamRating(name="复核主队", elo=1650.0, attack=1.25, defense=0.85, form=0.2)
AWAY = TeamRating(name="复核客队", elo=1520.0, attack=1.05, defense=1.05, form=-0.1)


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _tree(rel: str) -> ast.AST:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def _py_files(*dirs: str):
    for d in dirs:
        for p in sorted((ROOT / d).rglob("*.py")):
            if "__pycache__" not in p.parts:
                yield p


def _production_dc_config() -> DixonColesConfig:
    """与 engine/main.py 构造 dc_cfg 的方式逐字一致（只取 dataclass 认识的键）。"""
    pred = load_config("prediction").get("prediction", {})
    return DixonColesConfig(**{k: v for k, v in pred.items()
                               if k in DixonColesConfig.__dataclass_fields__})


def _production_mc_config() -> MonteCarloConfig:
    pred = load_config("prediction").get("prediction", {})
    return MonteCarloConfig(**{k: v for k, v in pred.items()
                               if k in MonteCarloConfig.__dataclass_fields__})


def _probs(pred) -> tuple[float, float, float]:
    return (pred.home_win_prob, pred.draw_prob, pred.away_win_prob)


def _name_attrs(node: ast.AST) -> set[tuple[str, str]]:
    """形如 x.y 的属性读取（x 必须是简单名字）。"""
    return {(n.value.id, n.attr) for n in ast.walk(node)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}


def _string_constants(node: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _key_reads(tree: ast.AST, key: str) -> list[int]:
    """消费某个 dict 键：x["key"] / x.get("key")（与审计脚本同口径）。"""
    lines = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                and n.slice.value == key):
            lines.append(n.lineno)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"get", "pop"} and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == key):
            lines.append(n.lineno)
    return lines


def _predict_calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "predict"]


# ================================================================== 待核 1
# 问 1：market_odds 的来源是哪个市场（追取数代码，不看命名/注释）

def test_sporttery_fixture_odds_are_the_target_market_had(monkeypatch):
    """竞彩官方接口的 had(h/d/a) 原样成为 Fixture 的三项赔率 = target 市场。"""
    from engine.sources.sporttery import SportterySource

    payload = {"value": {"matchInfoList": [{
        "businessDate": "2026-11-04",
        "subMatchList": [{
            "matchNumStr": "周二001",
            "homeTeamAbbName": "复核主队", "awayTeamAbbName": "复核客队",
            "leagueAbbName": "英超", "matchDate": "2026-11-04", "matchTime": "20:00",
            "had": {"h": "2.10", "d": "3.40", "a": "3.80"},
            "hhad": {"goalLine": "-1", "h": "1.90", "d": "3.50", "a": "4.10"},
        }],
    }]}}
    seen: dict = {}

    def fake_fetch(self, url, params=None, retries=3):
        seen["url"] = url
        return payload

    monkeypatch.setattr(SportterySource, "_fetch_json", fake_fetch)
    fixtures = SportterySource().fetch_fixtures(date(2026, 11, 4))

    assert fixtures, "假响应必须能产出赛程，否则本检查是假绿"
    f = fixtures[0]
    assert (f.home_odds, f.draw_odds, f.away_odds) == ODDS_BALANCED
    # 让球盘是另一组值：证明取的是 had 而不是 hhad/别的盘口
    assert f.handicap_home_odds == 1.90
    # 市场身份由端点钉死，不靠类名/注释
    assert "sporttery" in seen["url"]
    assert f.source == "sporttery"


def _definition_closure(name: str, assigns: list[ast.Assign], depth: int = 0) -> list[ast.AST]:
    """名字的定义式闭包：含定义式里引用的其它名字（深度受限，防环）。"""
    out: list[ast.AST] = []
    for a in assigns:
        if not any(isinstance(t, ast.Name) and t.id == name for t in a.targets):
            continue
        out.append(a.value)
        if depth < 6:
            for n in ast.walk(a.value):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id != name:
                    out += _definition_closure(n.id, assigns, depth + 1)
    return out


def test_production_model_input_is_fixture_odds_with_pinnacle_fallback():
    """生产传给模型的 market_odds：首选 fixture（竞彩 HAD），兜底 Pinnacle。"""
    tree = _tree("engine/main.py")
    all_assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    assigns = [n for n in all_assigns
               if any(isinstance(t, ast.Name) and t.id == "market_odds"
                      for t in n.targets)]
    assert assigns, "main.py 必须给 market_odds 赋值"

    fixture_first = [a for a in assigns
                     if {("fixture", "home_odds"), ("fixture", "draw_odds"),
                         ("fixture", "away_odds")} <= _name_attrs(a.value)]
    assert fixture_first, "首选来源必须是 fixture 的三项赔率（= 竞彩 HAD）"

    closure = _definition_closure("market_odds", all_assigns)
    assert any("pinnacle_odds" in _string_constants(v) for v in closure), (
        "兜底来源必须能追到 DJYY 的 pinnacle_odds（reference 市场）")

    fed = [c for c in _predict_calls(tree)
           if any(k.arg == "market_odds" and isinstance(k.value, ast.Name)
                  and k.value.id == "market_odds" for k in c.keywords)]
    assert fed, "同一个 market_odds 变量必须被喂进 model.predict"


def test_merged_fixture_prefers_sporttery_over_bet365():
    """合并层 `体彩 or Bet365`：目标盘优先，参考盘只补缺失。"""
    tree = _tree("engine/sources/manager.py")
    target_first = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or)):
            continue
        left, right = n.values[0], n.values[-1]
        if (isinstance(left, ast.Attribute) and left.attr == "home_odds"
                and isinstance(right, ast.Subscript)
                and isinstance(right.value, ast.Name) and "b365" in right.value.id
                and isinstance(right.slice, ast.Constant) and right.slice.value == "h"):
            target_first.append(n)
    assert target_first, "合并层必须是竞彩赔率优先、Bet365 兜底"


# ------------------------------------------------------------------ 问 2
# 路径是否在产：配置值 + 构造方式 + 有没有别的活入口

def test_production_dc_blend_weight_is_zero():
    """判定：生产配置下 DC 内混权重为 0（config/prediction.json 的 prediction 段）。"""
    assert _production_dc_config().market_blend_weight == 0.0


def test_production_dc_config_comes_from_config_not_dataclass_defaults():
    """生产不是用裸 DixonColesConfig()（默认 0.28）构造，而是从 config 展开。"""
    calls = [n for n in ast.walk(_tree("engine/main.py"))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "DixonColesConfig"]
    assert calls
    for c in calls:
        assert any(k.arg is None for k in c.keywords), "必须以 **config 展开构造"
    assert "pred_cfg" in ast.get_source_segment(
        (ROOT / "engine/main.py").read_text(encoding="utf-8"), calls[0])


def test_league_level_market_blend_is_not_wired():
    """data/state/league_params.json 的非零 market_blend 进不了 DC：取值函数无调用方。"""
    callers = []
    for p in _py_files("engine", "scripts"):
        for n in ast.walk(_tree(_rel(p))):
            if isinstance(n, ast.Call):
                fname = (n.func.attr if isinstance(n.func, ast.Attribute)
                         else n.func.id if isinstance(n.func, ast.Name) else "")
                if fname == "get_market_blend":
                    callers.append(f"{_rel(p)}:{n.lineno}")
    assert callers == []


def test_only_default_config_dc_script_feeds_no_odds():
    """唯一用默认 0.28 构造 DC 的脚本不喂赔率 ⇒ 那个默认值同样不活。"""
    tree = _tree("scripts/elo_backfill_replay.py")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "DixonColesConfig" and not n.args and not n.keywords
               for n in ast.walk(tree)), "该脚本确实用裸默认配置构造 DC"
    calls = _predict_calls(tree)
    assert calls
    for c in calls:
        kw = next((k for k in c.keywords if k.arg == "market_odds"), None)
        assert kw is not None and isinstance(kw.value, ast.Constant) and kw.value.value is None


def test_evolution_harness_blend_key_is_dead():
    """harness 的 DEFAULT_PRED 留着 0.28，但没有任何代码读它。"""
    assert "market_blend_weight" in (ROOT / "evolution/harness/model_core.py").read_text(
        encoding="utf-8"), "键必须还在，否则本检查是假绿"
    readers = []
    for p in _py_files("evolution"):
        if _key_reads(_tree(_rel(p)), "market_blend_weight"):
            readers.append(_rel(p))
    assert readers == []


# ------------------------------------------------------------------ 问 3
# 可复现性：喂明显不同的 target 赔率，输出概率是否随之变化

def test_dc_probabilities_do_not_follow_target_odds_under_production_config():
    """核心判定：生产配置下，DC 概率对 target 赔率**逐位不变** ⇒ 不可复现。

    这条断言同时是护栏：谁把 market_blend_weight 调回非零，它就变红。
    """
    model = DixonColesModel(_production_dc_config())
    base = model.predict(HOME, AWAY, market_odds=None)
    for odds in ODDS_SAMPLES:
        pred = model.predict(HOME, AWAY, market_odds=odds)
        assert _probs(pred) == _probs(base), f"target 赔率 {odds} 改变了模型概率"
        assert (pred.home_xg, pred.away_xg) == (base.home_xg, base.away_xg)


def test_ensemble_probabilities_do_not_follow_target_odds_under_production_config():
    """生产入口是 EnsembleModel(DC+MC)：非默认评级下同样对 target 赔率不敏感。"""
    cfg = load_config("prediction")
    model = EnsembleModel(
        dc_config=_production_dc_config(),
        mc_config=_production_mc_config(),
        weights={"dixon_coles": cfg.get("ensemble", {}).get("dixon_coles_weight", 0.6),
                 "monte_carlo": cfg.get("ensemble", {}).get("monte_carlo_weight", 0.4)},
    )
    base = _probs(model.predict(HOME, AWAY, market_odds=None))
    for odds in ODDS_SAMPLES:
        assert _probs(model.predict(HOME, AWAY, market_odds=odds)) == base


def test_blend_branch_executes_but_is_output_inert(monkeypatch):
    """分支在生产里确实走到（不是"没有这段代码"），使输出不变的是权重 0。"""
    model = DixonColesModel(_production_dc_config())
    seen: list = []
    original = model._de_vig

    def spy(odds):
        seen.append(odds)
        return original(odds)

    monkeypatch.setattr(model, "_de_vig", spy)
    pred = model.predict(HOME, AWAY, market_odds=ODDS_BALANCED)
    assert seen == [ODDS_BALANCED]
    assert _probs(pred) == _probs(model.predict(HOME, AWAY, market_odds=None))


def test_nonzero_blend_really_pulls_probabilities_toward_target_odds():
    """防假绿：权重非零时同一分支确实把概率拉向 target 赔率（机制真实存在）。

    即「来源 target + 路径在产 + 可复现」三条在 w>0 时同时成立 —— 所以
    生产判定完全依赖 w=0 这一条配置事实。
    """
    w = 0.28
    blended_model = DixonColesModel(dataclasses.replace(
        _production_dc_config(), market_blend_weight=w))
    pure_model = DixonColesModel(dataclasses.replace(
        _production_dc_config(), market_blend_weight=0.0))
    pure = _probs(pure_model.predict(HOME, AWAY, market_odds=None))

    by_odds = {}
    for odds in ODDS_SAMPLES:
        got = _probs(blended_model.predict(HOME, AWAY, market_odds=odds))
        fair = blended_model._de_vig(odds)
        assert got != pure, "权重非零却没动概率 ⇒ 分支被别的东西短路了"
        for p, g, f in zip(pure, got, fair):
            assert abs(g - f) < abs(p - f), "混合必须把概率拉向该 target 赔率"
            assert g == pytest.approx((1 - w) * p + w * f, abs=5e-4)
        by_odds[odds] = got

    assert by_odds[ODDS_HEAVY_FAV][0] > by_odds[ODDS_BALANCED][0]
    assert by_odds[ODDS_AWAY_FAV][2] > by_odds[ODDS_BALANCED][2]


def test_mc_odds_to_xg_branch_needs_default_ratings():
    """另列（非 DC 主张）：MC 用赔率反推 xG 只在评级为默认值时活。

    生产评级缺失的球队会落到默认值（EloUpdater.get_rating），因此这条路径
    与 DC 内混不同，**没有权重开关**；是否构成违规交人裁决，本测试只钉事实。
    """
    model = MonteCarloModel(_production_mc_config())
    base = _probs(model.predict(HOME, AWAY, market_odds=None))
    for odds in ODDS_SAMPLES:
        assert _probs(model.predict(HOME, AWAY, market_odds=odds)) == base

    default_home = TeamRating(name="无评级主队")  # elo=1500, attack=1.0
    default_base = _probs(model.predict(default_home, AWAY, market_odds=None))
    changed = [_probs(model.predict(default_home, AWAY, market_odds=o))
               for o in ODDS_SAMPLES]
    assert any(p != default_base for p in changed), "默认评级下赔率必须能改 xG/概率"
    assert changed[0] != changed[1], "不同的 target 赔率必须给出不同概率"


# ================================================================== 待核 2
# devig 口径：回放 proportional vs 服务 Shin —— 是否同对象漂移

def test_serving_devig_entry_is_unconditionally_shin():
    """服务入口没有判据/开关：select_devig_method 恒等于 devig_shin。"""
    fn = next(n for n in ast.walk(_tree("engine/prediction/calibration.py"))
              if isinstance(n, ast.FunctionDef) and n.name == "select_devig_method")
    assert not any(isinstance(n, (ast.If, ast.IfExp)) for n in ast.walk(fn))
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "devig_shin" for n in ast.walk(fn))
    for odds in ODDS_SAMPLES:
        assert select_devig_method(list(odds)) == pytest.approx(devig_shin(list(odds)))
        assert sum(select_devig_method(list(odds))) == pytest.approx(1.0, abs=1e-6)


def _write_csv(dir_path: Path, rows: list[dict]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with open(dir_path / "E0.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _row(**overrides) -> dict:
    base = {
        "Date": "04/11/2026", "Div": "E0", "HomeTeam": "H", "AwayTeam": "A",
        "FTHG": "1", "FTAG": "0",
        "AvgCH": "2.10", "AvgCD": "3.40", "AvgCA": "3.80",
        "PSCH": "2.20", "PSCD": "3.30", "PSCA": "3.60",
        "B365CH": "2.15", "B365CD": "3.35", "B365CA": "3.70",
        "AvgH": "2.05", "AvgD": "3.45", "AvgA": "3.90",
    }
    base.update(overrides)
    return base


def test_replay_benchmark_object_is_reference_close_not_target(tmp_path):
    """回放基准的赔率对象 = 参考盘收盘（Avg → Pinnacle → Bet365），与竞彩无关。"""
    full = tmp_path / "full"
    _write_csv(full, [_row()])
    matches = load_football_data(full)
    assert matches and matches[0].close == (2.10, 3.40, 3.80)
    assert matches[0].open_ == (2.05, 3.45, 3.90)

    no_avg = tmp_path / "no_avg"
    row = _row()
    for k in ("AvgCH", "AvgCD", "AvgCA"):
        row.pop(k)
    _write_csv(no_avg, [row])
    assert load_football_data(no_avg)[0].close == (2.20, 3.30, 3.60)  # Pinnacle

    only_b365 = tmp_path / "only_b365"
    for k in ("PSCH", "PSCD", "PSCA"):
        row.pop(k)
    _write_csv(only_b365, [row])
    assert load_football_data(only_b365)[0].close == (2.15, 3.35, 3.70)  # Bet365

    # 防假绿：塞进假设的竞彩列，loader 必须无视它（对象里根本没有目标盘）
    with_target = tmp_path / "with_target"
    _write_csv(with_target, [_row(SportteryH="1.95", SportteryD="3.10", SportteryA="4.20")])
    assert load_football_data(with_target)[0].close == matches[0].close


def test_replay_default_devig_is_proportional_and_differs_from_serving():
    """字面主张成立：两套口径确实不同（但是否同对象见上一条）。"""
    for odds in ODDS_SAMPLES:
        inv = [1.0 / o for o in odds]
        expected = tuple(p / sum(inv) for p in inv)
        assert replay_devig(odds) == pytest.approx(expected)
        assert replay_devig(odds, "proportional") == pytest.approx(replay_devig(odds))
    assert any(max(abs(a - b) for a, b in
                   zip(replay_devig(o), select_devig_method(list(o)))) > 1e-6
               for o in ODDS_SAMPLES)


def test_fusion_switch_replay_reuses_frozen_market_fair():
    """裁决融合开关的回放不重新去水：用生产冻结的 market_fair ⇒ 无口径漂移。"""
    rel = "scripts/ablation_replay.py"
    tree = _tree(rel)
    devig_names = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            devig_names += [a.name for a in n.names if "devig" in a.name]
        if isinstance(n, ast.Import):
            devig_names += [a.name for a in n.names if "devig" in a.name]
        if isinstance(n, ast.Call):
            fname = (n.func.attr if isinstance(n.func, ast.Attribute)
                     else n.func.id if isinstance(n.func, ast.Name) else "")
            if "devig" in fname:
                devig_names.append(fname)
    assert devig_names == [], f"{rel} 不得自己重新去水"
    assert _key_reads(tree, "market_fair"), f"{rel} 必须读账本冻结的 market_fair"


def test_serving_market_channel_is_shin_of_the_same_target_odds():
    """服务侧：同一份 target 赔率经 Shin 去水后进入融合市场通道（既有设计）。"""
    tree = _tree("engine/main.py")
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "calibrated_probs"
                       for t in n.targets)
               and isinstance(n.value, ast.Call)
               and isinstance(n.value.func, ast.Name)
               and n.value.func.id == "select_devig_method"]
    assert assigns
    assert any(isinstance(a, ast.Name) and a.id == "market_odds"
               for n in assigns for a in ast.walk(n.value))
    fusion_inputs = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "FusionInput"]
    assert fusion_inputs
    assert any(any(k.arg == "market_probs"
                   and "calibrated_probs" in ast.dump(k.value) for k in c.keywords)
               for c in fusion_inputs)


def test_same_object_double_devig_exists_but_is_masked_by_zero_blend():
    """真正的同对象不一致在服务内部：DC 比例去水 vs market_fair 的 Shin。

    它被 market_blend_weight=0 掩蔽（见上面的逐位不变断言），所以封顶 info。
    """
    dc = DixonColesModel(_production_dc_config())
    assert dc.cfg.market_blend_weight == 0.0
    for odds in ODDS_SAMPLES:
        proportional = dc._de_vig(odds)
        shin = select_devig_method(list(odds))
        assert max(abs(a - b) for a, b in zip(proportional, shin)) > 1e-6


def test_two_shin_implementations_are_not_equivalent():
    """都叫 Shin 也不等价：回放固定 z=0.02，服务二分求 z。"""
    diffs = [max(abs(a - b) for a, b in
                 zip(replay_devig(o, "shin"), devig_shin(list(o))))
             for o in ODDS_SAMPLES]
    assert max(diffs) > 1e-6
    for odds in ODDS_SAMPLES:
        assert sum(replay_devig(odds, "shin")) == pytest.approx(1.0, abs=1e-6)
        assert sum(devig_shin(list(odds))) == pytest.approx(1.0, abs=1e-6)


def test_shin_vs_proportional_gap_grows_with_favourite_strength():
    """口径差不是常数：极端热门处显著大于均衡盘（docstring 的 max 低估）。"""
    def gap(odds):
        return max(abs(a - b) for a, b in
                   zip(replay_devig(odds, "proportional"), devig_shin(list(odds))))

    assert gap(ODDS_HEAVY_FAV) > gap(ODDS_BALANCED)


# ================================================================== 写法守卫

def test_relative_paths_are_posix_under_both_path_flavours():
    for root in (PurePosixPath(ROOT.as_posix()), PureWindowsPath("C:/repo")):
        p = root / "engine" / "prediction" / "dixon_coles.py"
        assert p.relative_to(root).as_posix() == "engine/prediction/dixon_coles.py"
    assert _rel(ROOT / "config" / "prediction.json") == "config/prediction.json"
