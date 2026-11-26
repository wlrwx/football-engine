"""LGBM 影子训练与复活评估（2026-08-30 建立；2026-09-18 修正门槛对象）

历史问题（docs/UPGRADE2_20260829.md）：
  1. LGBM 无训练调用点 → is_available 恒 False，从未参与过预测
  2. 若当时直接接通，会用 train/serve 特征不一致的旧特征训练

2026-09-18 修正（用户问"现在能不能训练"后复盘发现）：
  原门槛"洁净样本(chain=v2)≥500 才训练"卡错了对象——被污染的是融合输出
  final_prob，而 LGBM 学的是「冻结特征 → 赛果」，特征(elo/xg/handicap/djyy)
  与标签(actual_idx)在全部历史里同样有效。因此：
  - 训练：用全部已结算样本（门槛降为 min_train_samples=300）
  - 验证：与生产融合概率的对照只在 v2 洁净子集上做（公平基线）；
    与纯市场的对照在全样本做（market_fair 未受污染）
  - ready 需同时显著优于两条基线；生产启用仍需人工翻 config 开关

闭环不变的部分：时间顺序 70/30 切分、配对显著性门槛、状态落盘
data/state/lgbm_status.json、绝不自动进生产。
"""

from __future__ import annotations

import inspect
import json
import math
from pathlib import Path

from engine.prediction.lgbm_model import (
    FEATURE_NAMES,
    build_features,
    feature_dict_to_vector,
)

DEFAULTS = {
    "min_train_samples": 300,   # 全量已结算样本门槛（特征/标签全链有效）
    "holdout_frac": 0.3,
    "early_stop_val_frac": 0.2,
    "min_improvement": 0.002,
    "min_z": 1.96,
}


def _brier(p, a):
    return sum((x - (1.0 if i == a else 0.0)) ** 2 for i, x in enumerate(p))


def _paired(diffs):
    n = len(diffs)
    if n == 0:
        return 0.0, 0.0, 0
    m = sum(diffs) / n
    var = sum((x - m) ** 2 for x in diffs) / max(1, n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    return m, se, n


def build_training_rows(records: list[dict], daily_root: Path,
                        side_index: dict | None = None) -> list[dict]:
    """从每日 predictions.json 重建 (特征, 标签)。

    只用预测时冻结字段，与 serve 端 build_features 同参调用 → 零偏移。
    特征/标签与融合链版本无关，遗留样本同样可用；
    额外标记 _v2（该行 final_prob 是否来自洁净链）供公平对照用。

    侧挂特征与 serve 同源（engine.sources.njstats_side），但多一道 as_of 闸：
    快照晚于当时的预测时刻就丢弃，否则训练集会学到未来才有的价。
    """
    by_date: dict[str, dict[str, dict]] = {}
    rows = []
    try:
        from engine.sources import njstats_side as side_mod
    except Exception:
        side_mod = None
    if side_index is None:
        side_index = side_mod.build_index() if side_mod else {}
    for r in records:
        d = r.get("date", "")
        if d not in by_date:
            pf = daily_root / d / "predictions.json"
            try:
                preds = json.loads(pf.read_text(encoding="utf-8"))
                by_date[d] = {m.get("match_id", ""): m for m in preds}
            except Exception:
                by_date[d] = {}
        p = by_date[d].get(r.get("match_id", ""))
        if not p:
            continue
        try:
            # 2026-10-22：优先回放服务端冻结的输入快照（form/联赛先验/
            # 排名/模型 xG）。旧样本没有该字段时退回当时落盘的列——
            # form_home/away 自 2026-10-09 起有值，home_xg 是模型 xG；
            # 联赛先验与排名旧样本无从追溯，保持 build_features 默认值，
            # 而不是拿今天的 league_matrix / 当前排名去回填历史（那时不可见）。
            inp = p.get("lgbm_inputs") or {}
            sig = (side_index.get(side_mod.key(p.get("home_team", ""), p.get("away_team", "")))
                   if side_mod else None)
            feats = build_features(
                elo_home=float(p.get("elo_home") or 1500),
                elo_away=float(p.get("elo_away") or 1500),
                handicap=p.get("handicap"),
                home_form=float(inp.get("form_home", p.get("form_home") or 0.0) or 0.0),
                away_form=float(inp.get("form_away", p.get("form_away") or 0.0) or 0.0),
                league_avg_goals=float(inp.get("league_avg_goals") or 2.5),
                league_home_win_rate=float(inp.get("league_home_win_rate") or 0.45),
                xg_home=inp.get("xg_home", p.get("home_xg")),
                xg_away=inp.get("xg_away", p.get("away_xg")),
                rank_home=inp.get("rank_home"),
                rank_away=inp.get("rank_away"),
                djyy_probs=p.get("djyy_model_prob"),
                include_market_odds=False,
                side=sig.features_as_of(p.get("as_of", "")) if sig else None,
            )
        except Exception:
            continue
        rows.append({"features": feats, "label": int(r["actual_idx"]),
                     "final_prob": list(r.get("final_prob") or []),
                     "market_fair": (list(r["market_fair"]) if r.get("market_fair") else None),
                     "match_id": r.get("match_id", ""),
                     "_v2": r.get("chain") == "v2"})
    return rows


def shadow_train(all_records: list[dict], clean_records: list[dict],
                 daily_root: Path, model_path: Path,
                 lgbm_cfg=None, config: dict | None = None,
                 trainer=None) -> dict:
    """全样本训练 + 公平双基线影子验证。返回 status dict（调用方落盘）。"""
    cfg = {**DEFAULTS, **(config or {})}
    clean_ids = {r.get("match_id") for r in clean_records}
    status = {
        "trained": False, "ready": False,
        "all_n": len(all_records), "clean_n": len(clean_records),
        "usable_rows": 0, "holdout_n": 0,
        "holdout_brier_lgbm": None,
        "holdout_brier_fusion_v2": None, "delta_vs_fusion_v2": None, "t_vs_fusion_v2": None,
        "holdout_brier_market": None, "delta_vs_market": None, "t_vs_market": None,
        "reason": "", "trained_at": None,
    }

    # 污染样本隔离（2026-10-11）：训练集来自 all_records，其中混着 20 场
    # 已修 bug 的残留（league_draw_baseline 把巴甲平局概率抬到 0.5025）。
    # 污染样本若进训练集会教 LGBM "平局给 0.5"，而真实巴甲平局率是 0.266。
    # clean_records 已由调用方过滤，这里按同样口径再挡一次训练集。
    try:
        from engine.integrity.quarantine import contaminated_ids as _qids
        _bad_ids = _qids(all_records, daily_root.parent.parent)
    except Exception:
        _bad_ids = set()
    _rows_all = build_training_rows(all_records, daily_root)
    rows = [r for r in _rows_all if r["match_id"] not in _bad_ids]
    for r in rows:
        r["_v2"] = r["match_id"] in clean_ids
    if _bad_ids:
        status["quarantined_rows"] = len(_bad_ids)
    status["usable_rows"] = len(rows)
    min_n = int(cfg["min_train_samples"])
    if len(rows) < min_n:
        need = min_n - len(rows)
        status["reason"] = f"可用样本 {len(rows)} < {min_n}，按 ~15 场/天约 {need // 15 + 1} 天后达标"
        return status

    try:
        if trainer is None:
            from engine.prediction.lgbm_model import LGBMModel
            trainer = LGBMModel(model_path, config=lgbm_cfg)
        import numpy as np

        # 2026-09-21: 用 FEATURE_NAMES 权威列序（与 predict_single 内部一致），
        # 缺键以 0.0 容错——避免首行键集与后续行不一致时 KeyError 整轮失败
        keys = list(FEATURE_NAMES)
        use_nan = bool(getattr(lgbm_cfg, "missing_as_nan", True))

        def _matrix(rs):
            X = np.array([
                feature_dict_to_vector(r["features"], keys, missing_as_nan=use_nan)
                for r in rs
            ])
            y = np.array([r["label"] for r in rs])
            return X, y

        # 严格三段时序切分：train → early-stopping val → 纯样本外 holdout
        # 禁止把 holdout (Xho, yho) 传入 early stopping，避免评估泄漏。
        hold_split = int(len(rows) * (1 - float(cfg["holdout_frac"])))
        pre_hold, hold_rows = rows[:hold_split], rows[hold_split:]
        es_frac = float(cfg.get("early_stop_val_frac", 0.2))
        if len(pre_hold) >= 100 and 0.0 < es_frac < 0.5:
            es_split = int(len(pre_hold) * (1 - es_frac))
            train_rows, val_rows = pre_hold[:es_split], pre_hold[es_split:]
            Xtr, ytr = _matrix(train_rows)
            Xval, yval = _matrix(val_rows)
            trainer.train(Xtr, ytr, eval_features=Xval, eval_labels=yval)
        else:
            Xtr, ytr = _matrix(pre_hold)
            trainer.train(Xtr, ytr)
        Xho, yho = _matrix(hold_rows)

        # 影子验证：holdout 上 lgbm vs 双基线
        lb, fb_v2, mb = [], [], []
        d_fus, d_mkt = [], []
        for r in hold_rows:
            pred = trainer.predict_single(r["features"])
            if not pred:
                continue
            pl = list(pred)[:3]
            s = sum(pl)
            if s <= 0:
                continue
            pl = [v / s for v in pl]
            b_l = _brier(pl, r["label"])
            lb.append(b_l)
            if r["_v2"] and len(r["final_prob"]) == 3:
                b_f = _brier(r["final_prob"], r["label"])
                fb_v2.append(b_f)
                d_fus.append(b_f - b_l)
            if r.get("market_fair"):
                b_m = _brier(r["market_fair"], r["label"])
                mb.append(b_m)
                d_mkt.append(b_m - b_l)
        status["holdout_n"] = len(lb)
        status["holdout_brier_lgbm"] = round(sum(lb) / len(lb), 4) if lb else None
        mf = _paired(d_fus)
        mm = _paired(d_mkt)
        status["holdout_brier_fusion_v2"] = round(sum(fb_v2) / len(fb_v2), 4) if fb_v2 else None
        status["holdout_brier_market"] = round(sum(mb) / len(mb), 4) if mb else None
        status["delta_vs_fusion_v2"] = round(mf[0], 5)
        status["t_vs_fusion_v2"] = round(mf[0] / mf[1], 2) if mf[1] > 0 else 0.0
        status["delta_vs_market"] = round(mm[0], 5)
        status["t_vs_market"] = round(mm[0] / mm[1], 2) if mm[1] > 0 else 0.0

        thr_f = max(float(cfg["min_improvement"]), float(cfg["min_z"]) * mf[1])
        thr_m = max(float(cfg["min_improvement"]), float(cfg["min_z"]) * mm[1])
        ok_f = mf[2] >= 30 and mf[0] > thr_f
        ok_m = mm[2] >= 30 and mm[0] > thr_m
        if ok_f and ok_m:
            status["ready"] = True
            status["reason"] = (f"影子验证双基线均显著优于 (vs融合v2 Δ={mf[0]:+.4f} t={status['t_vs_fusion_v2']}; "
                                f"vs市场 Δ={mm[0]:+.4f} t={status['t_vs_market']}) → 具备人工评估启用条件")
        else:
            miss = []
            if not ok_f:
                miss.append(f"vs融合v2 Δ={mf[0]:+.4f}(n={mf[2]})")
            if not ok_m:
                miss.append(f"vs市场 Δ={mm[0]:+.4f}(n={mm[2]})")
            status["reason"] = "影子验证未双基线显著 → 保持关闭: " + " | ".join(miss)

        # 生产模型 = 全量重训（供 ready 后启用时使用；按早停最优迭代数封顶防过拟合）
        Xall, yall = _matrix(rows)
        best_iter = getattr(trainer, "best_iteration_", None)
        refit_kw: dict = {}
        if isinstance(best_iter, int) and best_iter > 0:
            try:
                sig = inspect.signature(trainer.train)
                if "n_estimators_override" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()
                ):
                    refit_kw["n_estimators_override"] = best_iter
            except (ValueError, TypeError):
                pass
        trainer.train(Xall, yall, **refit_kw)
        try:
            trainer.save()
        except Exception:
            pass
        status["trained"] = True
        from datetime import datetime
        status["trained_at"] = datetime.now().isoformat(timespec="seconds")
    except ImportError as e:
        status["reason"] = f"环境缺依赖（{e}），训练推迟到 CI 侧执行"
    except Exception as e:
        status["reason"] = f"训练异常: {e}"
    return status
