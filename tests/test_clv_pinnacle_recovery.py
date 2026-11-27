"""CLV Pinnacle 覆盖率回收 fallback 路径回归测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import clv_pinnacle as cp  # noqa: E402


def _fdc_row(date: str, home: str, away: str, src: str = "2627_E0", close: float = 1.95) -> dict:
    return {
        "src": src,
        "date": date,
        "home_en": home,
        "away_en": away,
        "b365c_h": close,
        "b365c_d": 3.5,
        "b365c_a": 4.0,
    }


def test_lookup_pred_recovers_missing_date_prefix():
    taken_map = {"2026-08-01_周六006": {"home_team": "阿森纳", "home_odds": 2.0}}
    assert cp._lookup_pred(taken_map, "2026-08-01", "2026-08-01_周六006")["home_team"] == "阿森纳"
    assert cp._lookup_pred(taken_map, "2026-08-01", "周六006")["home_team"] == "阿森纳"
    assert cp._lookup_pred(taken_map, "2026-08-01", "周六999") == {}


def test_match_fdc_row_pass1_invariance_and_diacritics_fallback():
    idx = {
        ("2627_E0", "2026-11-01"): [_fdc_row("2026-11-01", "Arsenal", "Chelsea")],
        ("2627_P1", "2026-11-01"): [_fdc_row("2026-11-01", "Maritimo", "Casa Pia", src="2627_P1")],
        ("new_NOR", "2026-11-01"): [_fdc_row("2026-11-01", "Lillestrom", "Rosenborg", src="new_NOR")],
    }
    # Pass 1 原口径精确命中
    r1, s1 = cp._match_fdc_row(["2627_E0"], "2026-11-01", "Arsenal", "Chelsea", idx)
    assert r1 is not None and s1 == 1.0

    # Pass 2 同日重音转写 fallback（Marítimo / Lillestrøm）
    r2, _ = cp._match_fdc_row(["2627_P1"], "2026-11-01", "Marítimo", "Casa Pia", idx)
    assert r2 is not None and r2["home_en"] == "Maritimo"

    r3, _ = cp._match_fdc_row(["new_NOR"], "2026-11-01", "Lillestrøm", "Rosenborg", idx)
    assert r3 is not None and r3["home_en"] == "Lillestrom"


def test_match_fdc_row_plus_minus_one_day_strict_blocks_subset_false_positive():
    idx = {
        ("new_USA", "2026-07-23"): [_fdc_row("2026-07-23", "Inter Miami", "Chicago Fire", src="new_USA")],
        ("new_USA", "2026-07-24"): [_fdc_row("2026-07-24", "Los Angeles Galaxy", "Seattle Sounders", src="new_USA")],
    }
    # +1 天精确匹配应回收
    r_ok, _ = cp._match_fdc_row(["new_USA"], "2026-07-22", "Inter Miami", "Chicago Fire", idx)
    assert r_ok is not None and r_ok["date"] == "2026-07-23"

    # ±1 天子集误配必须被拦截（Los Angeles FC != Los Angeles Galaxy）
    r_bad, _ = cp._match_fdc_row(["new_USA"], "2026-07-23", "Los Angeles FC", "Seattle Sounders", idx)
    assert r_bad is None


def test_clv_pinnacle_main_recovers_all_three_buckets_without_touching_primary(tmp_path: Path, monkeypatch):
    ledger = tmp_path / "review_ledger.jsonl"
    daily = tmp_path / "daily"
    fdc = tmp_path / "parsed.jsonl"
    out = tmp_path / "clv.json"
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "team_zh_en.json").write_text(json.dumps({
        "seed": {
            "阿森纳": "Arsenal", "切尔西": "Chelsea",
            "马里迪莫": "Marítimo", "卡萨皮亚": "Casa Pia",
            "迈阿密国际": "Inter Miami", "芝加哥": "Chicago Fire",
        }
    }, ensure_ascii=False), encoding="utf-8")

    (daily / "2026-08-01").mkdir(parents=True)
    (daily / "2026-08-01" / "predictions.json").write_text(json.dumps([
        {"match_id": "2026-08-01_周六006", "home_team": "阿森纳", "away_team": "切尔西",
         "home_odds": 2.0, "draw_odds": 3.4, "away_odds": 3.8},
    ], ensure_ascii=False), encoding="utf-8")
    (daily / "2026-11-01").mkdir(parents=True)
    (daily / "2026-11-01" / "predictions.json").write_text(json.dumps([
        {"match_id": "2026-11-01_周日001", "home_team": "马里迪莫", "away_team": "卡萨皮亚",
         "home_odds": 2.1, "draw_odds": 3.2, "away_odds": 3.5},
        {"match_id": "2026-11-01_周日002", "home_team": "迈阿密国际", "away_team": "芝加哥",
         "home_odds": 1.8, "draw_odds": 3.6, "away_odds": 4.2},
    ], ensure_ascii=False), encoding="utf-8")

    ledger.write_text("\n".join([
        json.dumps({"match_id": "周六006", "date": "2026-08-01", "league": "英超", "best_selection": 0}, ensure_ascii=False),
        json.dumps({"match_id": "2026-11-01_周日001", "date": "2026-11-01", "league": "葡超", "best_selection": 0}, ensure_ascii=False),
        json.dumps({"match_id": "2026-11-01_周日002", "date": "2026-11-01", "league": "美职联", "best_selection": 0}, ensure_ascii=False),
    ]) + "\n", encoding="utf-8")

    fdc.write_text("\n".join([
        json.dumps(_fdc_row("2026-08-01", "Arsenal", "Chelsea", src="2627_E0", close=1.90)),
        json.dumps(_fdc_row("2026-11-01", "Maritimo", "Casa Pia", src="2627_P1", close=2.00)),
        json.dumps(_fdc_row("2026-11-02", "Inter Miami", "Chicago Fire", src="new_USA", close=1.75)),
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr(cp, "ROOT", tmp_path)
    monkeypatch.setattr(cp, "LEDGER", ledger)
    monkeypatch.setattr(cp, "DAILY", daily)
    monkeypatch.setattr(cp, "FDC", fdc)
    monkeypatch.setattr(cp, "OUT", out)
    assert cp.main() == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["stats"]["ok"] == 3
    assert rep["stats"]["no_en"] == 0
    assert rep["stats"]["no_match"] == 0
