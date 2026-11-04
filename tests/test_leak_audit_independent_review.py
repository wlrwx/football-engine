"""独立复核 PR #80 泄漏审计 6 条结论 —— 可复现检查（只读，不改任何生产行为）。

对应报告：docs/proposals/leak-audit-independent-review.md
每个测试钉住一条代码事实；跑一遍即可复现报告里的判定：

    .venv/bin/python -m pytest tests/test_leak_audit_independent_review.py -q

判定摘要（盲测，逐条追代码得出，不引用注释/docstring 作证据）：
  1 成立（29 处命中分布正确；措辞修正：lgbm 命中是特征名表+受闸赋值，非参数注释）
  2 成立
  3 不成立（信号源是 Bet365/Pinnacle 参考市场；且生产开关 false，路径不活）
  4 成立（核心"未进入概率形成"成立；措辞修正：实际消费者是页面展示+落盘）
  5 成立（代码只读 opening* 字段；市场归属另列待核）
  6 不成立（按字面：生产调用传 15 参，is_knockout 未传；两端同默认值，无实际偏移）
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _py_files(*dirs: str):
    for d in dirs:
        yield from sorted((ROOT / d).rglob("*.py"))


def _string_constants(rel: str) -> list[str]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


# ---------------------------------------------------------------- 结论 1
# 黑名单扫描：engine/ 全量 .py，14 字段共 29 处命中，只落在两个文件。
# 修正点：lgbm_model.py 的 12 处是 BASE_FEATURE_NAMES(53-56) 与 build_features
# 受 include_market_odds 闸控制的赋值(288-296)，不是"参数注释"。

def test_claim1_blacklist_scan_29_hits_in_two_files():
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    assert len(TARGET_MARKET_FEATURES) == 14
    pat = re.compile(r"\b(" + "|".join(sorted(TARGET_MARKET_FEATURES)) + r")\b")
    hits: dict[str, int] = {}
    for p in _py_files("engine"):
        n = sum(len(pat.findall(line))
                for line in p.read_text(encoding="utf-8").splitlines())
        if n:
            hits[p.relative_to(ROOT).as_posix()] = n
    assert sum(hits.values()) == 29
    assert hits == {
        "engine/prediction/market_isolation.py": 17,
        "engine/prediction/lgbm_model.py": 12,
    }


def test_claim1_odds_features_zero_filled_when_gate_off():
    # 命中点之所以"预期"：include_market_odds 默认 False 时四个字段恒 0.0，
    # 即使传入真实赔率也不进特征（lgbm_model.py:285-296）。
    from engine.prediction.lgbm_model import build_features
    f = build_features(elo_home=1500.0, elo_away=1400.0, odds=(2.0, 3.2, 3.5))
    for k in ("odds_home_impl", "odds_draw_impl", "odds_away_impl", "odds_overround"):
        assert f[k] == 0.0


# ---------------------------------------------------------------- 结论 2
# use_odds_features 默认 False，代码/配置任何位置均未设为 True。

def test_claim2_use_odds_features_false_everywhere():
    from engine.prediction.lgbm_model import LGBMConfig
    assert LGBMConfig().use_odds_features is False
    assert _read_json("config/prediction.json")["lgbm"]["use_odds_features"] is False
    assert _read_json("evolution/demo/base_prediction.json")["lgbm"]["use_odds_features"] is False

    banned = {"use_odds_features", "include_market_odds"}
    offenders = []
    for p in _py_files("engine", "scripts", "tests", "evolution"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (isinstance(n, ast.keyword) and n.arg in banned
                    and isinstance(n.value, ast.Constant) and n.value.value is True):
                offenders.append(f"{p}:{n.lineno}")
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    name = (t.id if isinstance(t, ast.Name)
                            else t.attr if isinstance(t, ast.Attribute) else "")
                    if (name in banned and isinstance(n.value, ast.Constant)
                            and n.value.value is True):
                        offenders.append(f"{p}:{n.lineno}")
    assert offenders == []


# ---------------------------------------------------------------- 结论 3
# sina_odds_movement：生产开关 false（路径不活）；compression/movement 的取数
# 代码优先 Bet365(companyId=7)/Pinnacle(companyId=9)，属参考市场，非竞彩官方。

def test_claim3_sina_movement_switch_off_in_production_config():
    cfg = _read_json("config/prediction.json")
    assert cfg["fusion"]["post_fusion"]["sina_odds_movement"] is False

    from engine.prediction.fusion import FusionInput, fuse_probabilities
    sina = {"movement": {"home": "down", "draw": "flat", "away": "up"},
            "compression": {"home": 1.2, "draw": 1.0, "away": 0.8}}
    out = fuse_probabilities(FusionInput(
        model_probs=(0.5, 0.3, 0.2), sina_data=sina,
        post_fusion=dict(cfg["fusion"]["post_fusion"]),
    ))
    assert not any(t["step"] == "sina_odds_movement" for t in out.trace)
    assert out.probs == (0.5, 0.3, 0.2)


def test_claim3_sina_movement_step_fires_only_when_switch_on():
    # 对照：开关翻开时该步确实会动概率 —— 证明生产不动概率的原因是 config 闸。
    from engine.prediction.fusion import FusionInput, fuse_probabilities
    sina = {"movement": {"home": "down"},
            "compression": {"home": 1.2, "draw": 1.0, "away": 0.8}}
    out = fuse_probabilities(FusionInput(
        model_probs=(0.5, 0.3, 0.2), sina_data=sina,
        post_fusion={"sina_odds_movement": True},
    ))
    assert any(t["step"] == "sina_odds_movement" for t in out.trace)
    assert out.probs != (0.5, 0.3, 0.2)


def test_claim3_compression_computed_from_bet365_or_pinnacle():
    # fetch_sina_odds.py:99-103 primary = bet365 or pinnacle or first；
    # compression = 该公司 initial/current（142-144）。即使列表里混进一家
    # "竞彩官方"，取数代码也不会选它 —— 信号源是参考市场。
    spec = importlib.util.spec_from_file_location(
        "fetch_sina_odds_review", ROOT / "scripts" / "fetch_sina_odds.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    match = {"matchId": "1", "matchNo": "周五001", "team1": "A", "team2": "B"}
    bet365 = {"companyId": "7", "companyName": "Bet365",
              "o1Ini": "2.1", "o2Ini": "3.2", "o3Ini": "3.6",
              "o1New": "1.8", "o2New": "3.3", "o3New": "4.2"}
    pinnacle = {"companyId": "9", "companyName": "Pinnacle",
                "o1Ini": "2.0", "o2Ini": "3.3", "o3Ini": "3.8",
                "o1New": "1.9", "o2New": "3.4", "o3New": "4.0"}
    other = {"companyId": "99", "companyName": "竞彩官方",
             "o1Ini": "9.9", "o2Ini": "9.9", "o3Ini": "9.9",
             "o1New": "1.1", "o2New": "1.1", "o3New": "1.1"}

    snap = mod.extract_odds_snapshot(match, [pinnacle, bet365, other], [], [], {})
    assert snap["euro"]["company"] == "Bet365"
    assert snap["euro"]["compression"]["home"] == round(2.1 / 1.8, 3)
    assert snap["euro"]["movement"]["home"] == "down"

    snap2 = mod.extract_odds_snapshot(match, [pinnacle, other], [], [], {})
    assert snap2["euro"]["company"] == "Pinnacle"
    assert snap2["euro"]["compression"]["home"] == round(2.0 / 1.9, 3)


# ---------------------------------------------------------------- 结论 4
# series 在 build_sina_data 构造；不进入概率形成。
# 修正点：实际消费者是 build_site 页面展示 + predictions.json 落盘，
# engine/review 与 scripts 里没有任何读 "series" 的代码（非"结算验证"）。

def test_claim4_series_built_in_build_sina_data():
    from engine.pipeline.sina_odds import build_sina_data
    # 虚构编号，保证不命中任何真实序列文件 → points=0，纯只读。
    out = build_sina_data({"euro": {}}, "1999-01-01_周一999")
    assert "series" in out
    assert out["series"].get("points") == 0


def test_claim4_fusion_never_reads_series():
    assert "series" not in _string_constants("engine/prediction/fusion.py")


def test_claim4_series_does_not_change_probabilities():
    from engine.prediction.fusion import FusionInput, fuse_probabilities
    base = {"movement": {"home": "down"},
            "compression": {"home": 1.2, "draw": 1.0, "away": 1.0}}
    p1 = fuse_probabilities(FusionInput(
        model_probs=(0.5, 0.3, 0.2), sina_data=base)).probs
    p2 = fuse_probabilities(FusionInput(
        model_probs=(0.5, 0.3, 0.2),
        sina_data={**base, "series": {"points": 9, "recent_home": -5.0,
                                      "recent_away": 4.0, "overall_home": -8.0}})).probs
    assert p1 == p2


def test_claim4_no_settlement_reader_of_series():
    for p in _py_files("engine/review", "scripts"):
        assert "series" not in _string_constants(p.relative_to(ROOT).as_posix()), p


def test_claim4_series_consumed_by_build_site_display():
    assert "series" in _string_constants("engine/build_site.py")


# ---------------------------------------------------------------- 结论 5
# njstats_features 只读 openingHome/openingDraw/openingAway，输出
# opening/opening_fair；不读任何 closing 字段。site_fair（站点价=目标市场）
# 被 to_features 排除在外。

def test_claim5_extract_record_reads_opening_only():
    from engine.sources.njstats_features import devig, extract_record
    r = {"matchId": "m1", "homeName": "A", "awayName": "B",
         "openingHome": 2.5, "openingDraw": 3.2, "openingAway": 2.8,
         # 假设存在的收盘字段：代码不应读取
         "closingHome": 1.9, "closingDraw": 3.4, "closingAway": 4.0}
    out = extract_record(r, "2026-11-04", "2026-11-04 08:00:00")
    assert out["opening"] == {"home": 2.5, "draw": 3.2, "away": 2.8}
    assert out["opening_fair"] == devig(out["opening"])
    consts = _string_constants("engine/sources/njstats_features.py")
    assert not any("closing" in c.lower() for c in consts)


def test_claim5_side_features_exclude_site_fair():
    from engine.sources.njstats_side import SideSignal
    sig = SideSignal(home="A", away="B", kickoff="", snapshot_at="",
                     open_fair=(0.4, 0.3, 0.3), site_fair=(0.5, 0.25, 0.25))
    f = sig.to_features()
    assert set(f) == {"side_open_home", "side_open_draw", "side_open_away"}
    assert not any("site" in k for k in f)


# ---------------------------------------------------------------- 结论 6
# 按字面不成立：签名 16 参，main.py 生产调用只传 15 参（is_knockout 未传）；
# 训练侧传 14 参（odds/is_knockout 未传）。未传参数两端同取默认值，
# 实际特征值无偏移 —— "对齐"的实质结论成立，"16 参完全一致"的证据不成立。

def _build_features_params() -> list[str]:
    tree = ast.parse((ROOT / "engine/prediction/lgbm_model.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "build_features")
    return [a.arg for a in fn.args.args]


def _build_features_call_kwargs(rel: str) -> list[str]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "build_features"]
    assert len(calls) == 1, rel
    return [kw.arg for kw in calls[0].keywords]


def test_claim6_signature_16_params_production_call_passes_15():
    params = _build_features_params()
    assert len(params) == 16
    assert "is_knockout" in params

    serve = _build_features_call_kwargs("engine/main.py")
    assert len(serve) == 15
    assert set(serve) <= set(params)
    assert set(params) - set(serve) == {"is_knockout"}

    train = _build_features_call_kwargs("engine/learning/lgbm_shadow.py")
    assert len(train) == 14
    assert set(train) <= set(params)
    assert set(params) - set(train) == {"odds", "is_knockout"}


def test_claim6_omitted_is_knockout_defaults_to_zero():
    from engine.prediction.lgbm_model import build_features
    f = build_features(elo_home=1500.0, elo_away=1400.0)
    assert f["is_knockout"] == 0.0
