"""主入口 - 每日预测流水线（增强版）

集成模块:
  - Dixon-Coles + Monte Carlo + Ensemble 预测
  - 多市场KL校准 + Shin去水 + 对数意见池
  - 逆向赔率分析（压缩比 + 级联漏斗 + 冷门风险）
  - 同赔历史匹配
  - Wilson信任度 + N维组合挖掘
  - 熔断机制 + CPPI + 三票制资金管理
  - Kelly准则 + 推荐引擎
  - SHA-256不可变决策链
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.sources.manager import SourceManager
from engine.sources.same_odds import SameOddsAnalyzer
from engine.prediction.ensemble import EnsembleModel
from engine.prediction.dixon_coles import DixonColesConfig
from engine.prediction.monte_carlo import MonteCarloConfig
from engine.prediction.base import TeamRating
from engine.prediction.calibration import (
    devig_shin,
    select_devig_method,
    multi_market_calibration,
    MarketOdds,
)
from engine.prediction.reverse_odds import ReverseOddsEngine, ReverseOddsInput
from engine.strategy.kelly import KellyStrategy
from engine.strategy.circuit_breaker import CircuitBreaker
from engine.strategy.three_ticket import ThreeTicketAllocator
from engine.strategy.cppi import CPPIStrategy
from engine.integrity.decision_bundle import DecisionBundle
from engine.integrity.plan_lock import PlanLock
from engine.learning.elo_updater import EloUpdater
from engine.learning.wilson_trust import TrustSystem
from engine.learning.combo_miner import ComboMiner
from engine.learning.online_weights import OnlineWeightLearner
from engine.prediction.lgbm_model import LGBMModel, LGBMConfig, build_features
from engine.prediction.isotonic_cal import IsotonicCalibrator, CalibrationConfig
from engine.learning.league_params import LeagueParamsManager
from engine.storage.match_db import MatchDB
from engine.prediction.htft_model import htft_probabilities, top_htft


def load_config(name: str) -> dict:
    path = ROOT / "config" / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def run_daily_pipeline(target_date: date, predict_only: bool = False):
    """执行每日完整流水线"""
    print(f"{'='*60}")
    print(f"  每日预测流水线 - {target_date.isoformat()}")
    print(f"{'='*60}")

    # 1. 加载配置
    pred_cfg = load_config("prediction")
    strat_cfg = load_config("strategy")

    # 2. 获取数据（三源融合模式: 体彩+500万+DJYY）
    print("\n[1/8] 获取赛程数据（三源融合）...")
    source_mgr = SourceManager(ROOT / "data")
    try:
        fixtures, manifest = source_mgr.fetch_merged_fixtures(target_date)
    except Exception:
        # 融合失败时降级为简单 fallback
        fixtures, manifest = source_mgr.fetch_fixtures(target_date)
    print(f"  ✓ 获取 {len(fixtures)} 场比赛 (来源: {manifest.source})")

    # 2.5 DJYY增强: 获取第三方模型概率 + Pinnacle赔率 + xG
    print("\n[1.5/8] DJYY增强数据...")
    try:
        djyy_enrichment = source_mgr.enrich_from_djyy(fixtures, target_date)
        if djyy_enrichment:
            print(f"  ✓ DJYY增强: {len(djyy_enrichment)}/{len(fixtures)} 场匹配")
        else:
            print(f"  - DJYY无匹配（不影响主流程）")
    except Exception as e:
        djyy_enrichment = {}
        print(f"  - DJYY增强跳过: {e}")

    # 3. 加载球队评级
    print("\n[2/8] 加载球队评级...")
    elo_updater = EloUpdater(ROOT / "data" / "models" / "team_ratings.json")

    # 4. 初始化增强模块
    print("\n[3/8] 初始化增强分析模块...")
    trust_system = TrustSystem()
    combo_miner = ComboMiner(ROOT / "data" / "state" / "combo_stats.json")
    same_odds = SameOddsAnalyzer(ROOT / "data" / "historical" / "odds.csv")
    reverse_engine = ReverseOddsEngine()
    print(f"  ✓ 同赔库 {same_odds.stats_summary()['total_records']} 条记录")

    # LightGBM 第三模型层
    lgbm_cfg = LGBMConfig(**{k: v for k, v in pred_cfg.get("lgbm", {}).items()
                             if k in LGBMConfig.__dataclass_fields__})
    lgbm_model = LGBMModel(ROOT / "data" / "models" / "lgbm_model.txt", config=lgbm_cfg)
    if lgbm_model.is_available:
        print(f"  ✓ LightGBM 已加载")
    else:
        print(f"  - LightGBM 未训练/未安装（跳过第三层）")

    # Isotonic 校准层
    cal_cfg = CalibrationConfig(**{k: v for k, v in pred_cfg.get("calibration", {}).items()
                                   if k in CalibrationConfig.__dataclass_fields__})
    calibrator = IsotonicCalibrator(
        ROOT / "data" / "models" / "isotonic_cal.pkl", config=cal_cfg
    )
    if calibrator.is_fitted:
        print(f"  ✓ Isotonic 校准已加载 (method={calibrator.method_used})")
    else:
        print(f"  - Isotonic 未拟合（原样输出）")

    # 联赛独立参数
    league_mgr = LeagueParamsManager(ROOT / "data" / "state" / "league_params.json")
    # 尝试从 DJYY league-matrix 更新先验
    try:
        matrix = source_mgr.get_league_params()
        if matrix:
            league_mgr.update_from_league_matrix(matrix)
    except Exception:
        pass
    print(f"  ✓ 联赛参数: {len(league_mgr.summary())} 个联赛已配置")

    # MatchDB: 历史xG作为预测辅助
    match_db = MatchDB(ROOT / "data" / "state" / "match_history.db")

    # 5. 预测 + 增强分析
    print("\n[4/8] 运行预测模型 + 增强分析...")
    dc_cfg = DixonColesConfig(**{k: v for k, v in pred_cfg.get("prediction", {}).items()
                                  if k in DixonColesConfig.__dataclass_fields__})
    mc_cfg = MonteCarloConfig(**{k: v for k, v in pred_cfg.get("prediction", {}).items()
                                  if k in MonteCarloConfig.__dataclass_fields__})
    # 在线权重学习: 动态调整模型权重
    weight_learner = OnlineWeightLearner(ROOT / "data" / "state" / "online_weights.json")
    static_weights = pred_cfg.get("ensemble", {"dixon_coles_weight": 0.6, "monte_carlo_weight": 0.4})
    default_w = {
        "dixon_coles": static_weights.get("dixon_coles_weight", 0.6),
        "monte_carlo": static_weights.get("monte_carlo_weight", 0.4),
    }
    dynamic_weights = weight_learner.get_weights(default=default_w)
    print(f"  模型权重: DC={dynamic_weights.get('dixon_coles', 0.6):.3f}, "
          f"MC={dynamic_weights.get('monte_carlo', 0.4):.3f} "
          f"({'动态' if dynamic_weights != default_w else '静态'})")

    model = EnsembleModel(
        dc_config=dc_cfg,
        mc_config=mc_cfg,
        weights=dynamic_weights,
    )

    # 融合参数（可由 param_optimizer 自动调整，不写死）
    fusion_cfg = pred_cfg.get("fusion", {})
    fusion_cfg.setdefault("model_weight", 0.60)
    fusion_cfg.setdefault("market_weight", 0.25)
    fusion_cfg.setdefault("djyy_weight", 0.15)  # DJYY第三方模型权重
    fusion_cfg.setdefault("same_odds_max_adjust", 0.05)
    fusion_cfg.setdefault("same_odds_min_confidence", 0.3)
    fusion_cfg.setdefault("combo_boost_cap", 0.03)
    fusion_cfg.setdefault("trust_shrink_enabled", True)

    # 自我革新: 读取优化器冠军权重覆盖静态默认
    from engine.learning.fusion_optimizer import FusionOptimizer, FusionWeights
    from engine.review.post_match import ReviewLedger
    _ledger = ReviewLedger(ROOT / "data" / "state" / "review_ledger.jsonl")
    _fusion_opt = FusionOptimizer(ROOT / "data" / "state" / "fusion_weights.json", _ledger, pred_cfg.get("optimizer", {}))
    _champion = _fusion_opt.get_champion()
    fusion_cfg["model_weight"] = _champion.model
    fusion_cfg["market_weight"] = _champion.market
    fusion_cfg["djyy_weight"] = _champion.djyy
    print(f"  融合权重(优化器): model={_champion.model:.3f} market={_champion.market:.3f} djyy={_champion.djyy:.3f}")

    predictions = []
    for fixture in fixtures:
        home_rating = elo_updater.get_rating(fixture.home_team)
        away_rating = elo_updater.get_rating(fixture.away_team)

        # DJYY form_xG 修正: 用真实近期xG替代默认ratings
        djyy_pre = djyy_enrichment.get(fixture.match_id, {})
        form_xg = djyy_pre.get("form_xg")
        if form_xg:
            base_goals = pred_cfg.get("prediction", {}).get("base_goals", 1.35)
            if home_rating.attack == 1.0 and form_xg.get("home_avg"):
                home_rating.attack = form_xg["home_avg"] / base_goals
            if away_rating.attack == 1.0 and form_xg.get("away_avg"):
                away_rating.attack = form_xg["away_avg"] / base_goals

        # MatchDB fallback: DJYY无数据时用历史积累xG
        base_goals = pred_cfg.get("prediction", {}).get("base_goals", 1.35)
        if home_rating.attack == 1.0:
            db_xg = match_db.get_team_xg(fixture.home_team, fixture.competition)
            if db_xg and db_xg.get("avg_xg_for"):
                home_rating.attack = db_xg["avg_xg_for"] / base_goals
        if away_rating.attack == 1.0:
            db_xg = match_db.get_team_xg(fixture.away_team, fixture.competition)
            if db_xg and db_xg.get("avg_xg_for"):
                away_rating.attack = db_xg["avg_xg_for"] / base_goals

        # xG校准反馈: 用历史偏差修正联赛级别系统误差
        if fixture.competition:
            cal = match_db.get_xg_calibration(league=fixture.competition, limit=50)
            if cal.get("n", 0) >= 5 and cal.get("avg_pred_total_xg"):
                # factor = 真实xG / 预测xG, >1说明低估, <1说明高估
                factor = cal["avg_actual_total_xg"] / cal["avg_pred_total_xg"]
                factor = max(0.80, min(1.20, factor))  # 防过矫
                if abs(factor - 1.0) > 0.03:  # 偏差>3%才修正
                    home_rating.attack *= factor
                    away_rating.attack *= factor

        # 赛程密度: 休息不足→疲劳惩罚 (attack下降)
        rest = djyy_pre.get("rest_days")
        if rest:
            home_rest = rest.get("home")
            away_rest = rest.get("away")
            # <3天休息: 每少1天扣5%攻击力, 最多扣15%
            if home_rest is not None and home_rest < 3:
                home_rating.attack *= max(0.85, 1.0 - (3 - home_rest) * 0.05)
            if away_rest is not None and away_rest < 3:
                away_rating.attack *= max(0.85, 1.0 - (3 - away_rest) * 0.05)

        # 伤停缺阵: 攻击型球员缺阵→下调attack
        inj = djyy_pre.get("injuries")
        if inj:
            home_miss = inj.get("home_attackers", 0)
            away_miss = inj.get("away_attackers", 0)
            # 每个缺阵攻击手扣4%, 最多扣12%
            if home_miss > 0:
                home_rating.attack *= max(0.88, 1.0 - home_miss * 0.04)
            if away_miss > 0:
                away_rating.attack *= max(0.88, 1.0 - away_miss * 0.04)

        market_odds = None
        if fixture.home_odds and fixture.draw_odds and fixture.away_odds:
            market_odds = (fixture.home_odds, fixture.draw_odds, fixture.away_odds)
        elif djyy_pre.get("pinnacle_odds"):
            # 国内源被WAF挡时, 用DJYY的Pinnacle赔率作为fallback
            po = djyy_pre["pinnacle_odds"]
            if isinstance(po, (list, tuple)) and len(po) >= 3:
                market_odds = (float(po[0]), float(po[1]), float(po[2]))
            elif isinstance(po, dict):
                market_odds = (float(po.get("home", 0)), float(po.get("draw", 0)), float(po.get("away", 0)))

        pred = model.predict(
            home=home_rating,
            away=away_rating,
            market_odds=market_odds,
            handicap=fixture.handicap,
        )
        pred.match_id = fixture.match_id
        pred.competition = fixture.competition

        # --- 增强: Shin去水 + 多市场校准 ---
        calibrated_probs = None
        if market_odds:
            fair_probs = select_devig_method(list(market_odds))
            # 多市场KL校准（如果有让球/大小球赔率）
            if fixture.handicap is not None:
                try:
                    mo = MarketOdds(
                        home_win=market_odds[0],
                        draw=market_odds[1],
                        away_win=market_odds[2],
                    )
                    cal_result = multi_market_calibration(
                        pred.home_xg, pred.away_xg, mo
                    )
                    calibrated_probs = cal_result.get("probs")
                except Exception:
                    pass
            if calibrated_probs is None:
                calibrated_probs = fair_probs

        # --- 增强: 逆向赔率分析 ---
        reverse_result = None
        if market_odds:
            try:
                ri = ReverseOddsInput(
                    had_odds=market_odds,
                    had_odds_initial=market_odds,  # 无初始赔率时用当前
                )
                reverse_result = reverse_engine.analyze(ri)
            except Exception:
                pass

        # --- 增强: 同赔分析 ---
        same_odds_result = None
        if market_odds:
            same_odds_result = same_odds.analyze(
                market_odds[0], market_odds[1], market_odds[2],
                league=fixture.competition,
            )

        # --- 增强: 组合挖掘加分 ---
        features = _extract_features(fixture, pred)
        combo_boost = combo_miner.get_boost(features)

        # --- 增强: Wilson信任度调整 ---
        # 用模型历史命中率（简化: 用confidence作为代理）
        trust_score = trust_system.compute_trust(
            hits=int(pred.confidence * 10),
            total=10,
        )

        # 综合概率（融合: 模型 + 市场校准 + DJYY第三方 + 同赔偏差 + 组合加分）
        # 所有融合参数从 config/prediction.json["fusion"] 读取，可由优化器自动调整
        final_h, final_d, final_a = pred.home_win_prob, pred.draw_prob, pred.away_win_prob

        # 获取DJYY增强数据
        djyy_data = djyy_enrichment.get(fixture.match_id, {})
        djyy_probs = djyy_data.get("model_probs")

        if calibrated_probs and djyy_probs and djyy_probs.get("home"):
            # 三路融合: 自有模型 + 市场校准 + DJYY模型
            mw = fusion_cfg["model_weight"]
            kw = fusion_cfg["market_weight"]
            dw = fusion_cfg["djyy_weight"]
            # 归一化权重（确保总和=1）
            total_w = mw + kw + dw
            mw, kw, dw = mw / total_w, kw / total_w, dw / total_w
            final_h = mw * pred.home_win_prob + kw * calibrated_probs[0] + dw * djyy_probs["home"]
            final_d = mw * pred.draw_prob + kw * calibrated_probs[1] + dw * djyy_probs["draw"]
            final_a = mw * pred.away_win_prob + kw * calibrated_probs[2] + dw * djyy_probs["away"]
        elif calibrated_probs:
            # 两路融合（无DJYY数据时）
            mw = fusion_cfg["model_weight"]
            kw = fusion_cfg["market_weight"]
            total_w = mw + kw
            mw, kw = mw / total_w, kw / total_w
            final_h = mw * pred.home_win_prob + kw * calibrated_probs[0]
            final_d = mw * pred.draw_prob + kw * calibrated_probs[1]
            final_a = mw * pred.away_win_prob + kw * calibrated_probs[2]
        elif djyy_probs and djyy_probs.get("home"):
            # 只有DJYY（无市场赔率时）
            mw = 1.0 - fusion_cfg["djyy_weight"]
            dw = fusion_cfg["djyy_weight"]
            final_h = mw * pred.home_win_prob + dw * djyy_probs["home"]
            final_d = mw * pred.draw_prob + dw * djyy_probs["draw"]
            final_a = mw * pred.away_win_prob + dw * djyy_probs["away"]

        # 同赔偏差微调
        if same_odds_result and same_odds_result.confidence > fusion_cfg["same_odds_min_confidence"]:
            adj_strength = fusion_cfg["same_odds_max_adjust"] * same_odds_result.confidence
            final_h += same_odds_result.home_bias * adj_strength
            final_d += same_odds_result.draw_bias * adj_strength
            final_a += same_odds_result.away_bias * adj_strength

        # 组合挖掘加分
        if combo_boost > 0:
            best_sel = max(
                [("H", final_h), ("D", final_d), ("A", final_a)],
                key=lambda x: x[1],
            )
            boost_amount = min(combo_boost, fusion_cfg["combo_boost_cap"])
            if best_sel[0] == "H":
                final_h += boost_amount
            elif best_sel[0] == "D":
                final_d += boost_amount
            else:
                final_a += boost_amount

        # --- LightGBM 第三层融合 ---
        if lgbm_model.is_available:
            lgbm_weight = fusion_cfg.get("lgbm_weight", 0.10)
            feature_dict = build_features(
                elo_home=home_rating.elo,
                elo_away=away_rating.elo,
                odds=market_odds,
                handicap=fixture.handicap,
                xg_home=getattr(fixture, "_xg_home", None),
                xg_away=getattr(fixture, "_xg_away", None),
                djyy_probs=djyy_probs,
            )
            lgbm_pred = lgbm_model.predict_single(feature_dict)
            if lgbm_pred:
                # 混合: (1-lgbm_weight)*当前 + lgbm_weight*lgbm
                final_h = (1 - lgbm_weight) * final_h + lgbm_weight * lgbm_pred[0]
                final_d = (1 - lgbm_weight) * final_d + lgbm_weight * lgbm_pred[1]
                final_a = (1 - lgbm_weight) * final_a + lgbm_weight * lgbm_pred[2]

        # 归一化
        total_prob = final_h + final_d + final_a
        if total_prob > 0:
            final_h /= total_prob
            final_d /= total_prob
            final_a /= total_prob

        # --- Isotonic 校准（最终修正） ---
        if calibrator.is_fitted:
            final_h, final_d, final_a = calibrator.calibrate((final_h, final_d, final_a))

        # 半全场概率 (基于最终xG)
        _htft = htft_probabilities(pred.home_xg, pred.away_xg)

        # 无真实赔率时, 用融合概率+竞彩返还率生成参考赔率 (供Kelly计算)
        _odds_synthetic = False
        if market_odds is None and final_h > 0 and final_d > 0 and final_a > 0:
            _margin = 0.87
            market_odds = (round(_margin / final_h, 2), round(_margin / final_d, 2), round(_margin / final_a, 2))
            _odds_synthetic = True

        predictions.append({
            "match_id": pred.match_id,
            "competition": pred.competition,
            "home_team": pred.home_team,
            "away_team": pred.away_team,
            # 最终融合概率
            "home_win_prob": round(final_h, 4),
            "draw_prob": round(final_d, 4),
            "away_win_prob": round(final_a, 4),
            # xG
            "home_xg": pred.home_xg,
            "away_xg": pred.away_xg,
            # 市场赔率 (真实或合成)
            "home_odds": market_odds[0] if market_odds else None,
            "draw_odds": market_odds[1] if market_odds else None,
            "away_odds": market_odds[2] if market_odds else None,
            "odds_synthetic": _odds_synthetic,
            "handicap": fixture.handicap,
            # 置信度
            "confidence": round(pred.confidence * trust_score, 4),
            "wilson_trust": round(trust_score, 4),
            # 模型信号分解
            "model_raw": {
                "home": round(pred.home_win_prob, 4),
                "draw": round(pred.draw_prob, 4),
                "away": round(pred.away_win_prob, 4),
            },
            "market_fair": (
                [round(x, 4) for x in calibrated_probs] if calibrated_probs else None
            ),
            # 概率分布（优先DJYY模型，fallback到MC模拟）
            "top_scores": (
                djyy_data.get("top_scores") if djyy_data and djyy_data.get("top_scores")
                else getattr(pred, "top_scores", None)
            ),
            "total_goals": (
                djyy_data.get("totals") if djyy_data and djyy_data.get("totals")
                else getattr(pred, "top_total_goals", None)
            ),
            # 半全场概率
            "htft": _htft,
            "htft_top3": top_htft(_htft),
            # 逆向赔率
            "reverse_upset_risk": (
                reverse_result.direction.upset_risk if reverse_result else None
            ),
            "reverse_direction": (
                reverse_result.direction.label if reverse_result and hasattr(reverse_result.direction, 'label') else None
            ),
            "reverse_compression": (
                round(reverse_result.compression_ratio, 3) if reverse_result and hasattr(reverse_result, 'compression_ratio') else None
            ),
            # 同赔分析
            "same_odds_matched": (
                same_odds_result.matched_count if same_odds_result else 0
            ),
            "same_odds_confidence": (
                round(same_odds_result.confidence, 3) if same_odds_result else 0
            ),
            "same_odds_bias": (
                [round(same_odds_result.home_bias, 3), round(same_odds_result.draw_bias, 3), round(same_odds_result.away_bias, 3)]
                if same_odds_result else None
            ),
            # 组合加分
            "combo_boost": combo_boost,
            # DJYY增强
            "djyy_enriched": bool(djyy_probs and djyy_probs.get("home")),
            "djyy_model_prob": (
                djyy_probs if djyy_probs and djyy_probs.get("home") else None
            ),
            "_djyy_id": djyy_data.get("djyy_id") if djyy_data else None,
            # Elo
            "elo_home": round(home_rating.elo, 1),
            "elo_away": round(away_rating.elo, 1),
        })

    print(f"  ✓ 完成 {len(predictions)} 场预测（含增强分析）")

    # 6. 资金管理 + 投注计划
    print("\n[5/8] 资金管理与投注计划...")

    # 熔断检查
    breaker = CircuitBreaker(ROOT / "data" / "state" / "circuit_breaker.json")
    bankroll = strat_cfg.get("bankroll", 10000)
    breaker_mult = breaker.get_multiplier(bankroll)
    breaker_status = breaker.status_report()
    print(f"  熔断状态: tier={breaker_status['tier']}, "
          f"streak={breaker_status['current_streak']}, "
          f"multiplier={breaker_mult}")

    if breaker_mult == 0:
        print("  ⚠ 熔断停注中，生成观察计划（不实际投注）")

    # 自适应置信阈值（连败收紧）
    conf_threshold = breaker.get_confidence_threshold()
    if conf_threshold > 0:
        print(f"  置信阈值收紧: > {conf_threshold:.2f} (tier={breaker_status['tier']})")

    # CPPI风险预算
    cppi = CPPIStrategy(
        ROOT / "data" / "state" / "cppi.json",
        initial_bankroll=bankroll,
    )
    risk_budget = cppi.get_risk_budget()
    print(f"  CPPI: 安全垫={risk_budget['cushion']}, "
          f"风险预算={risk_budget['risk_exposure']}")

    # Kelly + 三票制
    strategy = KellyStrategy(ROOT / "config" / "strategy.json")
    plan = strategy.evaluate_candidates(predictions)
    plan.date = target_date.isoformat()

    # 三票制重分配
    effective_mult = breaker_mult * min(1.0, risk_budget["cushion_ratio"] * 3)
    allocator = ThreeTicketAllocator(
        bankroll=bankroll,
        breaker_multiplier=effective_mult,
    )
    candidates = []
    filtered_count = 0
    for p in predictions:
        # 自适应置信阈值过滤（连败时收紧）
        if p.get("confidence", 0) < conf_threshold:
            filtered_count += 1
            continue
        for sel, prob, odds_key in [
            ("home", p["home_win_prob"], "home_odds"),
            ("draw", p["draw_prob"], "draw_odds"),
            ("away", p["away_win_prob"], "away_odds"),
        ]:
            odds = p.get(odds_key)
            if odds and prob * odds > 1.0:  # 正期望
                kelly_f = (prob * odds - 1) / (odds - 1) * 0.25  # quarter-Kelly
                candidates.append({
                    "match_id": p["match_id"],
                    "selection": sel,
                    "odds": odds,
                    "prob": prob,
                    "kelly_fraction": kelly_f,
                })
    if filtered_count > 0:
        print(f"  置信过滤: {filtered_count} 场低于阈值 {conf_threshold:.2f}，已跳过")

    ticket_plan = allocator.allocate(candidates)
    print(f"  ✓ 三票方案: 稳胆{len(ticket_plan.stable_picks)}场, "
          f"搏冷{len(ticket_plan.value_picks)}场, "
          f"彩票{len(ticket_plan.lottery_picks)}场, "
          f"总投入={ticket_plan.total_stake}元")

    # 7. 创建决策包 + 锁定
    print("\n[6/8] 创建不可变决策包...")
    bundle_mgr = DecisionBundle(ROOT / "data" / "daily" / target_date.isoformat())
    bundle = bundle_mgr.create(
        date_str=target_date.isoformat(),
        import_manifest=manifest.__dict__,
        predictions=predictions,
        betting_plan={
            "singles": [{"match_id": s.match_id, "selection": s.selection,
                         "stake": s.stake, "odds": s.odds} for s in plan.singles],
            "three_ticket": allocator.summary(ticket_plan),
            "breaker_status": breaker_status,
            "cppi_budget": risk_budget,
            "total_stake": plan.total_stake,
        },
        config_prediction=pred_cfg,
        config_strategy=strat_cfg,
    )
    print(f"  ✓ 决策包 SHA-256: {bundle['bundle_sha256'][:16]}...")

    # 8. 锁定计划
    print("\n[7/8] 锁定计划...")
    if predict_only:
        print("  ⏭ --predict-only 模式，跳过锁定")
    else:
        lock_mgr = PlanLock(ROOT / "data" / "daily" / target_date.isoformat())
        if not lock_mgr.is_locked(target_date.isoformat()):
            import hashlib
            plan_hash = hashlib.sha256(
                json.dumps([s.__dict__ for s in plan.singles], default=str).encode()
            ).hexdigest()
            lock_mgr.lock(
                date_str=target_date.isoformat(),
                plan_hash=plan_hash,
                bundle_hash=bundle["bundle_sha256"],
            )
            print(f"  ✓ 计划已锁定")
        else:
            print(f"  ⚠ 计划已存在锁定，跳过")

    # 保存预测结果
    print("\n[8/8] 保存结果...")
    output_dir = ROOT / "data" / "daily" / target_date.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "predictions.json").write_text(
        json.dumps(predictions, indent=2, ensure_ascii=False)
    )
    (output_dir / "ticket_plan.json").write_text(
        json.dumps(allocator.summary(ticket_plan), indent=2, ensure_ascii=False)
    )

    print(f"\n{'='*60}")
    print(f"  流水线完成 ✓")
    print(f"  预测: {len(predictions)} 场")
    print(f"  投注: {ticket_plan.total_stake} 元 (乘数={effective_mult:.2f})")
    print(f"{'='*60}")

    match_db.close()
    return predictions, plan


def run_settlement(target_date: date):
    """执行结算 + Elo 更新 + 熔断记录 + 组合挖掘更新"""
    print(f"\n{'='*60}")
    print(f"  结算流水线 - {target_date.isoformat()}")
    print(f"{'='*60}")

    source_mgr = SourceManager(ROOT / "data")
    results = source_mgr.fetch_results(target_date)
    if not results:
        print("  ⚠ 无比赛结果")
        return

    # Elo 更新
    print("\n[1/4] Elo 更新...")
    elo_updater = EloUpdater(ROOT / "data" / "models" / "team_ratings.json")
    for r in results:
        elo_updater.update(r.home_team, r.away_team, r.home_score, r.away_score)
        print(f"  {r.home_team} {r.home_score}-{r.away_score} {r.away_team} ✓")
    elo_updater.save()
    print(f"  ✓ Elo 已更新 ({len(results)} 场)")

    # MatchDB: 记录比赛历史 + 积累球队xG
    print("\n[1.5/4] MatchDB 数据积累...")
    pred_cfg = load_config("prediction")
    db = MatchDB(ROOT / "data" / "state" / "match_history.db")
    db_recorded = 0

    # 读取当日预测用于关联
    daily_dir = ROOT / "data" / "daily" / target_date.isoformat()
    predictions = []
    pred_file = daily_dir / "predictions.json"
    if pred_file.exists():
        predictions = json.loads(pred_file.read_text())

    pred_map = {}
    for p in predictions:
        pred_map[f"{p['home_team']}_vs_{p['away_team']}"] = p

    for r in results:
        key = f"{r.home_team}_vs_{r.away_team}"
        pred = pred_map.get(key)

        # 尝试获取DJYY赛后真实xG
        actual_xg = None
        djyy_id = pred.get("_djyy_id") if pred else None

        if djyy_id:
            try:
                actual_xg = source_mgr._djyy.fetch_post_match_xg(djyy_id)
            except Exception:
                pass
            # 存储球员xG (积累关键球员数据)
            try:
                lineups = source_mgr._djyy.fetch_match_lineups(djyy_id)
                if lineups and lineups.get("available"):
                    league = pred.get("competition", "unknown") if pred else "unknown"
                    for side, team in [("home", r.home_team), ("away", r.away_team)]:
                        side_data = lineups.get(side, {})
                        players = []
                        for p in (side_data.get("starting") or []) + (side_data.get("bench") or []):
                            if p.get("xg") is not None:
                                players.append({
                                    "name": p.get("name_zh") or p.get("name"),
                                    "position": p.get("position"),
                                    "xg": p.get("xg"),
                                    "xgot": p.get("xgot"),
                                    "rating": p.get("rating"),
                                    "minutes": p.get("minutes"),
                                })
                        if players:
                            db.record_lineup_xg(team, league, target_date.isoformat(), players)
            except Exception:
                pass

        # 记录到match_history
        if pred:
            db.record_match({
                "match_id": pred.get("match_id", key),
                "date": target_date.isoformat(),
                "league": pred.get("competition"),
                "home_team": r.home_team,
                "away_team": r.away_team,
                "pred_home_prob": pred.get("home_win_prob"),
                "pred_draw_prob": pred.get("draw_prob"),
                "pred_away_prob": pred.get("away_win_prob"),
                "pred_home_xg": pred.get("home_xg"),
                "pred_away_xg": pred.get("away_xg"),
                "pred_top_score": pred.get("top_scores", [])[:1],
                "score_home": r.home_score,
                "score_away": r.away_score,
                "actual_home_xg": actual_xg.get("home_xg") if actual_xg else None,
                "actual_away_xg": actual_xg.get("away_xg") if actual_xg else None,
                "ht_home": actual_xg.get("ht_home") if actual_xg else None,
                "ht_away": actual_xg.get("ht_away") if actual_xg else None,
                "djyy_id": djyy_id,
            })
            db_recorded += 1

        # 更新球队赛季统计（无论有无预测都记录）
        league = pred.get("competition", "unknown") if pred else "unknown"
        home_xg = actual_xg.get("home_xg") if actual_xg else None
        away_xg = actual_xg.get("away_xg") if actual_xg else None

        db.update_team_stats(
            team_name=r.home_team, league=league,
            goals_for=r.home_score, goals_against=r.away_score,
            xg_for=home_xg, xg_against=away_xg,
        )
        db.update_team_stats(
            team_name=r.away_team, league=league,
            goals_for=r.away_score, goals_against=r.home_score,
            xg_for=away_xg, xg_against=home_xg,
        )

    # 同步联赛基线（从DJYY league-matrix）
    try:
        matrix = source_mgr.get_league_params()
        if matrix and isinstance(matrix, list):
            db.sync_league_baselines(matrix)
            print(f"  联赛基线已同步: {len(matrix)} 个联赛")
    except Exception:
        pass

    print(f"  ✓ MatchDB: {db_recorded} 场记录, 球队统计已更新")
    db.close()

    # 熔断 + 逐场结算
    print("\n[2/4] 熔断 + 信任更新...")

    breaker = CircuitBreaker(ROOT / "data" / "state" / "circuit_breaker.json")
    strat_cfg = load_config("strategy")
    bankroll = strat_cfg.get("bankroll", 10000)

    # 读取投注计划
    ticket_file = daily_dir / "ticket_plan.json"
    ticket_data = {}
    if ticket_file.exists():
        ticket_data = json.loads(ticket_file.read_text())

    # 逐场结算
    result_map = {f"{r.home_team}_vs_{r.away_team}": r for r in results}
    total_pnl = 0.0
    wins = 0
    losses = 0

    for pred in predictions:
        key = f"{pred['home_team']}_vs_{pred['away_team']}"
        match_result = result_map.get(key)
        if not match_result:
            continue

        # 判断赛果
        if match_result.home_score > match_result.away_score:
            actual = "home"
        elif match_result.home_score == match_result.away_score:
            actual = "draw"
        else:
            actual = "away"

        # 检查是否命中（基于最大概率选项）
        best_sel = max(
            [("home", pred["home_win_prob"]),
             ("draw", pred["draw_prob"]),
             ("away", pred["away_win_prob"])],
            key=lambda x: x[1],
        )
        won = best_sel[0] == actual

        # 计算PnL（简化: 基于Kelly plan）
        pnl = 0.0
        for s in (ticket_data.get("stable", []) +
                  ticket_data.get("value", []) +
                  ticket_data.get("lottery", [])):
            if s.get("match") == pred["match_id"]:
                if s.get("sel") == actual:
                    pnl += s["stake"] * (s["odds"] - 1)
                else:
                    pnl -= s["stake"]

        total_pnl += pnl
        if won:
            wins += 1
        else:
            losses += 1

        breaker.record_result(won=won, pnl=pnl, bankroll=bankroll)

    print(f"  ✓ 命中 {wins}/{wins+losses}, PnL={total_pnl:.2f}")
    print(f"  熔断状态: {breaker.status_report()}")

    # 在线权重学习反馈
    print("\n[2.5/4] 在线权重学习更新...")
    weight_learner = OnlineWeightLearner(ROOT / "data" / "state" / "online_weights.json")
    for pred in predictions:
        key = f"{pred['home_team']}_vs_{pred['away_team']}"
        match_result = result_map.get(key)
        if not match_result:
            continue
        if match_result.home_score > match_result.away_score:
            actual_idx = 0  # home
        elif match_result.home_score == match_result.away_score:
            actual_idx = 1  # draw
        else:
            actual_idx = 2  # away

        # Brier Score: sum of (prob - actual)^2 for all 3 outcomes
        probs = [pred["home_win_prob"], pred["draw_prob"], pred["away_win_prob"]]
        actuals = [0.0, 0.0, 0.0]
        actuals[actual_idx] = 1.0
        brier = sum((p - a) ** 2 for p, a in zip(probs, actuals))

        best_sel_idx = probs.index(max(probs))
        hit = best_sel_idx == actual_idx

        # 更新ensemble整体表现（后续可扩展为per-model）
        weight_learner.update("ensemble", brier=brier, hit=hit)

    print(f"  ✓ 权重学习已更新: {weight_learner.get_weights()}")

    # 组合挖掘更新
    print("\n[3/4] 组合挖掘更新...")
    combo_miner = ComboMiner(ROOT / "data" / "state" / "combo_stats.json")
    for pred in predictions:
        key = f"{pred['home_team']}_vs_{pred['away_team']}"
        match_result = result_map.get(key)
        if not match_result:
            continue
        if match_result.home_score > match_result.away_score:
            actual = "home"
        elif match_result.home_score == match_result.away_score:
            actual = "draw"
        else:
            actual = "away"

        best_sel = max(
            [("home", pred["home_win_prob"]),
             ("draw", pred["draw_prob"]),
             ("away", pred["away_win_prob"])],
            key=lambda x: x[1],
        )
        won = best_sel[0] == actual

        features = {
            "league": pred.get("competition", "unknown"),
            "prob_band": _prob_band(best_sel[1]),
            "odds_band": _odds_band(pred.get(f"{best_sel[0]}_odds", 2.0)),
        }
        combo_miner.record(features, won=won)
    print(f"  ✓ 组合统计已更新")

    # CPPI 更新
    print("\n[4/4] CPPI 资产更新...")
    cppi = CPPIStrategy(
        ROOT / "data" / "state" / "cppi.json",
        initial_bankroll=bankroll,
    )
    new_bankroll = bankroll + total_pnl
    cppi.update(new_bankroll)
    print(f"  ✓ 资产: {bankroll:.0f} → {new_bankroll:.0f}")

    # 复盘 + 自我革新
    print("\n[5/6] 赛后复盘...")
    from engine.review.post_match import PostMatchReviewer, ReviewLedger
    from engine.learning.fusion_optimizer import FusionOptimizer, FusionWeights

    reviewer = PostMatchReviewer(ROOT / "data", pred_cfg.get("review", {}))
    review_report = reviewer.review_day(target_date.isoformat())
    if review_report.get("n_matches", 0) > 0:
        print(f"  ✓ 复盘: {review_report['n_matches']}场, 命中率{review_report.get('hit_rate', 0):.0%}")
        src_b = review_report.get("source_brier", {})
        print(f"    Brier: model={src_b.get('model', '?')} market={src_b.get('market', '?')} djyy={src_b.get('djyy', '?')} final={src_b.get('final', '?')}")
        for bias in review_report.get("biases", []):
            print(f"    ⚠ 偏差: {bias['dimension']}:{bias['key']} {bias['outcome']} gap={bias['gap']:+.3f}")
    else:
        print(f"  - 无可复盘数据")

    print("\n[6/6] 融合权重优化...")
    ledger = ReviewLedger(ROOT / "data" / "state" / "review_ledger.jsonl")
    fusion_opt = FusionOptimizer(
        ROOT / "data" / "state" / "fusion_weights.json",
        ledger,
        pred_cfg.get("optimizer", {}),
    )
    decision = fusion_opt.step()
    print(f"  决策: {decision.action} | 权重: {decision.champion}")
    print(f"  原因: {decision.reason}")
    if decision.guard_rails_applied:
        print(f"  守卫: {decision.guard_rails_applied}")

    print(f"\n{'='*60}")
    print(f"  结算完成 ✓")
    print(f"{'='*60}")


def _extract_features(fixture, pred) -> dict:
    """从比赛和预测中提取离散特征（用于组合挖掘）"""
    features = {
        "league": fixture.competition or "unknown",
        "prob_band": _prob_band(max(pred.home_win_prob, pred.draw_prob, pred.away_win_prob)),
    }
    if fixture.home_odds:
        features["odds_band"] = _odds_band(fixture.home_odds)
    if fixture.handicap is not None:
        features["handicap"] = str(fixture.handicap)
    return features


def _prob_band(prob: float) -> str:
    """概率分档"""
    if prob >= 0.65:
        return "high"
    elif prob >= 0.45:
        return "mid"
    else:
        return "low"


def _odds_band(odds: float) -> str:
    """赔率分档"""
    if odds < 1.5:
        return "1.0-1.5"
    elif odds < 2.0:
        return "1.5-2.0"
    elif odds < 3.0:
        return "2.0-3.0"
    elif odds < 5.0:
        return "3.0-5.0"
    else:
        return "5.0+"


def main():
    parser = argparse.ArgumentParser(description="Football Engine")
    parser.add_argument("--date", default="today", help="目标日期 (YYYY-MM-DD 或 today)")
    parser.add_argument("--settle", action="store_true", help="执行结算")
    parser.add_argument("--predict-only", action="store_true", help="仅预测不锁定")
    parser.add_argument("--backtest", action="store_true", help="回测历史表现")
    args = parser.parse_args()

    if args.backtest:
        from engine.backtest.runner import BacktestRunner
        runner = BacktestRunner(ROOT / "data")
        report = runner.run()
        print(report.summary())
        # 保存报告
        out = ROOT / "data" / "state" / "backtest_report.json"
        out.write_text(json.dumps({
            "n_matches": report.n_matches,
            "n_days": report.n_days,
            "hit_rate": report.hit_rate,
            "avg_brier": report.avg_brier,
            "roi": report.roi,
            "total_pnl": report.total_pnl,
            "by_league": report.by_league,
            "by_confidence": report.by_confidence,
            "calibration": report.calibration,
            "source_comparison": report.source_comparison,
        }, indent=2, ensure_ascii=False))
        print(f"\n报告已保存: {out}")
        return

    if args.date == "today":
        target = date.today()
    else:
        target = date.fromisoformat(args.date)

    if args.settle:
        run_settlement(target)
    else:
        run_daily_pipeline(target, predict_only=args.predict_only)


if __name__ == "__main__":
    main()
