"""CLV 覆盖审计（scripts/clv_coverage_audit.py）的口径测试（2026-11-08）

审计是只读诊断：顶层计数必须与 clv_pinnacle.main 的分类顺序一致，
子成因必须把「可回收」与「结构性缺失」分开，否则提案会把噪声当信号。
全部用 tmp_path 合成数据，不依赖仓库里的真实账本 / fdc 缓存。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import clv_coverage_audit as ca


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _root(tmp_path: Path, bets: list[dict], preds: dict[str, list] | None = None,
          fdc_rows: list[dict] | None = None, zh_en: dict | None = None,
          status: dict | None = None) -> Path:
    ledger = tmp_path / "data" / "state" / "review_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps(b, ensure_ascii=False) + "\n" for b in bets),
                      encoding="utf-8")
    for day, rows in (preds or {}).items():
        _write(tmp_path / "data" / "daily" / day / "predictions.json",
               {"predictions": rows})
    if fdc_rows is not None:
        p = tmp_path / "data" / "state" / "fdc" / "parsed.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in fdc_rows),
                     encoding="utf-8")
    if zh_en:
        _write(tmp_path / "assets" / "team_zh_en.json", {"seed": zh_en})
    if status is not None:
        _write(tmp_path / "data" / "state" / "fdc" / "status.json", status)
    return tmp_path


def _bet(mid="2026-11-01_周日001", date="2026-11-01", league="英超", sel=0):
    return {"match_id": mid, "date": date, "league": league, "best_selection": sel}


def _pred(mid="2026-11-01_周日001", home="阿森纳", away="切尔西", odds=2.0):
    return {"match_id": mid, "home_team": home, "away_team": away,
            "home_odds": odds, "draw_odds": 3.4, "away_odds": 3.9}


def _fdc(date="2026-11-01", src="2627_E0", home="Arsenal", away="Chelsea",
         close=1.9):
    return {"src": src, "date": date, "home_en": home, "away_en": away,
            "b365c_h": close, "b365c_d": 3.5, "b365c_a": 4.0}


ZH_EN = {"阿森纳": "Arsenal", "切尔西": "Chelsea"}


def test_ok_path_counts_match_production_semantics(tmp_path):
    root = _root(tmp_path, [_bet()], {"2026-11-01": [_pred()]},
                 [_fdc()], ZH_EN)
    r = ca.audit(root)
    assert r["stats"] == {"bets": 1, "league_uncovered": 0, "no_en": 0,
                          "no_match": 0, "no_close": 0, "ok": 1}


def test_league_uncovered_breakdown_by_league(tmp_path):
    bets = [_bet(league="欧冠"), _bet(mid="x2", league="欧冠"),
            _bet(mid="x3", league="K1联赛")]
    root = _root(tmp_path, bets)
    r = ca.audit(root)
    assert r["stats"]["league_uncovered"] == 3
    assert r["league_uncovered_by_league"] == {"欧冠": 2, "K1联赛": 1}


def test_no_en_prediction_id_format_is_recoverable(tmp_path):
    # 账本 match_id 缺日期前缀（2026-08-01/02 真实出现过的格式漂移）
    root = _root(tmp_path, [_bet(mid="周日001")],
                 {"2026-11-01": [_pred(mid="2026-11-01_周日001")]},
                 [_fdc()], ZH_EN)
    r = ca.audit(root)
    assert r["stats"]["no_en"] == 1  # 生产口径仍计 no_en
    assert r["no_en_causes"] == {"prediction_id_format": 1}
    assert r["recoverable"]["match_id_format"] == 1
    assert r["samples"]["prediction_id_format"][0]["normalized_match_id"] == \
        "2026-11-01_周日001"


def test_no_en_prediction_row_truly_missing(tmp_path):
    root = _root(tmp_path, [_bet()], {})
    r = ca.audit(root)
    assert r["no_en_causes"] == {"prediction_row_missing": 1}
    assert r["recoverable"]["match_id_format"] == 0


def test_no_en_taken_odds_missing(tmp_path):
    root = _root(tmp_path, [_bet()], {"2026-11-01": [_pred(odds=None)]})
    r = ca.audit(root)
    assert r["stats"]["no_en"] == 1
    assert r["no_en_causes"] == {"taken_odds_missing": 1}


def test_no_en_team_unresolved_lists_names(tmp_path):
    root = _root(tmp_path, [_bet()],
                 {"2026-11-01": [_pred(home="大宫松鼠", away="阿森纳")]},
                 [_fdc()], {"阿森纳": "Arsenal"})
    r = ca.audit(root)
    assert r["no_en_causes"] == {"team_unresolved": 1}
    assert r["unresolved_team_names"] == {"大宫松鼠": 1}
    assert r["recoverable"]["dict_candidate_teams"] == 1
    assert r["recoverable"]["dict_candidate_bets"] == 1


def test_no_match_name_mismatch_reports_closest_row(tmp_path):
    root = _root(tmp_path, [_bet()], {"2026-11-01": [_pred()]},
                 [_fdc(home="Liverpool", away="Everton")], ZH_EN)
    r = ca.audit(root)
    assert r["stats"]["no_match"] == 1
    assert r["no_match_causes"] == {"name_mismatch": 1}
    s = r["samples"]["name_mismatch"][0]
    assert s["closest_fdc_row"] == "Liverpool vs Everton"
    assert s["best_score"] < 0.5


def test_no_match_stale_cache_detected_from_status(tmp_path):
    root = _root(tmp_path, [_bet()], {"2026-11-01": [_pred()]},
                 [_fdc(date="2026-10-20")], ZH_EN,
                 status={"latest_by_src": {"2627_E0": "2026-10-20"}})
    r = ca.audit(root)
    assert r["no_match_causes"] == {"cache_stale_for_date": 1}


def test_no_match_failed_source_file_detected(tmp_path):
    bets = [_bet(league="瑞士超")]
    preds = {"2026-11-01": [_pred(home="年轻人", away="巴塞尔")]}
    root = _root(tmp_path, bets, preds, [_fdc()],
                 {"年轻人": "Young Boys", "巴塞尔": "Basel"},
                 status={"failed": {"2627_SW1": "HTTP 300"}})
    r = ca.audit(root)
    assert r["no_match_causes"] == {"source_file_failed": 1}


def test_no_match_date_shift_is_recoverable(tmp_path):
    # fdc 把比赛记在 ±1 天（时区口径）：同日无行，但次日有同名行
    root = _root(tmp_path, [_bet()], {"2026-11-01": [_pred()]},
                 [_fdc(date="2026-11-02")], ZH_EN,
                 status={"latest_by_src": {"2627_E0": "2026-11-02"}})
    r = ca.audit(root)
    assert r["stats"]["no_match"] == 1
    assert r["recoverable"]["date_shift_no_rows"] == 1
    assert r["recoverable"]["date_shift_name_mismatch"] == 0
    assert r["samples"]["date_shift_recoverable"][0]["fdc_date"] == "2026-11-02"


def test_no_match_name_mismatch_can_also_be_date_shift(tmp_path):
    # 同日有别的比赛（name_mismatch），但 ±1 天能找到同名行：仍是日期漂移
    root = _root(tmp_path, [_bet()], {"2026-11-01": [_pred()]},
                 [_fdc(home="Liverpool", away="Everton"),
                  _fdc(date="2026-11-02")], ZH_EN)
    r = ca.audit(root)
    assert r["no_match_causes"] == {"name_mismatch": 1}
    assert r["recoverable"]["date_shift_name_mismatch"] == 1
    assert r["recoverable"]["date_shift_no_rows"] == 0


def test_fdc_unavailable_degrades_gracefully(tmp_path):
    root = _root(tmp_path, [_bet()], {"2026-11-01": [_pred()]},
                 fdc_rows=None, zh_en=ZH_EN)
    r = ca.audit(root)
    assert r["meta"]["fdc_available"] is False
    assert r["stats"]["no_match"] == 1
    assert r["no_match_causes"] == {"fdc_unavailable": 1}


def test_no_close_counted_separately(tmp_path):
    row = _fdc()
    row.update({"b365c_h": None, "b365c_d": None, "b365c_a": None})
    root = _root(tmp_path, [_bet()], {"2026-11-01": [_pred()]}, [row], ZH_EN)
    r = ca.audit(root)
    assert r["stats"]["no_close"] == 1
    assert r["stats"]["ok"] == 0


def test_audit_is_read_only(tmp_path):
    root = _root(tmp_path, [_bet()], {"2026-11-01": [_pred()]}, [_fdc()], ZH_EN)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    ca.audit(root)
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after


def test_bets_without_selection_are_excluded(tmp_path):
    root = _root(tmp_path, [{"match_id": "x", "date": "2026-11-01",
                             "league": "英超", "best_selection": None}])
    r = ca.audit(root)
    assert r["stats"]["bets"] == 0


def test_translit_folds_diacritics_not_deletes():
    # 生产 _norm 把 Marítimo 删成 "mar timo"；转写应得 Maritimo
    assert ca._translit("Marítimo") == "Maritimo"
    assert ca._translit("Lillestrøm") == "Lillestrom"  # ø 需显式映射
    assert ca._translit("Málaga") == "Malaga"
    assert ca._name_match_translit("Marítimo", "Maritimo") == 1.0
    assert ca._name_match_translit("Lillestrøm", "Lillestrom") == 1.0


def test_what_if_translit_recovers_diacritic_mismatch(tmp_path):
    # 字典解出 "Marítimo"，fdc 写 "Maritimo"：生产 no_match，转写 what-if 可回收
    root = _root(tmp_path, [_bet(league="葡超")],
                 {"2026-11-01": [_pred(home="马里迪莫", away="卡萨皮亚")]},
                 [_fdc(src="2627_P1", home="Maritimo", away="Casa Pia")],
                 {"马里迪莫": "Marítimo", "卡萨皮亚": "Casa Pia"})
    r = ca.audit(root)
    assert r["stats"]["no_match"] == 1          # 生产口径不变
    assert r["no_match_causes"] == {"name_mismatch": 1}
    assert r["recoverable"]["name_translit_only"] == 1
    assert r["recoverable"]["name_translit_plus_alias"] == 0


def test_what_if_alias_needs_flag_and_is_separate_bucket(tmp_path):
    # Depor→La Coruna 是别名（需交叉验证），只在 use_alias 时回收
    root = _root(tmp_path, [_bet(league="西甲")],
                 {"2026-11-01": [_pred(home="拉科鲁尼亚", away="埃尔切")]},
                 [_fdc(src="2627_SP1", home="La Coruna", away="Elche")],
                 {"拉科鲁尼亚": "Depor", "埃尔切": "Elche"})
    r = ca.audit(root)
    assert r["stats"]["no_match"] == 1
    assert r["recoverable"]["name_translit_only"] == 0
    assert r["recoverable"]["name_translit_plus_alias"] == 1


def test_what_if_does_not_inflate_production_stats(tmp_path):
    # what-if 只进 recoverable，绝不改顶层 stats（否则等于改判据）
    root = _root(tmp_path, [_bet(league="葡超")],
                 {"2026-11-01": [_pred(home="马里迪莫", away="卡萨皮亚")]},
                 [_fdc(src="2627_P1", home="Maritimo", away="Casa Pia")],
                 {"马里迪莫": "Marítimo", "卡萨皮亚": "Casa Pia"})
    r = ca.audit(root)
    assert r["stats"]["ok"] == 0
    assert r["stats"]["no_match"] == 1
    assert sum(r["stats"][k] for k in
               ("league_uncovered", "no_en", "no_match", "no_close", "ok")) == \
        r["stats"]["bets"]