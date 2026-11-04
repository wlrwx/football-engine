"""独立复核 PR #80 泄漏审计 6 条结论 —— 可复现检查（只读，不改任何生产行为）。

对应报告：docs/proposals/leak-audit-independent-review.md

    .venv/bin/python -m pytest tests/test_leak_audit_independent_review.py -q

写法约定（2026-11-04 按 review 意见重写）：**断言判定结论，不钉计数**。
黑名单加字段、代码挪行、build_features 加带默认值的参数，都不该让 CI 变红；
只有"判定结论被推翻"才红。命中数、每文件条数、参数个数只出现在报告文本
（docs/proposals/leak-audit-independent-review.md）里，不进断言。

判定摘要（盲测，逐条追代码得出，不引用注释/docstring 作证据）：
  1 成立（命中全部落在定义/特征名表/受闸赋值/自检演示；生产特征不带市场信息）
  2 成立（无任何 True 赋值；按当前 config 闸构造的特征对赔率不敏感）
  3 不成立（信号源是 Bet365/Pinnacle 参考市场；开关关闭时概率逐位不变）
  4 成立（概率形成不消费 series；消费者是页面展示+落盘）
  5 成立（只读 opening* 字段；站点价改变不影响特征）
  6 按字面"16 参完全一致"不成立；实质成立：未传参数取签名默认值，
    训练/服务在共同输入下特征逐位相同，无实际偏移
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import io
import json
import re
import tokenize
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]


def _read_json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _tree(rel: str) -> ast.AST:
    return ast.parse(_source(rel))


def _py_files(*dirs: str):
    for d in dirs:
        yield from sorted((ROOT / d).rglob("*.py"))


def _rel(path, root=ROOT) -> str:
    """仓库相对路径，统一 POSIX 分隔符（Windows 上也是 /）。"""
    return path.relative_to(root).as_posix()


def _string_constants(rel: str) -> list[str]:
    return [n.value for n in ast.walk(_tree(rel))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _reads_key(tree: ast.AST, key: str) -> list[int]:
    """消费某个 dict 键的代码行：x["key"] 或 x.get("key")。

    只认"读取行为"，不认任何提到该词的字符串常量——审计/文档脚本提到 key
    不算消费，新增报表字段也不会误红。
    """
    lines: list[int] = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                and n.slice.value == key):
            lines.append(n.lineno)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"get", "pop"} and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == key):
            lines.append(n.lineno)
    return lines


# ---------------------------------------------------------------- 结论 1
# 判定：黑名单字段的每一处出现都落在"允许位置"（黑名单/特征名表定义、
# 受 include_market_odds 闸控制的赋值、__main__ 自检演示、tests/），
# 且生产特征构造路径上不带任何目标市场信息。计数只进报告，不进断言。

GATE_NAMES = {"include_market_odds", "use_odds_features"}
DEFINITION_NAMES = {
    "TARGET_MARKET_FEATURES", "REFERENCE_MARKET_FEATURES", "BENCHMARK_ONLY_FEATURES",
}


def _allowed_ranges(tree: ast.AST) -> list[tuple[int, int, str]]:
    """允许出现黑名单字段的行区间 + 理由。按结构判定，与行数/条数无关。"""
    ranges: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if names & DEFINITION_NAMES or any(n.endswith("FEATURE_NAMES") for n in names):
                ranges.append((node.lineno, node.end_lineno or node.lineno, "definition"))
        if isinstance(node, ast.If):
            if any(isinstance(n, ast.Name) and n.id in GATE_NAMES
                   for n in ast.walk(node.test)):
                ranges.append((node.lineno, node.end_lineno or node.lineno, "gated"))
            if any(isinstance(n, ast.Name) and n.id == "__name__"
                   for n in ast.walk(node.test)):
                ranges.append((node.lineno, node.end_lineno or node.lineno, "selftest"))
    return ranges


def _occurrences(text: str, features: set[str]) -> list[tuple[int, str]]:
    """代码里对黑名单字段的引用（行号, 字段）。

    用 tokenize：注释与 docstring 自然被排除；字符串字面量只认"整体等于
    字段名"的（dict 键、集合成员），不认长文本里顺带提到。
    """
    out: list[tuple[int, str]] = []
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        words: list[str] = []
        if tok.type == tokenize.NAME:
            words = [tok.string]
        elif tok.type == tokenize.STRING:
            try:
                value = ast.literal_eval(tok.string)
            except (ValueError, SyntaxError):
                value = None
            words = [value] if isinstance(value, str) else []
        elif fstring_middle is not None and tok.type == fstring_middle:
            words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", tok.string)
        out.extend((tok.start[0], w) for w in words if w in features)
    return out


def classify_source(text: str, features: set[str], *, in_tests: bool = False) -> dict:
    """把命中分成 allowed / unallowed。unallowed 非空 = 生产路径疑似泄漏。"""
    ranges = [] if in_tests else _allowed_ranges(ast.parse(text))
    allowed: list[dict] = []
    unallowed: list[dict] = []
    for line, feat in _occurrences(text, features):
        kind = "tests" if in_tests else next(
            (kind for lo, hi, kind in ranges if lo <= line <= hi), "")
        item = {"line": line, "feature": feat, "kind": kind}
        (allowed if kind else unallowed).append(item)
    return {"allowed": allowed, "unallowed": unallowed}


def test_claim1_every_blacklist_hit_is_in_an_allowed_location():
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    features = set(TARGET_MARKET_FEATURES)
    unallowed: list[str] = []
    for p in _py_files("engine", "tests"):
        rel = _rel(p)
        res = classify_source(p.read_text(encoding="utf-8"), features,
                              in_tests=rel.startswith("tests/"))
        unallowed.extend(f"{rel}:{h['line']} {h['feature']}" for h in res["unallowed"])
    assert unallowed == [], "黑名单字段出现在非允许位置：\n" + "\n".join(unallowed)


def test_claim1_classifier_rejects_an_ungated_production_assignment():
    # 防止"扫描恒空"导致的假绿：无闸的生产赋值必须被判为 unallowed。
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    features = set(TARGET_MARKET_FEATURES)
    feat = sorted(features)[0]
    bad = f'def build(odds):\n    return {{"{feat}": 1.0 / odds[0]}}\n'
    assert classify_source(bad, features)["unallowed"]
    good = (
        "def build(odds, include_market_odds=False):\n"
        "    if include_market_odds and odds:\n"
        f'        return {{"{feat}": 1.0 / odds[0]}}\n'
        "    else:\n"
        f'        return {{"{feat}": 0.0}}\n'
    )
    res = classify_source(good, features)
    assert res["allowed"] and not res["unallowed"]


def test_claim1_production_features_carry_no_target_market_information():
    # 判定：按生产默认闸构造特征时，黑名单键不随赔率变化（恒 0.0）。
    from engine.prediction.lgbm_model import LGBMConfig, build_features
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    gate = LGBMConfig().use_odds_features
    a = build_features(elo_home=1500.0, elo_away=1400.0, odds=(2.0, 3.2, 3.5),
                       include_market_odds=gate)
    b = build_features(elo_home=1500.0, elo_away=1400.0, odds=(1.2, 6.0, 15.0),
                       include_market_odds=gate)
    keys = set(TARGET_MARKET_FEATURES) & set(a)
    assert keys, "特征字典里应存在黑名单键（否则本检查是空的）"
    for k in sorted(keys):
        assert a[k] == b[k] == 0.0, k


def test_relative_paths_are_posix_under_both_path_flavours():
    # 扫描结果里的相对路径在 POSIX 与 Windows 两种分隔符下必须完全一致。
    for root in (PurePosixPath("/repo"), PureWindowsPath("C:/repo")):
        p = root / "engine" / "prediction" / "lgbm_model.py"
        assert _rel(p, root) == "engine/prediction/lgbm_model.py"
        assert "\\" not in _rel(p, root)


# ---------------------------------------------------------------- 结论 2
# 判定：没有任何代码把开关打开；且按当前 config 的闸构造特征时，
# 目标市场赔率不进入特征。不断言 config 当前字面等于 false。

def test_claim2_no_code_turns_the_odds_gate_on():
    banned = set(GATE_NAMES)
    offenders = []
    for p in _py_files("engine", "scripts", "tests", "evolution"):
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if (isinstance(n, ast.keyword) and n.arg in banned
                    and isinstance(n.value, ast.Constant) and n.value.value is True):
                offenders.append(f"{_rel(p)}:{n.lineno}")
            # AnnAssign 也要看：dataclass 字段默认值
            # （use_odds_features: bool = False）是带注解的赋值。
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    name = (t.id if isinstance(t, ast.Name)
                            else t.attr if isinstance(t, ast.Attribute) else "")
                    if (name in banned and isinstance(n.value, ast.Constant)
                            and n.value.value is True):
                        offenders.append(f"{_rel(p)}:{n.lineno}")
    assert offenders == [], offenders


def test_claim2_configured_gate_keeps_target_odds_out_of_features():
    from engine.prediction.lgbm_model import build_features
    from engine.prediction.market_isolation import TARGET_MARKET_FEATURES
    for rel in ("config/prediction.json", "evolution/demo/base_prediction.json"):
        gate = _read_json(rel)["lgbm"]["use_odds_features"]
        a = build_features(elo_home=1500.0, elo_away=1400.0, odds=(2.0, 3.2, 3.5),
                           include_market_odds=gate)
        b = build_features(elo_home=1500.0, elo_away=1400.0, odds=(1.2, 6.0, 15.0),
                           include_market_odds=gate)
        for k in sorted(set(TARGET_MARKET_FEATURES) & set(a)):
            assert a[k] == b[k] == 0.0, f"{rel}:{k}"


# ---------------------------------------------------------------- 结论 3
# 判定规则：开关为 false ⇒ 该步不得改变概率、不得进 trace（因此审计判定
# 不得高于 info）；开关为 true 才允许它动概率。信号源是参考市场。

def _sina_signal() -> dict:
    return {"movement": {"home": "down", "draw": "flat", "away": "up"},
            "compression": {"home": 1.2, "draw": 1.0, "away": 0.8}}


def test_claim3_switch_off_implies_probabilities_unchanged():
    from engine.prediction.fusion import FusionInput, fuse_probabilities
    post = dict(_read_json("config/prediction.json")["fusion"]["post_fusion"])
    out = fuse_probabilities(FusionInput(
        model_probs=(0.5, 0.3, 0.2), sina_data=_sina_signal(), post_fusion=post))
    fired = any(t["step"] == "sina_odds_movement" for t in out.trace)
    # 规则形式：开关关 ⇒ 不动概率；开关开则不作此约束。
    assert post.get("sina_odds_movement") or (
        not fired and out.probs == (0.5, 0.3, 0.2))


def test_claim3_step_fires_only_when_switch_on():
    # 对照：开关翻开时该步确实会动概率 —— 证明生产不动概率的原因是 config 闸。
    from engine.prediction.fusion import FusionInput, fuse_probabilities
    out = fuse_probabilities(FusionInput(
        model_probs=(0.5, 0.3, 0.2), sina_data=_sina_signal(),
        post_fusion={"sina_odds_movement": True}))
    assert any(t["step"] == "sina_odds_movement" for t in out.trace)
    assert out.probs != (0.5, 0.3, 0.2)


def test_claim3_compression_computed_from_bet365_or_pinnacle():
    # 即使 euro 列表里混进一家"竞彩官方"，取数代码也不会选它。
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
    assert snap["euro"]["company"] in {"Bet365", "Pinnacle"}
    assert snap["euro"]["compression"]["home"] == round(2.1 / 1.8, 3)
    assert snap["euro"]["movement"]["home"] == "down"

    snap2 = mod.extract_odds_snapshot(match, [pinnacle, other], [], [], {})
    assert snap2["euro"]["company"] == "Pinnacle"
    assert snap2["euro"]["compression"]["home"] == round(2.0 / 1.9, 3)


# ---------------------------------------------------------------- 结论 4
# 判定：概率形成不消费 series；注入任意 series 值不改变概率；
# 实际消费者是页面展示（不是"结算验证"）。

def test_claim4_series_built_in_build_sina_data():
    from engine.pipeline.sina_odds import build_sina_data
    # 虚构编号，保证不命中任何真实序列文件 → points=0，纯只读。
    out = build_sina_data({"euro": {}}, "1999-01-01_周一999")
    assert "series" in out
    assert out["series"].get("points") == 0


def test_claim4_fusion_never_consumes_series():
    assert _reads_key(_tree("engine/prediction/fusion.py"), "series") == []


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
    for p in _py_files("engine/review"):
        assert _reads_key(ast.parse(p.read_text(encoding="utf-8")), "series") == [], _rel(p)


def test_claim4_series_consumed_by_build_site_display():
    assert _reads_key(_tree("engine/build_site.py"), "series")


# ---------------------------------------------------------------- 结论 5
# 判定：只读 opening* 字段；站点价（目标市场）不进入特征。

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


def test_claim5_site_price_never_reaches_side_features():
    from engine.sources.njstats_side import SideSignal

    def features(site_fair):
        return SideSignal(home="A", away="B", kickoff="", snapshot_at="",
                          open_fair=(0.4, 0.3, 0.3), site_fair=site_fair).to_features()

    base = features(None)
    assert base, "开盘去水概率应产出侧挂特征"
    assert features((0.5, 0.25, 0.25)) == base
    assert features((0.9, 0.05, 0.05)) == base
    assert not any("site" in k for k in base)


# ---------------------------------------------------------------- 结论 6
# 判定：调用点未传的参数一律有签名默认值；训练与服务在共同输入下
# 特征逐位相同（无实际偏移）。参数个数只进报告，不进断言。

CALL_SITES = ("engine/main.py", "engine/learning/lgbm_shadow.py")


def _build_features_call_kwargs(rel: str) -> set[str]:
    calls = [n for n in ast.walk(_tree(rel))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "build_features"]
    assert calls, f"{rel} 里应存在 build_features 调用"
    passed: set[str] = set()
    for c in calls:
        assert not c.args, f"{rel}:{c.lineno} 用了位置实参，需人工复核对齐"
        passed |= {kw.arg for kw in c.keywords if kw.arg}
    return passed


def test_claim6_omitted_params_all_have_signature_defaults():
    from engine.prediction.lgbm_model import build_features
    params = inspect.signature(build_features).parameters
    for rel in CALL_SITES:
        passed = _build_features_call_kwargs(rel)
        assert passed <= set(params), f"{rel} 传了签名里没有的参数：{passed - set(params)}"
        for name in sorted(set(params) - passed):
            assert params[name].default is not inspect.Parameter.empty, (
                f"{rel} 未传必填参数 {name}")


def test_claim6_training_and_serving_features_do_not_diverge():
    from engine.prediction.lgbm_model import build_features
    params = inspect.signature(build_features).parameters
    gate = _read_json("config/prediction.json")["lgbm"]["use_odds_features"]
    samples = {
        "elo_home": 1500.0, "elo_away": 1400.0, "odds": (2.0, 3.2, 3.5),
        "handicap": -0.5, "home_form": 1.2, "away_form": 0.9,
        "league_avg_goals": 2.6, "league_home_win_rate": 0.44,
        "xg_home": 1.4, "xg_away": 1.1,
        "djyy_probs": {"home": 0.5, "draw": 0.3, "away": 0.2},
        "is_knockout": False, "rank_home": 3, "rank_away": 8,
        "side": {"side_open_home": 0.4}, "include_market_odds": gate,
    }

    def value(name: str):
        if name in samples:
            return samples[name]
        default = params[name].default
        assert default is not inspect.Parameter.empty, (
            f"新增必填参数 {name}：请在 samples 里补一个样例值")
        return default

    serve, train = (_build_features_call_kwargs(rel) for rel in CALL_SITES)
    f_serve = build_features(**{k: value(k) for k in sorted(serve)})
    f_train = build_features(**{k: value(k) for k in sorted(train)})
    assert f_serve == f_train, {
        k: (f_serve[k], f_train[k]) for k in f_serve if f_serve[k] != f_train.get(k)}
