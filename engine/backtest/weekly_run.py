"""每周回测 + 参数优化入口（workflow: backtest-weekly.yml）

职责:
1. Walk-forward 回测评估当前模型
2. 融合权重优化器走一步（champion/challenger 闭环）
3. 结果写入 data/state/weekly_backtest.json
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    print("=" * 60)
    print("  每周回测 + 参数优化")
    print("=" * 60)

    data_dir = ROOT / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    report = {"date": date.today().isoformat(), "steps": {}}

    # 1. Walk-forward 回测
    try:
        from engine.backtest.walk_forward import WalkForwardEvaluator
        evaluator = WalkForwardEvaluator(data_dir)
        wf_report = evaluator.evaluate()
        # WalkForwardEvaluator 可能返回 dict 或对象，兼容处理
        if isinstance(wf_report, dict):
            report["steps"]["walk_forward"] = {
                k: v for k, v in wf_report.items() if not isinstance(v, (dict, list))
            }
        else:
            report["steps"]["walk_forward"] = {
                "hit_rate": getattr(wf_report, "hit_rate", None),
                "rps": getattr(wf_report, "rps", None),
                "brier": getattr(wf_report, "brier", None),
                "n_matches": getattr(wf_report, "n_matches", None),
            }
        print(f"  ✓ Walk-forward: {json.dumps(report['steps']['walk_forward'], ensure_ascii=False)}")
    except Exception as e:
        print(f"  ⚠ Walk-forward 失败: {e}")
        report["steps"]["walk_forward"] = {"error": str(e)}

    # 2. 融合权重优化（走一步闭环）
    try:
        from engine.review.post_match import ReviewLedger
        from engine.learning.fusion_optimizer import FusionOptimizer

        # 2026-08-05 修复：账本实际文件名是 review_ledger.jsonl（append-only 滚动账本），
        # 之前误写成 .json → 读到 0 键 → 融合优化基于空账本做假决策。
        ledger = ReviewLedger(state_dir / "review_ledger.jsonl")
        # 条件融合参数（2026-08-11）：与生产 main.py 语义一致
        # 2026-08-29 统一：guardrails 完整读 config["optimizer"]，消除 CI 周/日权重震荡
        _opt_cfg = {"djyy_min_confidence": 0.50, "djyy_disagree_penalty": 0.5}
        try:
            _pred_cfg = json.loads((ROOT / "config" / "prediction.json").read_text(encoding="utf-8"))
            _fus = _pred_cfg.get("fusion", {})
            _opt_cfg["djyy_min_confidence"] = _fus.get("djyy_min_confidence", 0.50)
            _opt_cfg["djyy_disagree_penalty"] = _fus.get("djyy_disagree_penalty", 0.5)
            _opt_cfg.update(_pred_cfg.get("optimizer", {}))
        except Exception:
            pass
        opt = FusionOptimizer(state_dir / "fusion_weights.json", ledger, _opt_cfg)
        decision = opt.step()
        report["steps"]["fusion_optimizer"] = {
            "decision": getattr(decision, "action", str(decision)),
            "champion": getattr(decision, "champion", None),
            "message": getattr(decision, "message", None),
        }
        print(f"  ✓ 融合优化: {json.dumps(report['steps']['fusion_optimizer'], ensure_ascii=False)}")
    except Exception as e:
        print(f"  ⚠ 融合优化跳过: {e}")
        report["steps"]["fusion_optimizer"] = {"error": str(e)}

    # 3. 预测时点分桶对比（2026-08-11：哪个时点 Brier 最低/EV 最高 → 决定出手时机）
    try:
        from engine.backtest.horizon_eval import evaluate_by_horizon
        _ledger_path = state_dir / "review_ledger.jsonl"
        _h_records = []
        if _ledger_path.exists():
            for _line in _ledger_path.read_text(encoding="utf-8").strip().split("\n"):
                if not _line.strip():
                    continue
                try:
                    _r = json.loads(_line)
                except json.JSONDecodeError:
                    continue
                # 旧账本无 as_of/kickoff → 交给 evaluate_by_horizon 自动跳过
                _h_records.append({
                    "as_of": _r.get("as_of", ""),
                    "kickoff": _r.get("kickoff", ""),
                    "probs": _r.get("final_prob"),
                    "actual_idx": _r.get("actual_idx"),
                    "ev": _r.get("pnl", 0.0),
                })
        _h_report = evaluate_by_horizon(_h_records).to_dict()
        report["steps"]["horizon_eval"] = _h_report
        print(f"  ✓ 时点分桶: {json.dumps(_h_report, ensure_ascii=False)}")
    except Exception as e:
        print(f"  ⚠ 时点分桶跳过: {e}")
        report["steps"]["horizon_eval"] = {"error": str(e)}

    # 4. 分层收缩 Dixon-Coles 对比（2026-08-11：challenger vs 生产，滚动防泄漏）
    try:
        import sqlite3
        from engine.prediction.shrinkage_dc import (
            ShrinkageDCConfig,
            fit_shrinkage_dc,
            save_model,
        )
        from engine.sources.base import MatchResult
        from engine.backtest.ts_split import brier_score as _brier_score

        _db_path = state_dir / "match_history.db"
        _matches: list[MatchResult] = []
        if _db_path.exists():
            _conn = sqlite3.connect(_db_path)
            _rows = _conn.execute(
                "SELECT match_id, date, home_team, away_team, score_home, score_away "
                "FROM match_history WHERE score_home IS NOT NULL AND score_away IS NOT NULL"
            ).fetchall()
            _conn.close()
            for _m in _rows:
                _matches.append(
                    MatchResult(
                        match_id=str(_m[0]), match_date=str(_m[1]),
                        home_team=str(_m[2]), away_team=str(_m[3]),
                        home_score=int(_m[4]), away_score=int(_m[5]),
                        competition="",
                    )
                )

        # 账本 match_id → 每日预测（取主客队名）
        _pred_map = {}
        for _pf in (data_dir / "daily").glob("*/predictions.json"):
            try:
                for _pr in json.loads(_pf.read_text(encoding="utf-8")):
                    _pred_map[_pr.get("match_id")] = _pr
            except (json.JSONDecodeError, OSError):
                continue

        _ledger_recs = []
        if (state_dir / "review_ledger.jsonl").exists():
            for _line in (state_dir / "review_ledger.jsonl").read_text(encoding="utf-8").strip().split("\n"):
                if _line.strip():
                    try:
                        _ledger_recs.append(json.loads(_line))
                    except json.JSONDecodeError:
                        continue

        # 滚动拟合：每个评估日只用其之前的历史（防未来泄漏）
        _cfg = ShrinkageDCConfig(min_matches=20)
        _dates = sorted({r.get("date", "") for r in _ledger_recs if r.get("date")})
        _shrink_probs: list[tuple[list, int]] = []
        _sample_keys: list[str] = []
        _skipped_dates = 0
        for _d in _dates:
            _hist = [m for m in _matches if str(m.match_date) < _d]
            if len(_hist) < _cfg.min_matches:
                _skipped_dates += 1
                continue
            try:
                _m = fit_shrinkage_dc(_hist, _cfg)
            except (ValueError, RuntimeError):
                _skipped_dates += 1
                continue
            for _r in _ledger_recs:
                if _r.get("date") != _d:
                    continue
                _pr = _pred_map.get(_r.get("match_id", ""))
                if not _pr:
                    continue
                _p = _m.predict_probs(_pr.get("home_team", ""), _pr.get("away_team", ""))
                _shrink_probs.append((list(_p), int(_r.get("actual_idx", 0))))
                _sample_keys.append(_r.get("match_id", ""))

        _sdc_report: dict = {"n_evaluated": len(_shrink_probs), "skipped_dates": _skipped_dates}
        if len(_shrink_probs) >= 30:
            _sdc_brier = float(_brier_score(
                [_p for _p, _ in _shrink_probs], [_a for _, _a in _shrink_probs]
            ))
            _by_key = {k: r for r in _ledger_recs for k in [r.get("match_id", "")]}
            _final_briers = [_by_key[k].get("brier_final", 1.0) for k in _sample_keys if k in _by_key]
            _model_briers = [_by_key[k].get("brier_model") for k in _sample_keys if k in _by_key]
            _market_briers = [_by_key[k].get("brier_market") for k in _sample_keys if k in _by_key]
            def _avg(xs):
                _v = [x for x in xs if x is not None]
                return sum(_v) / len(_v) if _v else 0.0
            _sdc_report.update({
                "shrinkage_dc_brier": round(_sdc_brier, 4),
                "final_brier": round(_avg(_final_briers), 4),
                "model_raw_brier": round(_avg(_model_briers), 4),
                "market_brier": round(_avg(_market_briers), 4),
                "verdict": (
                    "challenger_candidate"
                    if _sdc_brier < _avg(_model_briers) and _sdc_brier < _avg(_final_briers)
                    else "hold"
                ),
            })
        # 全量历史快照（供后续复用/页面展示）
        if len(_matches) >= _cfg.min_matches:
            try:
                _full = fit_shrinkage_dc(_matches, _cfg)
                save_model(_full, state_dir / "shrinkage_dc_model.json")
                _sdc_report["snapshot"] = {
                    "n_matches": _full.n_matches,
                    "teams": len(_full.teams),
                    "rho": round(_full.rho, 4),
                    "fitted_at": _full.fitted_at,
                }
            except (ValueError, RuntimeError) as _e:
                _sdc_report["snapshot"] = {"error": str(_e)}
        report["steps"]["shrinkage_dc"] = _sdc_report
        print(f"  ✓ shrinkage_dc: {json.dumps(_sdc_report, ensure_ascii=False)}")
    except Exception as e:
        print(f"  ⚠ shrinkage_dc 对比跳过: {e}")
        report["steps"]["shrinkage_dc"] = {"error": str(e)}

    # 保存报告
    out = state_dir / "weekly_backtest.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"  ✓ 报告已保存: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
