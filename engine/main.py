from __future__ import annotations
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
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.beijing_time import BEIJING_TZ, beijing_now, beijing_today
from engine.sources.manager import SourceManager
from engine.odds_series import series_features
from engine.sources.base import MatchResult
from engine.sources.same_odds import SameOddsAnalyzer
from engine.prediction.ensemble import EnsembleModel
from engine.prediction.dixon_coles import DixonColesConfig
from engine.prediction.monte_carlo import MonteCarloConfig
from engine.prediction.calibration import select_devig_method
from engine.prediction.fusion import (
    FusionInput,
    fuse_probabilities,
    LEAGUE_DRAW_ANCHOR,
    DRAW_ANCHOR_W,
)
from engine.prediction.reverse_odds import ReverseOddsEngine, ReverseOddsInput
from engine.strategy.kelly import KellyStrategy, favorite_band_factor, market_signal_gate
from engine.strategy.circuit_breaker import CircuitBreaker
from engine.strategy.three_ticket import ThreeTicketAllocator
from engine.strategy.cppi import CPPIStrategy
from engine.integrity.decision_bundle import DecisionBundle
from engine.integrity.plan_lock import PlanLock
from engine.learning.elo_updater import EloUpdater
from engine.learning.wilson_trust import TrustSystem
from engine.learning.combo_miner import ComboMiner

# 判平强度由数据驱动（2026-08-05 结构升级）：不硬编码联赛白名单，
# 而是每个联赛维护判平反馈(draw_predictions/draw_hits)，draw_strength() 输出连续强度：
#   反馈足且准(巴甲 6/10、美职联 6/11) → 强抬升 0.85
#   样本少/不准(瑞典超 0/2、巴西杯 0/1) → 温和试探 0.35（不清零，继续积累反馈）
#   无反馈 → 温和 0.40
# 判平错误 → 强度自动下降，等反馈证明后再升，而不是关闭联赛
from engine.learning.online_weights import OnlineWeightLearner
from engine.prediction.lgbm_model import LGBMModel, LGBMConfig, build_features
from engine.prediction.isotonic_cal import IsotonicCalibrator, CalibrationConfig
from engine.prediction.temperature_scaling import TemperatureScaler
from engine.prediction.rho_fitter import RhoFitter
from engine.learning.league_params import LeagueParamsManager
from engine.learning.fusion_optimizer import FusionOptimizer
from engine.storage.match_db import MatchDB
from engine.prediction.htft_model import htft_probabilities, top_htft
from engine.prediction.enhanced import total_goals_from_xg
from engine.team_aliases import normalize_team, loose_normalize

# 2026-08-14 重构：纯工具/步骤抽到 engine.pipeline（helpers/sina_odds）。
# 保持本模块名字可用（外部 `from engine.main import load_config` 不受影响）。
from engine.pipeline.helpers import (  # noqa: E402
    _canon_league,
    _extract_features,
    _odds_band,
    _pick_direction,
    _prob_band,
    load_config,
)


# 高平局联赛名单（2026-08-12 账本实证 + 2026-08-12 二次复核）
# 入选标准：联赛整体平局率>=40% 且 R1 触发区间（市场平局P∈[0.20,0.30)）实际平局率>=40%
# 账本实证（193场，别名已归一化）：美职联 55%(11场) / 巴甲 50%(14场) / 葡超 56%(9场) / 芬超 26%(19场，但触发区间 44%)
# 瑞典超已移除（2026-08-12 复核）：合并瑞超后 23场仅30%，R1 触发区间仅 20%（10场2平）→ 无脑改判有害
#   回测：含瑞典超 44.6% → 移除后 45.6%（+1.0pp），判平质量 45% → 53%
# 注意：此名单为静态配置，随账本增长需人工复核更新（防止用未来数据泄漏）
HIGH_DRAW_LEAGUES = frozenset({"芬超", "美职联", "巴甲", "葡超"})

# 联赛平局率锚定表 + 融合后处理链：2026-08-29 迁移到 engine/prediction/fusion.py
# （LEAGUE_DRAW_ANCHOR / DRAW_ANCHOR_W / fuse_probabilities，每步独立开关 + trace）


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

    # 关键过滤：只预测"竞彩编号所属比赛日"== target_date 的场次
    # 竞彩跨日开售（周五开售周六/周日比赛），match_id 前缀 = 编号推断的比赛日。
    # 不过滤会导致同一场比赛出现在多个日期的预测页 + 复盘互相污染（历史痛点）。
    _before = len(fixtures)
    fixtures = [f for f in fixtures if f.match_id.startswith(target_date.isoformat())]
    _dropped = _before - len(fixtures)
    if _dropped:
        print(f"  ⏭ 跨日场次过滤: 丢弃 {_dropped} 场非 {target_date} 比赛日的场次")

    # 再过滤：开球时间已过的场次（防止把已开赛/已结束的比赛当未来场次预测）
    # 竞彩开球时间是北京时间，runner 的 datetime.now() 是 UTC，必须统一到北京时间再比较。
    _now = beijing_now()
    _still = []
    for f in fixtures:
        _ko = (f.kickoff or "").strip()
        if _ko:
            try:
                _ko_dt = datetime.fromisoformat(_ko.replace(" ", "T"))
                if _ko_dt.tzinfo is None:
                    _ko_dt = _ko_dt.replace(tzinfo=BEIJING_TZ)
                if _ko_dt <= _now:
                    print(f"  ⏭ 已开赛场次跳过: {f.match_id} ({_ko})")
                    continue
            except ValueError:
                pass
        _still.append(f)
    fixtures = _still

    if not fixtures:
        print("  ⚠ 今日无待预测场次（可能无在售比赛或全部已开赛）")
        return [], None

    # 2.5 DJYY增强: 获取第三方模型概率 + Pinnacle赔率 + xG
    print("\n[1.5/8] DJYY增强数据...")
    try:
        djyy_enrichment = source_mgr.enrich_from_djyy(fixtures, target_date)
        if djyy_enrichment:
            print(f"  ✓ DJYY增强: {len(djyy_enrichment)}/{len(fixtures)} 场匹配")
        else:
            print("  - DJYY无匹配（不影响主流程）")
    except Exception as e:
        djyy_enrichment = {}
        print(f"  - DJYY增强跳过: {e}")

    # 2.5b DJYY SSR 真实数据源 (赛前xG + Pinnacle赔率)
    from engine.sources.djyy_ssr import DJYYSSRSource
    djyy_ssr = DJYYSSRSource(ROOT / "data" / "djyy_matches.json")
    djyy_ssr_enriched = 0
    print(f"  DJYY SSR: {len(djyy_ssr.matches)} 场比赛数据可用")

    # 3. 加载球队评级
    print("\n[2/8] 加载球队评级...")
    elo_updater = EloUpdater(ROOT / "data" / "models" / "team_ratings.json")

    # 3.5 数据新鲜度护栏（2026-08-06 借鉴 MBS 概率系统）
    # 长时间无正式比赛（季前赛/杯赛间歇/跨联赛）→ 概率向均势收缩，避免旧状态当新状态
    from engine.learning.freshness import FreshnessTracker
    freshness_tracker = FreshnessTracker(
        ROOT / "data" / "state" / "match_history.db",
        ROOT / "data" / "league_matrix.json",
    )
    freshness_active = 0

    # 3.6 E 规则（2026-08-06）：高置信反向样本 ≥2 场的联赛 → 60%+ 段降一档
    try:
        from engine.review.high_conf_reversals import league_risk
        _hcr_league_risk = league_risk(ROOT / "data" / "state" / "high_conf_reversals.jsonl", min_samples=2)
        if _hcr_league_risk:
            print(f"  ⚠ 高置信反向风险联赛: {_hcr_league_risk} → 60%+ 段降一档")
    except Exception:
        _hcr_league_risk = {}

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
        print("  ✓ LightGBM 已加载")
    else:
        print("  - LightGBM 未训练/未安装（跳过第三层）")

    # Isotonic 校准层
    cal_cfg = CalibrationConfig(**{k: v for k, v in pred_cfg.get("calibration", {}).items()
                                   if k in CalibrationConfig.__dataclass_fields__})
    calibrator = IsotonicCalibrator(
        ROOT / "data" / "models" / "isotonic_cal.pkl", config=cal_cfg
    )
    if calibrator.is_fitted:
        print(f"  ✓ Isotonic 校准已加载 (method={calibrator.method_used})")
    else:
        print("  - Isotonic 未拟合（原样输出）")

    # Temperature Scaling 校准层（在 Isotonic 之后应用）
    temp_scaler = TemperatureScaler(ROOT / "data" / "models" / "temperature.json")
    if temp_scaler.is_fitted:
        print(f"  ✓ Temperature Scaling 已加载 (T={temp_scaler.temperature_value:.3f})")
    else:
        print("  - Temperature Scaling 未拟合（跳过）")

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
    fusion_cfg.setdefault("model_weight", 0.10)   # 多源时模型权重（2026-08-12 回测: 0.26→0.10 全量Brier 0.6494→0.6459）
    fusion_cfg.setdefault("market_weight", 0.60)  # 市场主导（回测: 市场是最强单源 0.6339）
    fusion_cfg.setdefault("djyy_weight", 0.30)    # DJYY第三方模型权重（条件融合后才生效）
    fusion_cfg.setdefault("djyy_min_confidence", 0.50)  # DJYY模型置信门槛（2026-08-11 回测: <0.5 是负贡献）
    fusion_cfg.setdefault("djyy_disagree_penalty", 0.5)  # 与市场分歧时权重减半（回测: 分歧时市场 48.9% vs 模型 19.1%）
    fusion_cfg.setdefault("same_odds_max_adjust", 0.05)
    fusion_cfg.setdefault("same_odds_min_confidence", 0.3)
    fusion_cfg.setdefault("combo_boost_cap", 0.03)
    fusion_cfg.setdefault("trust_shrink_enabled", True)

    # 加载新浪赔率数据（初始+即时+变化历史）——逻辑抽到
    # engine.pipeline.sina_odds.load_sina_odds_map（2026-08-14 重构）
    from engine.pipeline.sina_odds import load_sina_odds_map
    sina_odds_map, sina_odds_by_no, _sina_n = load_sina_odds_map(target_date.isoformat(), ROOT)
    if _sina_n:
        print(f"  ✓ 新浪赔率: {_sina_n} 场 (编号匹配: {len(sina_odds_by_no)})")
    else:
        print("  - 新浪赔率: 无（缺失或解析失败，走其他源）")

    # 自我革新: 读取优化器冠军权重覆盖静态默认
    from engine.learning.fusion_optimizer import FusionOptimizer
    from engine.review.post_match import ReviewLedger
    _ledger = ReviewLedger(ROOT / "data" / "state" / "review_ledger.jsonl")
    _opt_cfg = dict(pred_cfg.get("optimizer", {}))
    # 把条件融合门槛传给优化器，保证反事实评估与生产语义一致（2026-08-11）
    _opt_cfg.setdefault("djyy_min_confidence", fusion_cfg.get("djyy_min_confidence", 0.50))
    _opt_cfg.setdefault("djyy_disagree_penalty", fusion_cfg.get("djyy_disagree_penalty", 0.5))
    _fusion_opt = FusionOptimizer(ROOT / "data" / "state" / "fusion_weights.json", _ledger, _opt_cfg)
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

        # 查找新浪赔率数据（优先用竞彩编号匹配，fallback用队名）
        _sina_data = None
        # 从 match_id 提取竞彩编号: "2026-08-01_周六001" → "周六001"
        _match_no = fixture.match_id.split("_", 1)[-1] if "_" in fixture.match_id else ""
        _sina_match = sina_odds_by_no.get(_match_no) if _match_no else None
        if not _sina_match:
            # fallback: 队名精确匹配
            _sina_match = sina_odds_map.get((fixture.home_team, fixture.away_team))
        if not _sina_match:
            # fallback: 队名归一化匹配（译名变体，如 奥林匹亚 vs 奥林匹亚科斯）
            _hk_n, _ak_n = normalize_team(fixture.home_team), normalize_team(fixture.away_team)
            for (_sh, _sa), _v in sina_odds_map.items():
                if (normalize_team(_sh), normalize_team(_sa)) == (_hk_n, _ak_n):
                    _sina_match = _v
                    break
        if not _sina_match:
            # fallback: 模糊匹配
            import re as _re
            def _norm_name(s):
                s = s.replace("FC", "").replace("队", "").replace("市", "")
                s = _re.sub(r'[^\u4e00-\u9fffa-zA-Z]', '', s)
                return s.strip()
            _ht = _norm_name(fixture.home_team)
            _at = _norm_name(fixture.away_team)
            for (sh, sa), v in sina_odds_map.items():
                _sh = _norm_name(sh)
                _sa = _norm_name(sa)
                if (_ht and _sh and (_ht in _sh or _sh in _ht)) and \
                   (_at and _sa and (_at in _sa or _sa in _at)):
                    _sina_match = v
                    break
        if _sina_match:
            _sina_data = {
                "initial_odds": _sina_match.get("euro", {}).get("initial"),
                "current_odds": _sina_match.get("euro", {}).get("current"),
                "movement": _sina_match.get("euro", {}).get("movement"),
                "compression": _sina_match.get("euro", {}).get("compression"),
                "odds_history_count": len(_sina_match.get("odds_history", [])),
                "asia": _sina_match.get("asia"),
                "totals": _sina_match.get("totals"),
                "match_time": _sina_match.get("match_time"),
                # 水位时间序列特征（2026-08-05 盘口系统修复）：
                # 由 data/state/odds_series/ 累积快照计算，识别赛前资金流斜率/加速。
                # 特征存 predictions 供结算验证命中率，不预设有效/无效。
                "series": series_features(fixture.match_id),
            }

        market_odds = None
        if fixture.home_odds and fixture.draw_odds and fixture.away_odds:
            # 验证: 真实十进制赔率必须 > 1.0 (概率才 < 1.0)
            if all(o > 1.0 for o in (fixture.home_odds, fixture.draw_odds, fixture.away_odds)):
                market_odds = (fixture.home_odds, fixture.draw_odds, fixture.away_odds)
        if market_odds is None and djyy_pre.get("pinnacle_odds"):
            # 国内源被WAF挡时, 用DJYY的Pinnacle赔率作为fallback
            po = djyy_pre["pinnacle_odds"]
            _po_vals = None
            if isinstance(po, (list, tuple)) and len(po) >= 3:
                _po_vals = (float(po[0]), float(po[1]), float(po[2]))
            elif isinstance(po, dict):
                _po_vals = (float(po.get("home", 0)), float(po.get("draw", 0)), float(po.get("away", 0)))
            # DJYY有时返回概率(0-1)而非赔率(>1), 需验证
            if _po_vals and all(o > 1.0 for o in _po_vals):
                market_odds = _po_vals

        pred = model.predict(
            home=home_rating,
            away=away_rating,
            market_odds=market_odds,
            handicap=fixture.handicap,
        )
        pred.match_id = fixture.match_id
        # 联赛名归一化（2026-08-12）：瑞超→瑞典超、韩职→K1联赛，保证 R1/锚定/账本口径统一
        pred.competition = _canon_league(fixture.competition)

        # --- 增强: Shin去水 ---
        # 2026-08-29 清理：原"多市场KL校准"调用因 MarketOdds 字段不匹配
        # （home_win/draw/away_win vs had/hhad/...）静默 TypeError，
        # 从上线起就是死代码 → 删除，保留 Shin 去水。
        calibrated_probs = None
        if market_odds:
            calibrated_probs = select_devig_method(list(market_odds))

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
        # 2026-08-29 重构：融合+后处理链抽到 engine/prediction/fusion.py（纯函数，
        # 每步独立 post_fusion 开关 + trace 随 predictions.json 落盘可归因）。
        # 所有融合参数从 config/prediction.json["fusion"] 读取，可由优化器自动调整。
        djyy_data = djyy_enrichment.get(fixture.match_id, {})
        djyy_probs = djyy_data.get("model_probs")

        # LGBM 第三层（特征构建依赖 fixture/ratings 留在 main，掺混在 fusion.py）
        lgbm_probs = None
        if lgbm_model.is_available:
            feature_dict = build_features(
                elo_home=home_rating.elo,
                elo_away=away_rating.elo,
                odds=market_odds,
                handicap=fixture.handicap,
                xg_home=getattr(fixture, "_xg_home", None),
                xg_away=getattr(fixture, "_xg_away", None),
                djyy_probs=djyy_probs,
                include_market_odds=lgbm_cfg.use_odds_features,
            )
            _lgbm_pred = lgbm_model.predict_single(feature_dict)
            if _lgbm_pred:
                lgbm_probs = (_lgbm_pred[0], _lgbm_pred[1], _lgbm_pred[2])

        # 数据新鲜度护栏（2026-08-06）：长休赛 → 概率向联赛基线收缩。
        # 收缩发生在链条中间（温度校准之后），故以闭包传入 fusion。
        _fresh = freshness_tracker.evaluate(fixture.home_team, fixture.away_team, target_date)
        _fresh_fn = None
        if _fresh.shrink > 0:
            _baseline = freshness_tracker.league_baseline(fixture.competition)
            _fresh_fn = lambda p, _s=_fresh.shrink, _b=_baseline: tuple(
                freshness_tracker.apply(list(p), _s, _b)
            )
            freshness_active += 1
            if _fresh.risk == "alert":
                print(f"  ⚠ 新鲜度预警 [{fixture.home_team} {_fresh.home_days}天 / "
                      f"{fixture.away_team} {_fresh.away_days}天] 概率收缩 {_fresh.shrink:.0%} "
                      f"→ {'联赛基线' if _baseline else '均势'}")

        _league_db = league_mgr.get_draw_baseline(fixture.competition) if league_mgr else 0.25
        _draw_str = league_mgr.get_draw_strength(fixture.competition) if league_mgr else 0.0

        _fusion_out = fuse_probabilities(FusionInput(
            model_probs=(pred.home_win_prob, pred.draw_prob, pred.away_win_prob),
            market_probs=tuple(calibrated_probs) if calibrated_probs else None,
            djyy_probs=djyy_probs,
            cfg=fusion_cfg,
            same_odds=same_odds_result,
            combo_boost=combo_boost,
            lgbm_probs=lgbm_probs,
            sina_data=_sina_data,
            league_draw_baseline=_league_db,
            league_draw_strength=_draw_str,
            draw_anchor=LEAGUE_DRAW_ANCHOR.get(_canon_league(fixture.competition)),
            draw_anchor_w=DRAW_ANCHOR_W,
            isotonic_fn=calibrator.calibrate if calibrator.is_fitted else None,
            temperature_fn=temp_scaler.calibrate if temp_scaler.is_fitted else None,
            freshness_fn=_fresh_fn,
            post_fusion=fusion_cfg.get("post_fusion", {}),
        ))
        final_h, final_d, final_a = _fusion_out.probs
        fusion_trace = _fusion_out.trace

        # --- 平局预警分类 ---
        # 冷门平局: 一方被市场看好但模型+市场证据显示存在平局风险
        # 均势平局: 双方实力接近、平局被市场低估
        # 联赛平局(R1, 2026-08-12): 高平联赛 + 市场平局P∈[0.20,0.30) 无脑改判
        draw_alert = None
        if calibrated_probs:
            market_h, market_d, market_a = calibrated_probs
            max_market = max(market_h, market_d, market_a)
            # R1: 高平联赛且市场平局概率处于低估区间（回测 +2.9pp，切半稳健）
            if _canon_league(fixture.competition) in HIGH_DRAW_LEAGUES and 0.20 <= market_d < 0.30:
                draw_alert = "league_draw"  # 联赛平局（数据驱动 R1）
            # 冷门平局: 市场强烈看好一方(>50%)，但平局概率>=25%
            elif max_market > 0.50 and market_d >= 0.25:
                draw_alert = "cold_draw"  # 冷门平局
            # 均势平局: 双方接近(差距<15%)，平局概率>=26%
            elif abs(market_h - market_a) < 0.15 and market_d >= 0.26:
                draw_alert = "balanced_draw"  # 均势平局

        # 半全场概率 (基于最终xG)
        _htft = htft_probabilities(pred.home_xg, pred.away_xg)

        # 无真实赔率时, 优先用 DJYY SSR 真实赔率 (Pinnacle), 否则合成赔率
        _odds_synthetic = False
        _djyy_ssr = djyy_ssr.enrich_prediction(fixture.home_team, fixture.away_team)
        if _djyy_ssr:
            djyy_ssr_enriched += 1
            # 用 DJYY 真实赛前 xG 替代我们模型的 xG
            if _djyy_ssr.get("home_xg_djyy") and _djyy_ssr.get("away_xg_djyy"):
                pred.home_xg = _djyy_ssr["home_xg_djyy"]
                pred.away_xg = _djyy_ssr["away_xg_djyy"]
        if market_odds is None and _djyy_ssr and _djyy_ssr.get("home_odds_djyy"):
            # DJYY 真实 Pinnacle 赔率 → 不再合成
            market_odds = (
                _djyy_ssr["home_odds_djyy"],
                _djyy_ssr["draw_odds_djyy"],
                _djyy_ssr["away_odds_djyy"],
            )
        if market_odds is None and final_h > 0 and final_d > 0 and final_a > 0:
            # 合成赔率: 公平赔率 = 1/概率, 加 5% 庄家水位, 最低 1.01
            _margin_rate = 1.05
            market_odds = (
                max(1.01, round(1 / (final_h * _margin_rate), 2)),
                max(1.01, round(1 / (final_d * _margin_rate), 2)),
                max(1.01, round(1 / (final_a * _margin_rate), 2)),
            )
            _odds_synthetic = True

        # 概率分布：双源比分融合（2026-08-05 结构升级）
        #   DJYY 是分析参考不是权威概率 → 与 MC 泊松模拟按权重融合，
        #   每源原始候选保留(djyy_top_scores/mc_top_scores)，结算记录各源命中率，数据说话调权重
        _djyy_ts = (djyy_data.get("top_scores") if djyy_data and djyy_data.get("top_scores") else None)
        _mc_ts = getattr(pred, "top_scores", None)

        def _norm_scores(ts):
            """归一化比分候选: [(h,a,p),...] → [(h,a,p_norm),...]; 无概率则按排名衰减"""
            if not ts:
                return []
            out = []
            for i, it in enumerate(ts):
                if isinstance(it, (list, tuple)) and len(it) >= 3:
                    try:
                        p = float(it[2])
                    except (TypeError, ValueError):
                        p = 0.0
                    if p <= 0:
                        p = 1.0 / (i + 1)
                    out.append((int(it[0]), int(it[1]), p))
                elif isinstance(it, (list, tuple)) and len(it) >= 2:
                    out.append((int(it[0]), int(it[1]), 1.0 / (i + 1)))
            s = sum(x[2] for x in out) or 1.0
            return [(h, a, p / s) for h, a, p in out]

        _djyy_norm = _norm_scores(_djyy_ts)
        _mc_norm = _norm_scores(_mc_ts)
        # 比分候选：DJYY 优先 + xG差温和重排（2026-08-05 实证，替代 0.55/0.45 双源融合）
        # 发现：双源融合稀释命中率（top5 52.2%→48.7%），DJYY 纯源最优；
        #       DJYY 分布对强弱不敏感（碾压局 xG差>1.5 实际 59% 高比分, top1 只给 12%）
        # 温和重排（xG差≥0.8: 高比分×1.3、1-1/0-0×0.85）：
        #   walk-forward 113 场: top1 9.7%→13.3%, top5 52.2%→54.9%, top1≥3球 7.1%→31.0%
        _xdiff = abs(float(pred.home_xg) - float(pred.away_xg)) if pred.home_xg and pred.away_xg else 0.0
        _reranked = None
        if _djyy_norm:
            _reranked = list(_djyy_norm)
            if _xdiff >= 0.8:
                _reranked = [
                    (h, a, round(p * (1.3 if h + a >= 3 else (0.85 if (h, a) in ((1, 1), (0, 0)) else 1.0)), 6))
                    for h, a, p in _reranked
                ]
                _reranked.sort(key=lambda x: -x[2])
            _fused = [(h, a, round(p, 4)) for h, a, p in _reranked[:8]]
            _score_src = "djyy"
        elif _mc_norm:
            _fused = [(h, a, round(p, 4)) for h, a, p in _mc_norm[:8]]
            _score_src = "mc"
        else:
            _fused = None
            _score_src = None

        # 盘口信号（2026-08-05 结构化，替代装饰性 ±0.02）：
        #   sina 欧赔压缩比 → 方向分: 压缩(资金涌入)=+, 抬升=-
        #   (compression - 1.0) * 2: c=1.05(资金涌入) → +0.10, c=0.95 → -0.10
        # 2026-08-14 修复：此前落盘公式写成 (1.0-c)*2（旧的反向约定），与
        # 应用侧（>1.05 加仓）符号相反，导致"盘口信号命中率"统计方向是反的。
        # 统一走 engine.market_signal.compression_signals，回归测试锁定口径。
        _market_signal = None
        if _sina_data:
            _comp = _sina_data.get("compression") or {}
            from engine.market_signal import compression_signals
            _market_signal = compression_signals(_comp)

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
            # 竞彩让球盘（hhad）赔率：sporttery 主源采集，供让球玩法 EV 评估
            "handicap_home_odds": getattr(fixture, "handicap_home_odds", None),
            "handicap_draw_odds": getattr(fixture, "handicap_draw_odds", None),
            "handicap_away_odds": getattr(fixture, "handicap_away_odds", None),
            # 模型让球后胜平负概率（DC/MC 已按官方让球线计算，落盘供 EV 出注）
            "handicap_home_prob": round(float(getattr(pred, "handicap_home_prob", 0) or 0), 4),
            "handicap_draw_prob": round(float(getattr(pred, "handicap_draw_prob", 0) or 0), 4),
            "handicap_away_prob": round(float(getattr(pred, "handicap_away_prob", 0) or 0), 4),
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
            # 融合链 trace（2026-08-29）：每步对 [h,d,a] 的实际改动，结算后可归因
            "fusion_trace": fusion_trace,
            # 双源融合比分候选（DJYY+MC）+ 盘口信号（2026-08-05）
            "top_scores": _fused,
            "score_sources": _score_src,
            "djyy_top_scores": _djyy_norm[:8] if _djyy_norm else None,
            "mc_top_scores": _mc_norm[:8] if _mc_norm else None,
            "market_signal": _market_signal,
            # 数据新鲜度护栏（2026-08-06）：无正式比赛天数 + 风险级别 + 收缩强度
            "freshness": _fresh.to_dict(),
            # 总进球分布：从 xG 泊松直接算完整分布（0~7+球），与 xG 口径一致。
            # 2026-08-14 修复：原 top_total_goals 是 MC 模拟 top-6 截断分布，
            # 期望 ~1.84 球 vs xG ~2.74 球，系统性低估进球 → 总进球 EV 偏 0/1/2 球。
            # DJYY totals 是大小球让球线独立概率，不是总进球数分布，勿混入。
            "total_goals": (
                total_goals_from_xg(getattr(pred, "home_xg", 0) or 0, getattr(pred, "away_xg", 0) or 0)
            ),
            # DJYY/新浪 大小球让球线独立概率（供大小球/总进球辅助判断）
            "over_under_lines": (
                djyy_data.get("totals") if djyy_data and djyy_data.get("totals")
                else (_sina_data.get("totals") if _sina_data else None)
            ),
            # 竞彩其余玩法官方赔率（sporttery 主源采集）
            # 2026-08-13 修复：manager 合并流把盘口存 _sporttery_* 前缀，直接读 _raw_* 永远 None
            # （波胆/总进球/半全场 207 场 0% 抓取率即此 bug）
            "ttg_odds": getattr(fixture, "_raw_ttg", None) or getattr(fixture, "_sporttery_ttg", None) or None,
            "crs_odds": getattr(fixture, "_raw_crs", None) or getattr(fixture, "_sporttery_crs", None) or None,
            "hafu_odds": getattr(fixture, "_raw_hafu", None) or getattr(fixture, "_sporttery_hafu", None) or None,
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
            # 平局预警
            "draw_alert": draw_alert,
            # 开赛时间（新浪有就用新浪的完整时间，否则用体彩 matchDate+matchTime）
            "kickoff": (_sina_data.get("match_time") if _sina_data else "") or fixture.kickoff,
            # 预测截点（时点分桶用，2026-08-16 起记录）
            "as_of": beijing_now().isoformat(timespec="seconds"),
            # 竞彩编号（如"周六001"），与新浪/赛果匹配的稳定键
            "match_no": fixture.match_id.split("_", 1)[-1] if "_" in fixture.match_id else "",
            # 新浪赔率数据（初始+即时+变化方向+压缩比+亚盘+大小球）
            "sina_odds": _sina_data,
            # 方向：预测时即写入（最终概率 argmax + 平局改判），与结算口径一致
            # （修复：预测时 direction 为空，页面/复盘拿不到方向）
            "direction": _pick_direction(final_h, final_d, final_a, draw_alert),
            # direction_prob 必须是"所选方向自己的概率"，不能是 max(H,D,A)。
            # 2026-08-14 事故：R1 改判平局时 direction=draw 但 direction_prob=主胜概率，
            # 串关用这个错概率查校准表 → 平局腿概率被高估（0.33→0.47→校准0.57），
            # 产出"⭐正EV +155%"的假推荐。
            "direction_prob": round(
                {"home": final_h, "draw": final_d, "away": final_a}.get(
                    _pick_direction(final_h, final_d, final_a, draw_alert), 0.0
                ), 4
            ),
            # 方向置信度差（最高概率 - 次高概率）：< 0.08 视为低置信度硬选（08-04 欧冠 4 连错全在此区间），
            # 出票环节拦截、复盘统计分层（P0 止血，2026-08-05）
            "direction_margin": round(max(final_h, final_d, final_a) - sorted([final_h, final_d, final_a])[-2], 4),
        })

    print(f"  ✓ 完成 {len(predictions)} 场预测（含增强分析）")
    if djyy_ssr_enriched:
        print(f"  DJYY SSR 增强: {djyy_ssr_enriched}/{len(predictions)} 场匹配 (Pinnacle赔率+xG)")

    # 6. 资金管理 + 投注计划
    print("\n[5/8] 资金管理与投注计划...")

    # 熔断检查
    breaker = CircuitBreaker(ROOT / "data" / "state" / "circuit_breaker.json")
    bankroll = strat_cfg.get("bankroll", 10000)
    # 用 CPPI 实际资金池（而非永远 10000）
    cppi = CPPIStrategy(
        ROOT / "data" / "state" / "cppi.json",
        initial_bankroll=bankroll,
    )
    actual_bankroll = cppi.state.current_bankroll if cppi.state.current_bankroll > 0 else bankroll
    breaker_mult = breaker.get_multiplier(actual_bankroll)
    breaker_status = breaker.status_report()
    print(f"  熔断状态: tier={breaker_status['tier']}, "
          f"streak={breaker_status['current_streak']}, "
          f"multiplier={breaker_mult}")
    print(f"  💰 资金池: {actual_bankroll:.0f}")

    # 虚拟投注：不熔断，始终正常投注

    # 自适应置信阈值（连败收紧）
    # shadow(虚拟)模式放开；真实下注模式保留最低置信 0.25，避免 0.09 置信的重注
    _activation = strat_cfg.get("activation_mode", "shadow")
    conf_threshold = 0.25 if _activation != "shadow" else 0

    # CPPI风险预算（使用已加载的 cppi 实例）
    risk_budget = cppi.get_risk_budget()
    print(f"  CPPI: 安全垫={risk_budget['cushion']}, "
          f"风险预算={risk_budget['risk_exposure']}")

    # Kelly + 三票制
    strategy = KellyStrategy(ROOT / "config" / "strategy.json")
    plan = strategy.evaluate_candidates(predictions)
    plan.date = target_date.isoformat()

    # 联赛分层报告：送钱区联赛禁止出注（老系统实证：瑞典超 -58%/欧罗巴 -51%/欧冠 -60% 全送钱）
    # 2026-08-06 升级：回暖解禁——累计口径送钱区但最近5场命中≥60%的联赛自动解禁观察
    # 2026-08-13 升级：双窗口判定（近5≥60% 且 近10≥50%），见 league_report.build_league_report
    league_forbid: set[str] = set()
    league_recovered: set[str] = set()
    try:
        from engine.review.league_report import build_league_report
        _lrep = build_league_report(ROOT / "data" / "daily", ROOT / "data" / "state" / "league_report.json")
        for _row in _lrep.get("leagues", []):
            if _row.get("verdict") == "送钱区" and _row.get("n", 0) >= 5:
                league_forbid.add(_row["league"])
            elif _row.get("verdict") == "回暖解禁":
                league_recovered.add(_row["league"])
        if league_forbid:
            print(f"  🚫 送钱区联赛禁投: {sorted(league_forbid)}")
        if league_recovered:
            print(f"  ✅ 回暖解禁联赛（近5≥60% 且 近10≥50%，解除禁投）: {sorted(league_recovered)}")
    except Exception as e:
        print(f"  ⚠ 联赛分层报告加载跳过: {e}")

    # 让球玩法：作为独立预测保留（方向可与胜平负不同），但概率用"市场主导融合"，
    # 与 1X2 同一套口径（模型 0.25 / 市场 0.75），避免"模型比分矩阵对抗市场"
    # 造成的 -18% ROI。融合在 evaluate_handicap_ev 内部完成，这里无需额外闸。

    # 三票制重分配
    effective_mult = 1.0  # 虚拟投注不降注
    allocator = ThreeTicketAllocator(
        bankroll=actual_bankroll,
        breaker_multiplier=effective_mult,
        limits=strat_cfg.get("limits", {}),
    )
    candidates = []
    filtered_count = 0
    # 投注层闸门配置（2026-08-29）：水位信号一致性 + 热门区注量因子
    _staking_cfg = strat_cfg.get("market_signal_staking", {})
    _band_cfg = strat_cfg.get("favorite_band", {})
    for p in predictions:
        # 市场分歧检测（2026-08-05）：独立于候选/置信度过滤，任何场次都记录
        # 非模型方向但有显著正 EV 的赔率（模型 vs 市场严重分歧信号）。
        # 8/5 实证：周三002/003 客胜 EV +38%/+115% 被方向纪律拦下且无提示；
        # 低置信度场次（margin<0.08）尤其容易发生——模型概率接近时市场赔率信息量大。
        # 存 predictions 供复盘验证"市场分歧方向是否值得破纪律"（数据说话，不预设）。
        _dir = p.get("direction")
        if not _dir:
            _probs0 = (p.get("home_win_prob", 0), p.get("draw_prob", 0), p.get("away_win_prob", 0))
            _dir = ["home", "draw", "away"][_probs0.index(max(_probs0))]
        for _sel0, _prob0, _ok0 in (
            ("home", p.get("home_win_prob", 0), "home_odds"),
            ("draw", p.get("draw_prob", 0), "draw_odds"),
            ("away", p.get("away_win_prob", 0), "away_odds"),
        ):
            if _sel0 == _dir:
                continue
            _od0 = p.get(_ok0)
            if _od0 and _prob0 * _od0 - 1 > 0.05:
                p.setdefault("market_disagreement", {})[_sel0] = {
                    "prob": round(_prob0, 3), "odds": _od0, "ev": round(_prob0 * _od0 - 1, 3)
                }
        # 让球 EV 全量落盘（2026-08-06）：独立于置信/禁投过滤，任何有让球盘的场次都记录
        # 三方向 edges 都存——argmax 只显示概率最高方向，但让球平/让球主常有正 EV
        # 藏在窄区间+高赔率里（让球平赔率 3.5-4.5，概率 15% 就可能 +EV）。
        # 必须在所有 continue 之前：禁投区联赛（巴西杯/欧冠等）不出注但仍要记录 EV 供复盘。
        try:
            from engine.strategy.handicap_ev import evaluate_handicap_ev
            _hev0 = evaluate_handicap_ev(p)
            if _hev0:
                p["handicap_ev"] = {
                    "handicap": _hev0.handicap,
                    "probs": {k: round(v, 4) for k, v in _hev0.probs.items()},
                    "edges": {k: round(v, 4) for k, v in _hev0.edges.items()},
                    "odds": {k: v for k, v in _hev0.odds.items()},
                    "best_sel": _hev0.best_sel,
                    "best_edge": round(_hev0.best_edge, 4),
                    "recommended": _hev0.recommended,
                }
        except Exception:
            pass
        # 自适应置信阈值过滤（连败时收紧）
        if p.get("confidence", 0) < conf_threshold:
            filtered_count += 1
            continue
        # P0 方向低置信度禁投：方向概率差 < 0.08 视为"掷硬币"级硬选
        # （113 场账本实证：margin<0.08 命中率 33-43% 低于整体 45%+；08-04 欧冠 4 连错全在此区间）
        if p.get("direction_margin", 1.0) < 0.08:
            p["direction_low_confidence"] = True
            filtered_count += 1
            continue
        # P0+ 概率段降档（2026-08-06 MBS 实证 + 本账本互证）：
        # 最终方向概率落在 [0.50, 0.60) 是"平局盲点区"——113 场账本中此段命中率仅 37.1%
        # （整体 43.4%），35 场实际平局 16 场（45.7%），模型 0 场判平；
        # MBS 8/3 自检同构：50-60% 概率段 0/3 全败，称"中高概率段集中回撤"。
        # 处理：降档不禁投（仍有 37% 命中）→ 稳胆降搏冷 / 搏冷降彩票 / 减注 50%
        _final_prob = max(p.get("home_win_prob", 0), p.get("draw_prob", 0), p.get("away_win_prob", 0))
        if 0.50 <= _final_prob < 0.60:
            p["prob_band_5060"] = True  # 触发降档
        # E 规则（2026-08-06）：高置信反向样本 ≥2 场的联赛 → 该联赛 60%+ 段降一档
        # （借鉴 MBS AIK 案例；巴甲 2 场 71%/74% 主胜→平局即触发）
        # 60%+ 段整体命中 52.2% 是最好段，但风险联赛的高置信更易翻车 → 保守降档
        if _final_prob >= 0.60 and _hcr_league_risk.get(p.get("competition", ""), 0) >= 2:
            p["prob_band_60_risk"] = True  # 触发降档（稳胆→搏冷 / 搏冷→彩票 / 彩票减注30%）
        # 联赛分层禁投：送钱区联赛（历史 ROI<-5% 且 ≥5 场）不出任何注
        # 2026-08-06：回暖解禁联赛（近期5场命中≥60%）不再禁投，打标供页面展示
        if p.get("competition") in league_forbid:
            p["league_forbidden"] = True
            continue
        if p.get("competition") in league_recovered:
            p["league_recovered"] = True
        is_synthetic = p.get("odds_synthetic", False)
        max_edge = -1.0  # 记录最大期望值 (prob * odds - 1)
        # 只押模型预测方向（8/3 教训：预测 home 押 away+draw 全输）
        _direction = p.get("direction")
        if not _direction:
            _probs = (p.get("home_win_prob", 0), p.get("draw_prob", 0), p.get("away_win_prob", 0))
            _direction = ["home", "draw", "away"][_probs.index(max(_probs))]

        # 竞彩让球玩法 EV：同一场让球 vs 胜平负只取更高 EV（避免同场重复押）
        # 三方向 EV 已在循环开头全量落盘 p["handicap_ev"]（不受过滤影响），
        # 这里只用 recommended 结果生成出票候选。
        _hcap_cand = None
        try:
            from engine.strategy.handicap_ev import evaluate_handicap_ev
            _hev = evaluate_handicap_ev(p)
            if _hev and _hev.recommended:
                _odds = _hev.odds[_hev.best_sel]
                _hcap_cand = {
                    "match_id": p["match_id"],
                    "selection": f"hcap_{_hev.best_sel}",  # 让球玩法标记
                    "odds": _odds,
                    "prob": _hev.probs[_hev.best_sel],
                    "kelly_fraction": _hev.best_edge / (_odds - 1) * 0.25,
                    "_hcap_edge": _hev.best_edge,
                }
        except Exception:
            _hcap_cand = None

        for sel, prob, odds_key in [
            ("home", p["home_win_prob"], "home_odds"),
            ("draw", p["draw_prob"], "draw_odds"),
            ("away", p["away_win_prob"], "away_odds"),
        ]:
            if sel != _direction:
                continue  # 禁止押反方向，保证预测与投注一致
            odds = p.get(odds_key)
            if not odds:
                continue
            
            edge = prob * odds - 1  # 期望值
            if edge > max_edge:
                max_edge = edge

            if is_synthetic:
                # 合成赔率：一律不出注。合成赔率 odds=1/(p×1.05) 构造，
                # 则 edge = p×odds−1 = 1/1.05−1 = −4.76%，数学上必然为负 EV。
                # 曾因此对 8 笔共 ¥67,353 押在模拟赔率上（基本全亏）。
                # 只记录 edge 供复盘，绝不进 candidates。
                continue
            elif edge > 0:  # 真实赔率: 正期望
                kelly_f = edge / (odds - 1) * 0.25  # quarter-Kelly
                # 2026-08-29 投注层闸门（概率层不再被水位信号污染）：
                #   1) 水位信号与投注方向冲突 → 注量打折（命中 0.693 vs 未命中 0.423 的强信号）
                #   2) 热门区(odds<1.8) 注量打折止血（账本 L1 -10.3% / L2 -18.3% ROI）
                _sig_factor, _sig_verdict = market_signal_gate(sel, p.get("sina_odds"), _staking_cfg)
                _band_factor = favorite_band_factor(odds, _band_cfg)
                kelly_f *= _sig_factor * _band_factor
                candidates.append({
                    "match_id": p["match_id"],
                    "selection": sel,
                    "odds": odds,
                    "prob": prob,
                    "kelly_fraction": kelly_f,
                    "prob_band_5060": p.get("prob_band_5060", False),
                    "prob_band_60_risk": p.get("prob_band_60_risk", False),
                    "prob_max": _final_prob,
                    "signal_verdict": _sig_verdict,
                    "signal_stake_factor": _sig_factor,
                    "band_stake_factor": _band_factor,
                })

        # 让球候选：同一场只留 EV 更高的方向（胜平负 vs 让球取最优）
        if _hcap_cand:
            _plain_cands = [c for c in candidates if c["match_id"] == p["match_id"]]
            _plain_best = max((c.get("_hcap_edge", 0) or (c["prob"] * c["odds"] - 1)) for c in _plain_cands) if _plain_cands else 0
            if _hcap_cand["_hcap_edge"] >= _plain_best:
                candidates = [c for c in candidates if c["match_id"] != p["match_id"]]
                candidates.append(_hcap_cand)
                p["handicap_kelly_edge"] = round(_hcap_cand["_hcap_edge"], 4)

        # 竞彩多玩法 EV：总进球(ttg)/波胆(crs)/半全场(hafu) 小注搏冷
        # （老系统实证：深冷赔率区 ROI 最高；冷门玩法高赔率才覆盖得了水钱）
        try:
            from engine.strategy.multi_play_ev import evaluate_all_plays
            for _pev in evaluate_all_plays(p):
                if not _pev.recommended:
                    continue
                if _pev.play == "ttg":
                    _sel_code = f"ttg_{_pev.best_sel}"
                elif _pev.play == "crs":
                    _sel_code = f"crs_{_pev.best_sel[0]}_{_pev.best_sel[1]}"
                else:
                    _sel_code = f"hafu_{_pev.best_sel}"
                _odds = _pev.odds[_pev.best_sel]
                candidates.append({
                    "match_id": f"{p['match_id']}#{_pev.play}",  # 去重粒度=场次+玩法
                    "selection": _sel_code,
                    "odds": _odds,
                    "prob": _pev.probs[_pev.best_sel],
                    "kelly_fraction": 0.03,  # 冷门玩法小注（波胆/半全场赔率高）
                })
                p.setdefault("play_ev", {})[_pev.play] = round(_pev.best_edge, 4)
        except Exception:
            pass

        # 写回 kelly_edge 到预测字典，解决 Kelly=0 问题
        p["kelly_edge"] = round(max_edge, 4) if max_edge > -1.0 else 0.0

    if filtered_count > 0:
        print(f"  置信过滤: {filtered_count} 场低于阈值 {conf_threshold:.2f}，已跳过")

    ticket_plan = allocator.allocate(candidates)
    print(f"  ✓ 三票方案: 稳胆{len(ticket_plan.stable_picks)}场, "
          f"搏冷{len(ticket_plan.value_picks)}场, "
          f"彩票{len(ticket_plan.lottery_picks)}场, "
          f"总投入={ticket_plan.total_stake}元")

    # 6.4 串关方案（2026-08-08 新增；2026-08-13 用户确认保留——竞彩主流玩法）
    # 数学纪律：串关吃双重抽水，按模型概率选腿 = 送钱（账本校准 0.55-0.60 段命中率
    # 仅 31.6%）。因此用账本校准命中率算真实 EV，只推荐 cal_ev>0 的串票；
    # 负 EV 串票落盘标注 ⚠（页面展示但不出注），无腿/全负则空仓。
    # 2026-08-13 教训：比分串 84 张 0 中（ROI -100%）→ 比分串必须用官方
    # 真实赔率算 EV，无官方赔率一律不出（详见 6.4b）。
    try:
        from engine.strategy.parlay import ParlayBuilder, load_calibration
        _cal = load_calibration(
            ROOT / "data" / "state" / "review_ledger.jsonl",
            overall=0.433,
            min_samples=8,
        )
        parlay_builder = ParlayBuilder(
            bankroll=actual_bankroll,
            limits=strat_cfg.get("limits", {}),
            calibration=_cal,
            league_forbid=league_forbid,
        )
        parlay_plan = parlay_builder.build(candidates, ticket_plan, predictions)
        _n_rec = sum(1 for t in parlay_plan if t.recommended)
        if parlay_plan:
            _pl_desc = "、".join(f"{t.parlay_type}{'⭐' if t.recommended else '⚠'}" for t in parlay_plan)
            print(f"  ✓ 串关方案: {len(parlay_plan)} 张票（推荐{_n_rec}张）: {_pl_desc}")
            if _n_rec == 0:
                print("  ⚠ 校准后全部负 EV：串关吃双重抽水，模型概率高估——不推荐出串（数据纪律）")
        else:
            print(f"  ⚠ 串关方案: 无正 EV 串关（校准概率×赔率<{parlay_builder.cfg.value_edge}，"
                  f"1.2-1.5 大热全被价值门槛淘汰），空仓")
    except Exception as _e:
        parlay_plan = []
        _cal = {}
        print(f"  ⚠ 串关方案生成跳过: {_e}")

    # 6.4b 比分串（波胆过关）— 2026-08-13 改造：官方赔率 EV 门槛
    # 教训（84 张 0 中 ROI -100%）：DJYY top_scores 概率未校准（0-0 系统性高估
    # 2-3 倍），模拟赔率表算 EV 是自欺欺人。改造后只出"官方真实波胆赔率"且
    # 校准概率×赔率>1 的正 EV 组合；无官方赔率/全负 EV → 空仓并说明。
    try:
        from engine.strategy.score_parlay import ScoreParlayBuilder
        score_plan = ScoreParlayBuilder().build(predictions)
        if score_plan:
            _sp_desc = "、".join(t.parlay_type for t in score_plan)
            print(f"  ✓ 比分串方案（正EV）: {len(score_plan)} 张票: {_sp_desc}")
        else:
            print("  ⚠ 比分串方案: 无官方赔率或全负 EV（校准后），空仓——"
                  "无真实赔率不出比分串（2026-08-13 纪律）")
    except Exception as _e:
        score_plan = []
        print(f"  ⚠ 比分串方案生成跳过: {_e}")

    # 6.5 场次并集保护（2026-08-07 修复）：Actions 每半小时重跑当天预测，
    # 某次数据源不完整（sporttery WAF 拦截/场次停售）会用少场次覆盖多场次，
    # git 三方合并还会静默吃掉旧场次（8/6 库奥皮奥因此丢失）。
    # 落盘前按 match_id 合并：本次缺失但历史已有的场次保留旧条目，
    # 保证网页永远显示当天全部竞彩场次（决策包与 predictions.json 一致）。
    _prev_pred_path = ROOT / "data" / "daily" / target_date.isoformat() / "predictions.json"
    if _prev_pred_path.exists():
        try:
            _prev_preds = json.loads(_prev_pred_path.read_text(encoding="utf-8"))
            if isinstance(_prev_preds, dict):
                _prev_preds = _prev_preds.get("predictions", [])
            _new_mids = {p.get("match_id") for p in predictions}
            _kept = 0
            for _pp in _prev_preds:
                if _pp.get("match_id") not in _new_mids:
                    predictions.append(_pp)
                    _kept += 1
                    print(f"  ⚠ 保留旧场次（本次采集缺失）: {_pp.get('match_id')} {_pp.get('home_team')} vs {_pp.get('away_team')}")
            if _kept:
                # 恢复编号顺序（match_no 排序）
                def _no_key(_p):
                    _no = _p.get("match_no", "")
                    return (_no[:2], int(_no[2:])) if len(_no) >= 3 and _no[2:].isdigit() else (_no, 0)
                predictions.sort(key=_no_key)
                print(f"  ✓ 场次并集完成: {len(predictions)} 场（保留 {_kept} 场）")
        except Exception as _e:
            print(f"  ⚠ 场次并集合并跳过: {_e}")

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
            "parlay": [t.to_dict() for t in parlay_plan],
            "score_parlay": [t.to_dict() for t in score_plan],
            "breaker_status": breaker_status,
            "cppi_budget": risk_budget,
            "total_stake": plan.total_stake,
        },
        config_prediction=pred_cfg,
        config_strategy=strat_cfg,
    )
    print(f"  ✓ 决策包 SHA-256: {bundle['bundle_sha256'][:16]}...")
    # 清理旧版本决策包（保留最新3个），防止30分钟一次的流水线无限堆积
    try:
        _pruned = bundle_mgr.prune_old_versions(target_date.isoformat(), keep=3)
        if _pruned:
            print(f"  🧹 已清理 {_pruned} 个旧决策包版本")
    except Exception as _e:
        print(f"  ⚠ 决策包清理跳过: {_e}")

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
            print("  ✓ 计划已锁定")
        else:
            print("  ⚠ 计划已存在锁定，跳过")

    # 保存预测结果
    print("\n[8/8] 保存结果...")
    output_dir = ROOT / "data" / "daily" / target_date.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "predictions.json").write_text(
        json.dumps(predictions, indent=2, ensure_ascii=False)
    )
    _tp_summary = allocator.summary(ticket_plan)
    _tp_summary["parlay"] = [t.to_dict() for t in parlay_plan]
    _tp_summary["score_parlay"] = [t.to_dict() for t in score_plan]
    _tp_summary["parlay_calibration"] = {
        "overall": _cal.get("overall", 0.433),
        "n": _cal.get("n", 0),
        "table": _cal.get("table", {}),
        "note": "串关 EV 用账本校准命中率（模型概率高估，0.55-0.60 段仅 31.6% 命中）；推荐=校准EV>0",
    }
    (output_dir / "ticket_plan.json").write_text(
        json.dumps(_tp_summary, indent=2, ensure_ascii=False)
    )

    print(f"\n{'='*60}")
    print("  流水线完成 ✓")
    print(f"  预测: {len(predictions)} 场")
    if freshness_active:
        print(f"  ⚠ 新鲜度护栏触发: {freshness_active} 场（概率向均势收缩）")
    print(f"  投注: {ticket_plan.total_stake} 元 (乘数={effective_mult:.2f})")
    print(f"{'='*60}")

    match_db.close()
    return predictions, plan


def _backfill_sina_results(
    daily_root: Path,
    source_mgr: "SourceManager",
    target_date: date,
    days: int = 2,
) -> int:
    """补结算：向前 N 天补查新浪已开奖赛果，幂等合并进对应日期 results.json

    问题（2026-08-06 审计发现）：新浪赛果接口 isPrized=1 只取"当日已开奖"。
    巴西杯等北京凌晨开球的比赛，当日 12/19 点结算时尚未开奖 → 漏结算，
    次日也不回头补 → 复盘盲区 + 命中率统计失真（8/5 弗鲁米嫩塞 vs 达伽马即案例）。

    方案：结算目标日时，对 target-1/target-2 天各调一次新浪 fetch_results(back_date)，
    现在这些比赛应已开奖 → 幂等合并进 back_date/results.json，并回写 predictions.json
    actual 字段（direction_correct 等，供复盘统计）。返回新增场次数。
    """
    _norm = normalize_team
    added_total = 0
    for i in range(1, days + 1):
        back_date = target_date - timedelta(days=i)
        bdir = daily_root / back_date.isoformat()
        if not bdir.exists():
            continue
        rj = bdir / "results.json"
        pj = bdir / "predictions.json"
        try:
            back_results = source_mgr.fetch_results(back_date)
        except Exception as e:
            print(f"  ⚠ 补结算 {back_date} 抓取失败: {e}")
            continue
        if not back_results:
            continue
        # 读现有 results.json（幂等基准）
        existing: list[dict] = []
        if rj.exists():
            try:
                _raw = json.loads(rj.read_text(encoding="utf-8"))
                existing = _raw if isinstance(_raw, list) else list(_raw.values())
            except Exception:
                existing = []
        existing_keys = {
            (_norm(x.get("home_team", "")), _norm(x.get("away_team", ""))): x
            for x in existing
        }
        # 读 predictions.json（回写 actual）
        preds: list[dict] = []
        if pj.exists():
            try:
                preds = json.loads(pj.read_text(encoding="utf-8"))
            except Exception:
                preds = []
        pred_by_key = {
            (_norm(p.get("home_team", "")), _norm(p.get("away_team", ""))): p
            for p in preds
        }
        changed = False
        for r in back_results:
            k = (_norm(r.home_team), _norm(r.away_team))
            rec = {
                "match_id": getattr(r, "match_id", ""),
                "home_team": r.home_team,
                "away_team": r.away_team,
                "home_score": r.home_score,
                "away_score": r.away_score,
                "competition": getattr(r, "competition", ""),
                "match_date": back_date.isoformat(),
                "match_no": getattr(r, "match_no", ""),
                "status": "完赛",
            }
            if k in existing_keys:
                # 已存在：比分不同 → 用新浪终场修正（幂等保护）
                x = existing_keys[k]
                if (x.get("home_score"), x.get("away_score")) != (r.home_score, r.away_score):
                    print(f"  ↻ 补结算修正 {back_date}: {r.home_team} vs {r.away_team} "
                          f"{x.get('home_score')}-{x.get('away_score')} → {r.home_score}-{r.away_score}")
                    x.update(rec)
                    changed = True
                continue
            existing.append(rec)
            existing_keys[k] = rec
            changed = True
            added_total += 1
            # 回写 predictions actual（幂等：仅未结算场次）
            p = pred_by_key.get(k)
            if p and p.get("actual_home_score") is None:
                hs, as_ = r.home_score, r.away_score
                actual = "home" if hs > as_ else ("draw" if hs == as_ else "away")
                p["actual_result"] = f"{hs}-{as_}"
                p["actual_home_score"] = hs
                p["actual_away_score"] = as_
                best_sel = max(
                    [("home", p.get("home_win_prob", 0)),
                     ("draw", p.get("draw_prob", 0)),
                     ("away", p.get("away_win_prob", 0))],
                    key=lambda x: x[1],
                )
                # R1 league_draw 改判已停用（2026-08-17 实盘证伪：8/13 起 8 场 R1 改判 0 中，
                # 5 场把正确 argmax 改错。见 _pick_direction。draw_alert 仅作展示标记）
                p["direction"] = best_sel[0]
                p["direction_correct"] = best_sel[0] == actual
                print(f"  ✓ 补结算回写 {back_date}: {r.home_team} {hs}-{as_} {r.away_team} "
                      f"预测{best_sel[0]} {'✅' if best_sel[0] == actual else '❌'}")
        if changed:
            rj.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        if preds:
            pj.write_text(json.dumps(preds, ensure_ascii=False, indent=2))
    return added_total


def run_settlement(target_date: date):
    """执行结算 + Elo 更新 + 熔断记录 + 组合挖掘更新（幂等）

    幂等设计（历史反复出问题的核心修复）：
      - 同一场比赛不会重复结算：以 (目录日期, 主队, 客队) 为幂等键，
        已存在于任意日期 results.json 的比赛跳过 Elo/熔断/权重/组合更新。
      - 赛果按"全局竞彩编号 → 预测所在目录"落盘，不再依赖接口返回的日期推断，
        解决跨周/跨日赛果存错目录导致的复盘漏结算。
      - review.json 每次结算都重新生成（不再被"已存在即跳过"冻结），
        复盘数据始终反映最新赛果。
      - 无新赛果时跳过重型校准（Temperature/Rho/Walk-forward），快速退出。
    """
    print(f"\n{'='*60}")
    print(f"  结算流水线 - {target_date.isoformat()}")
    print(f"{'='*60}")

    daily_root = ROOT / "data" / "daily"

    # 1) 全局预测索引：竞彩编号 → (目录日期, 完整match_id)；match_id → pred；队名 → pred
    # 1) 全局预测索引：竞彩编号 → [(目录日期, 完整match_id)]；match_id → pred；队名 → pred
    # 注意：竞彩编号跨周复用（如"周二002"每周都有），编号必须按日期就近解析
    pno_place: dict[str, list[tuple[str, str]]] = {}
    pred_by_mid: dict[str, dict] = {}
    pred_by_team: dict[str, tuple[str, dict]] = {}  # 队名key -> (目录日期, pred)
    _norm = normalize_team
    _loose = loose_normalize
    if daily_root.exists():
        for folder in sorted(daily_root.iterdir()):
            if not folder.is_dir():
                continue
            pf = folder / "predictions.json"
            if not pf.exists():
                continue
            try:
                _preds = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            for _p in _preds:
                _mid = _p.get("match_id", "")
                if _mid:
                    pred_by_mid.setdefault(_mid, _p)
                    _pno = _mid.split("_", 1)[-1] if "_" in _mid else ""
                    if _pno:
                        pno_place.setdefault(_pno, []).append((folder.name, _mid))
                _hk, _ak = _p.get("home_team", ""), _p.get("away_team", "")
                if _hk and _ak:
                    # 三级队名索引：原文 / 主归一化 / 宽松归一化（译名变体兜底）
                    pred_by_team.setdefault(f"{_hk}_vs_{_ak}", (folder.name, _p))
                    pred_by_team.setdefault(f"{_norm(_hk)}_vs_{_norm(_ak)}", (folder.name, _p))
                    pred_by_team.setdefault(f"{_loose(_hk)}_vs_{_loose(_ak)}", (folder.name, _p))
    print(f"  ✓ 全局预测索引: 编号 {sum(len(v) for v in pno_place.values())} / match_id {len(pred_by_mid)} / 队名 {len(pred_by_team)}")

    # 2) 幂等键：所有目录 results.json 中已记录的 (目录日期, 主队, 客队) → 比分
    #    比分也记录：若同一场比赛比分发生变化（如"进行中 0-0"被修正为终场 3-0），
    #    视为新增重新结算，而不是永远被旧记录挡住。
    settled_pairs: dict[tuple[str, str, str], tuple] = {}
    if daily_root.exists():
        for folder in daily_root.iterdir():
            if not folder.is_dir():
                continue
            rj = folder / "results.json"
            if not rj.exists():
                continue
            try:
                _rs = json.loads(rj.read_text(encoding="utf-8"))
            except Exception:
                continue
            for _x in _rs:
                if _x.get("home_team") and _x.get("away_team"):
                    settled_pairs[(folder.name, _norm(_x.get("home_team")), _norm(_x.get("away_team")))] = (
                        _x.get("home_score"), _x.get("away_score"))
    print(f"  ✓ 已结算基准: {len(settled_pairs)} 条 (results.json 幂等保护)")

    source_mgr = SourceManager(ROOT / "data")
    results = source_mgr.fetch_results(target_date)

    # 补结算（2026-08-06）：向前 2 天补查新浪已开奖赛果（凌晨开球场次当日漏结算）
    # 巴西杯等凌晨开球 → 当日结算时未开奖漏掉，这里补录回 results.json + predictions.json
    try:
        _backfilled = _backfill_sina_results(daily_root, source_mgr, target_date, days=2)
        if _backfilled:
            print(f"  ✓ 补结算新增 {_backfilled} 场（凌晨场次跨日补录）")
    except Exception as _e:
        print(f"  ⚠ 补结算跳过: {_e}")

    # 合并新浪赛果（互补数据源，同队名比分不同 → 覆盖修正）
    sina_file = ROOT / "data" / "daily" / target_date.isoformat() / "results_sina.json"
    if sina_file.exists():
        try:
            sina_results_raw = json.loads(sina_file.read_text(encoding="utf-8"))
            existing_teams = {(_norm(r.home_team), _norm(r.away_team)): r for r in results}
            sina_added = 0
            sina_fixed = 0
            for sr in sina_results_raw:
                tkey = (_norm(sr.get("home_team")), _norm(sr.get("away_team")))
                if tkey in existing_teams:
                    # 同队名（归一化后）已存在：比分不同则用新浪赛果修正（终场为准）
                    r = existing_teams[tkey]
                    if (r.home_score, r.away_score) != (sr.get("home_score"), sr.get("away_score")):
                        print(f"  ↻ 赛果修正: {tkey[0]} vs {tkey[1]} "
                              f"{r.home_score}-{r.away_score} → "
                              f"{sr['home_score']}-{sr['away_score']}")
                        r.home_score = sr["home_score"]
                        r.away_score = sr["away_score"]
                        r.match_no = sr.get("match_no", r.match_no)
                        # 半场比分同步（半全场玩法结算依赖）
                        r.half_home_score = int(sr.get("half_home_score") or 0)
                        r.half_away_score = int(sr.get("half_away_score") or 0)
                        sina_fixed += 1
                    continue
                results.append(MatchResult(
                    match_id=sr.get("match_id", f"{sr['home_team']}_vs_{sr['away_team']}"),
                    home_team=sr["home_team"],
                    away_team=sr["away_team"],
                    home_score=sr["home_score"],
                    away_score=sr["away_score"],
                    match_date=target_date.isoformat(),
                    competition=sr.get("league", ""),
                    match_no=sr.get("match_no", ""),
                    half_home_score=int(sr.get("half_home_score") or 0),
                    half_away_score=int(sr.get("half_away_score") or 0),
                ))
                existing_teams[tkey] = results[-1]
                sina_added += 1
            print(f"  ✓ 新浪补充: {sina_added} 新增, {sina_fixed} 比分修正 (total={len(results)})")
        except Exception as e:
            print(f"  ⚠ 新浪赛果合并失败: {e}")

    if not results:
        # 兜底: 外部赛果接口为空时，用本地 results.json 重建复盘（老日期也可用）。
        # 幂等保护仍在: 这些比赛已存在于 results.json，Elo/熔断/权重不会重复更新。
        _local = daily_root / target_date.isoformat() / "results.json"
        if _local.exists():
            try:
                _lr = json.loads(_local.read_text(encoding="utf-8"))
                for _x in _lr:
                    results.append(MatchResult(
                        match_id=_x.get("match_id", ""),
                        home_team=_x.get("home_team", ""),
                        away_team=_x.get("away_team", ""),
                        home_score=_x.get("home_score"),
                        away_score=_x.get("away_score"),
                        match_date=target_date.isoformat(),
                    ))
                print(f"  ✓ 外部赛果为空，使用本地 results.json 兜底: {len(results)} 场")
            except Exception as _e:
                print(f"  ⚠ 本地 results.json 读取失败: {_e}")
    if not results:
        print("  ⚠ 无比赛结果")
        return

    # 3) 归一化：确定每场比赛的 (目录日期, 完整match_id, 是否新增)
    norm = []
    _target_iso = target_date.isoformat()
    for r in results:
        _rno = getattr(r, "match_no", "") or ""
        _rmid = getattr(r, "match_id", "") or ""
        r_date, r_mid, pred = "", _rmid, None
        # 1) 完整 match_id 直接命中（带日期前缀，如 "2026-08-04_周二002"）
        if pred_by_mid.get(_rmid) is not None:
            pred = pred_by_mid[_rmid]
            r_mid = _rmid
            r_date = _rmid.split("_", 1)[0] if "_" in _rmid and _rmid[:4].isdigit() else ""
        if pred is None:
            # 2) 竞彩编号解析：编号跨周复用（"周二002"每周都有），
            #    必须按与目标日期的距离就近选择，距离 >2 天不认（防跨周误认）
            _pno = _rno or (_rmid.split("_", 1)[-1] if "_" in _rmid else _rmid)
            if _pno:
                _cands = pno_place.get(_pno)
                if _cands:
                    def _dkey(_c: tuple) -> int:
                        try:
                            from datetime import date as _d
                            return abs((_d.fromisoformat(_c[0]) - _d.fromisoformat(_target_iso)).days)
                        except Exception:
                            return 999
                    _best = min(_cands, key=_dkey)
                    if _dkey(_best) <= 2:
                        r_date, r_mid = _best
                        pred = pred_by_mid.get(r_mid)
        if pred is None:
            # 3) 队名三级匹配：原文 → 主归一化 → 宽松归一化（译名变体兜底）
            pred_meta = (pred_by_team.get(f"{r.home_team}_vs_{r.away_team}")
                         or pred_by_team.get(f"{_norm(r.home_team)}_vs_{_norm(r.away_team)}")
                         or pred_by_team.get(f"{_loose(r.home_team)}_vs_{_loose(r.away_team)}"))
            if pred_meta:
                # 用预测身份统一赛果：新浪数字ID("3802146") → 竞彩编号("2026-08-04_周二004")
                r_date, pred = pred_meta
                r_mid = pred.get("match_id") or _rmid
        if not r_date:
            if r_mid and "_" in r_mid and r_mid[:4].isdigit():
                r_date = r_mid.split("_")[0]
            if not r_date:
                r_date = getattr(r, "match_date", "") or _target_iso
        key = (r_date, _norm(r.home_team), _norm(r.away_team))
        old_score = settled_pairs.get(key)
        # 新增判定：无记录，或比分与已记录不同（进行中误抓被修正为终场）→ 重新结算
        is_new = old_score is None or old_score != (r.home_score, r.away_score)
        norm.append({
            "date": r_date, "match_id": r_mid, "r": r,
            "is_new": is_new, "key": key,
            "pred": pred,
        })

    # 3.5) 同一预测合并：DJYY 与新浪可能对同一场比赛返回不同队名/比分
    #     （如"佐加顿斯 vs 韦斯特罗 5-0" vs "佐加顿斯 vs 瓦斯特拉斯 6-0"）。
    #     以预测的竞彩编号为比赛身份，合并多条赛果：带竞彩编号(match_no)的新浪赛果优先，
    #     避免错误比分(5-0)覆盖权威终场比分(6-0)，也避免同一场双条目重复计 Elo。
    #     match_id 格式可能不同（"2026-08-03_周一001" vs "周一001"），统一取编号段。
    def _num_of(mid: str) -> str:
        return mid.split("_", 1)[-1] if mid and "_" in mid else (mid or "")

    _by_pred: dict = {}
    for n in norm:
        _k = (n["date"], _num_of(n["match_id"]))
        if _k not in _by_pred:
            _by_pred[_k] = n
            continue
        old = _by_pred[_k]
        _r, _nr = old["r"], n["r"]
        if _nr.match_no and not _r.match_no:
            _by_pred[_k] = n  # 新浪赛果替换 DJYY 赛果
        elif _nr.match_no and _r.match_no and \
                (_r.home_score, _r.away_score) != (_nr.home_score, _nr.away_score):
            # 两条都带编号但比分不同（如旧进行中 0-0 vs 终场 3-0）：取比分更新的
            # 无法判断新旧，保守取非零比分优先；仍冲突则保留先到的
            if (_nr.home_score, _nr.away_score) not in ((0, 0),) and (_r.home_score, _r.away_score) == (0, 0):
                _by_pred[_k] = n
    if len(_by_pred) < len(norm):
        print(f"  ✓ 同一预测赛果合并: {len(norm)} → {len(_by_pred)} 条")
    norm = list(_by_pred.values())
    # 合并后按 (日期, 队名) 再兜底去重：极端情况下不同编号但同队名的重复
    _seen_team: set = set()
    _dedup = []
    for n in norm:
        _tk = (n["date"], _norm(n["r"].home_team), _norm(n["r"].away_team))
        if _tk in _seen_team:
            continue
        _seen_team.add(_tk)
        _dedup.append(n)
    if len(_dedup) < len(norm):
        print(f"  ✓ 队名兜底去重: {len(norm)} → {len(_dedup)} 条")
    norm = _dedup
    new_items = [n for n in norm if n["is_new"]]
    print(f"  ✓ 赛果 {len(norm)} 场, 其中新增 {len(new_items)} 场（其余已结算过，跳过重复处理）")

    # 4) Elo 更新（只处理新增）
    print("\n[1/5] Elo 更新...")
    elo_updater = EloUpdater(ROOT / "data" / "models" / "team_ratings.json")
    for n in new_items:
        r = n["r"]
        elo_updater.update(r.home_team, r.away_team, r.home_score, r.away_score)
        print(f"  {r.home_team} {r.home_score}-{r.away_score} {r.away_team} ✓")
    elo_updater.save()
    print(f"  ✓ Elo 已更新 ({len(new_items)} 场新增)")

    # 5) 保存结果到 results.json（按目录日期，追加去重）
    results_by_date: dict[str, list] = {}
    for n in norm:
        r = n["r"]
        results_by_date.setdefault(n["date"], []).append({
            "match_id": n["match_id"] or f"{r.home_team}_vs_{r.away_team}",
            "home_score": r.home_score,
            "away_score": r.away_score,
            "home_team": r.home_team,
            "away_team": r.away_team,
            # 半场比分（半全场玩法结算依赖）
            "half_home_score": int(getattr(r, "half_home_score", 0) or 0),
            "half_away_score": int(getattr(r, "half_away_score", 0) or 0),
        })
    stored_total = 0
    for r_date, r_list in results_by_date.items():
        r_dir = daily_root / r_date
        r_dir.mkdir(parents=True, exist_ok=True)
        r_file = r_dir / "results.json"
        existing = []
        if r_file.exists():
            try:
                existing = json.loads(r_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing_ids = {e.get("match_id") for e in existing}

        def _mid_rank(mid: str) -> int:
            """match_id 身份规范度：竞彩编号+日期前缀 > 竞彩编号 > 其他(数字ID/队名拼接)"""
            if not mid:
                return 0
            _no = mid.split("_", 1)[-1]
            if "周" in _no and "_" in mid and mid[:4].isdigit():
                return 3
            if "周" in _no:
                return 2
            return 1

        # 归一化去重：同场多条（译名不同 / match_id 格式不同，如数字ID vs 竞彩编号）
        # 保留身份更规范的一条，删除其余 → 从根上清理 results.json 脏数据
        _seen_idx: dict[tuple, int] = {}
        _keep = []
        for _e in existing:
            _k = (_norm(_e.get("home_team")), _norm(_e.get("away_team")))
            if _k not in _seen_idx:
                _seen_idx[_k] = len(_keep)
                _keep.append(_e)
            else:
                _cur = _keep[_seen_idx[_k]]
                if _mid_rank(_e.get("match_id", "")) > _mid_rank(_cur.get("match_id", "")):
                    _keep[_seen_idx[_k]] = _e
        if len(_keep) < len(existing):
            print(f"    ↻ 归一化去重: results.json {len(existing)} → {len(_keep)} 条 (同场译名/编号重复)")
        if len(_keep) < len(existing):
            print(f"    ↻ 归一化去重: results.json {len(existing)} → {len(_keep)} 条 (同场译名/编号重复)")
        _dedup_changed = len(_keep) < len(existing)
        existing = _keep
        existing_ids = {e.get("match_id") for e in existing}
        existing_by_team = {(_norm(e.get("home_team")), _norm(e.get("away_team"))): e for e in existing}
        added = 0
        fixed = 0
        for item in r_list:
            _tkey = (_norm(item["home_team"]), _norm(item["away_team"]))
            old = existing_by_team.get(_tkey)
            if old is not None:
                # 同场已存在：统一身份（match_id 一律以本轮规范为准，如 周二002 → 2026-08-04_周二002）
                if old.get("match_id") != item["match_id"]:
                    old["match_id"] = item["match_id"]
                    fixed += 1
                # 同队名已存在：比分不同则覆盖（终场比分修正进行中误抓）
                if (old.get("home_score"), old.get("away_score")) != (item["home_score"], item["away_score"]):
                    old["home_score"] = item["home_score"]
                    old["away_score"] = item["away_score"]
                    old["home_team"] = item["home_team"]
                    old["away_team"] = item["away_team"]
                    old["half_home_score"] = item.get("half_home_score", old.get("half_home_score", 0))
                    old["half_away_score"] = item.get("half_away_score", old.get("half_away_score", 0))
                    fixed += 1
                continue
            if item["match_id"] in existing_ids:
                # 同 match_id（同预测）已存在但队名不同（DJYY/新浪译名差异）：
                # 比分不同则覆盖，避免旧/错比分残留
                _hit = False
                for e in existing:
                    if e.get("match_id") == item["match_id"] and \
                            (e.get("home_score"), e.get("away_score")) != (item["home_score"], item["away_score"]):
                        e["home_score"] = item["home_score"]
                        e["away_score"] = item["away_score"]
                        e["home_team"] = item["home_team"]
                        e["away_team"] = item["away_team"]
                        e["half_home_score"] = item.get("half_home_score", e.get("half_home_score", 0))
                        e["half_away_score"] = item.get("half_away_score", e.get("half_away_score", 0))
                        fixed += 1
                        _hit = True
                        break
                if not _hit:
                    continue
                continue
            existing.append(item)
            existing_by_team[_tkey] = item
            existing_ids.add(item["match_id"])
            added += 1
        if added or fixed or _dedup_changed:
            r_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        stored_total += added + fixed
        print(f"  ✓ results.json 已保存到 {r_date} (+{added}, total={len(existing)})")
    print(f"  ✓ 赛果落盘: 新增 {stored_total} 条")

    # 6) MatchDB 数据积累（只处理新增）
    print("\n[1.5/5] MatchDB 数据积累...")
    pred_cfg = load_config("prediction")
    db = MatchDB(ROOT / "data" / "state" / "match_history.db")
    db_recorded = 0
    for n in new_items:
        r = n["r"]
        pred = n["pred"]
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
                            db.record_lineup_xg(team, league, n["date"], players)
            except Exception:
                pass

        # 记录到match_history
        if pred:
            db.record_match({
                "match_id": pred.get("match_id", f"{r.home_team}_vs_{r.away_team}"),
                "date": n["date"],
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

    print(f"  ✓ MatchDB: {db_recorded} 场新增记录")
    db.close()

    # 7) 熔断 + 逐场结算（只处理新增）
    print("\n[2/5] 熔断 + 信任更新...")
    breaker = CircuitBreaker(ROOT / "data" / "state" / "circuit_breaker.json")
    strat_cfg = load_config("strategy")
    bankroll = strat_cfg.get("bankroll", 10000)
    # 从 CPPI 加载当前资金池（而非每次重置为 10000）
    cppi = CPPIStrategy(ROOT / "data" / "state" / "cppi.json", initial_bankroll=bankroll)
    running_bankroll = cppi.state.current_bankroll if cppi.state.current_bankroll > 0 else bankroll
    print(f"  💰 当前资金池: {running_bankroll:.0f}")

    # 读取投注计划（该日期目录的 ticket_plan）
    daily_dir = daily_root / target_date.isoformat()
    ticket_file = daily_dir / "ticket_plan.json"
    ticket_data = {}
    if ticket_file.exists():
        ticket_data = json.loads(ticket_file.read_text())
    ticket_map = {}
    for grp in ("stable", "value", "lottery"):
        for s in ticket_data.get(grp, []):
            # 多玩法候选 match 形如 "T1#ttg"，拆出原始 match_id，同场多玩法共处
            _mid = s.get("match", "").split("#")[0]
            ticket_map.setdefault(_mid, []).append(s)

    total_pnl = 0.0
    wins = 0
    losses = 0
    # 联赛自适应：结算时把每场赛果喂给联赛管理器（修复 league_params.json 永为空）
    league_mgr = LeagueParamsManager(ROOT / "data" / "state" / "league_params.json")
    league_fed = 0
    for n in new_items:
        r = n["r"]
        pred = n["pred"]
        if not pred:
            continue
        # 判断赛果
        if r.home_score > r.away_score:
            actual = "home"
        elif r.home_score == r.away_score:
            actual = "draw"
        else:
            actual = "away"
        # 检查是否命中（纯 argmax；R1 league_draw 改判 2026-08-17 停用，
        # 实盘证伪：8/13 起 8 场 0 中，5 场把正确 argmax 改错，见 _pick_direction）
        best_sel = max(
            [("home", pred["home_win_prob"]),
             ("draw", pred["draw_prob"]),
             ("away", pred["away_win_prob"])],
            key=lambda x: x[1],
        )
        won = best_sel[0] == actual
        # 联赛参数记录：方向命中反馈（用本循环已算出的 won，避免 direction 未回写时误判）
        lg_name = _canon_league(pred.get("competition") or r.competition or "未知")
        try:
            league_mgr.record_result(league=lg_name, hit=won)
            # 判平反馈（结构升级：判平强度随反馈自适应，判错自动降权而非关闭）
            league_mgr.record_draw_result(
                league=lg_name,
                was_draw=(best_sel[0] == "draw"),
                hit=(actual == "draw"),
            )
            league_fed += 1
        except Exception:
            pass
        # 计算PnL（基于Kelly plan，支持同场多玩法）
        pnl = 0.0
        s_list = ticket_map.get(pred["match_id"], [])
        for s in s_list:
            _sel = s.get("sel", "")
            _stake = s.get("stake", 0.0)
            _odds = s.get("odds", 0.0)
            # 按玩法前缀判定命中
            if _sel.startswith("hcap_"):
                _hit = _sel[len("hcap_"):] == actual
            elif _sel.startswith("ttg_"):
                _hit = int(_sel[4:]) == min(7, r.home_score + r.away_score)
            elif _sel.startswith("crs_"):
                _parts = _sel[4:].split("_")
                _hit = len(_parts) == 2 and int(_parts[0]) == r.home_score and int(_parts[1]) == r.away_score
            elif _sel.startswith("hafu_"):
                _hsel = _sel[5:]
                _hh, _ha = getattr(r, "half_home_score", 0), getattr(r, "half_away_score", 0)
                _half_actual = "H" if _hh > _ha else ("D" if _hh == _ha else "A")
                _full_actual = "H" if r.home_score > r.away_score else ("D" if r.home_score == r.away_score else "A")
                _hit = _hsel == _half_actual + _full_actual
            else:
                _hit = _sel == actual
            if _hit:
                pnl += _stake * (_odds - 1)
            else:
                pnl -= _stake
        pred["pnl"] = round(pnl, 2)  # 写回预测记录，review.json 才能统计真实盈亏
        total_pnl += pnl
        if won:
            wins += 1
        else:
            losses += 1
        breaker.record_result(won=won, pnl=pnl, bankroll=running_bankroll)
        running_bankroll += pnl
    print(f"  ✓ 命中 {wins}/{wins+losses}, PnL={total_pnl:.2f}")
    print(f"  熔断状态: {breaker.status_report()}")

    # 联赛自适应调参（命中率<45% → 更信任市场；>60% → 更信任模型）
    if league_fed > 0:
        try:
            league_mgr.adapt_all()
            print(f"  ✓ 联赛自适应完成（{league_fed} 场反馈，{len(league_mgr.summary())} 个联赛）")
        except Exception as e:
            print(f"  ⚠️ 联赛自适应跳过: {e}")

    # 8) 在线权重学习 + 组合挖掘（只处理新增）
    # 8.5) 赛果回写（[3.5/5]）对全部赛果执行：无新赛果时也补写历史 actual_result（幂等）
    if new_items or norm:
        # [2.5/5] 在线权重学习：已停用（2026-08-05 审计）
        # OnlineWeightLearner 只被 update("ensemble") 写入 performances，
        # 权重计算只认 dixon_coles/monte_carlo → current_weights 永远空；
        # 且 ensemble.py 从不读 online_weights → 纯死代码、假学习。
        # 真正的融合权重学习走 fusion_optimizer（fusion_weights.json，反事实验证+守卫栏）。
        print("  ⏭ 在线权重学习已停用（死代码无消费者，权重学习走 fusion_optimizer）")

        print("\n[3/5] 组合挖掘更新...")
        combo_miner = ComboMiner(ROOT / "data" / "state" / "combo_stats.json")
        for n in new_items:
            r, pred = n["r"], n["pred"]
            if not pred:
                continue
            if r.home_score > r.away_score:
                actual = "home"
            elif r.home_score == r.away_score:
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
        print("  ✓ 组合统计已更新")

        # 将赛果写回 predictions.json（自愈: 写回预测所在目录）
        # 遍历 norm（全部赛果）而非 new_items：已结算场次也能补写 actual_result
        # （如译名修复前结算过、actual_result 仍为 None 的历史场次）。写回幂等。
        print("\n[3.5/5] 更新预测赛果...")
        _touch: dict[str, list] = {}
        for n in norm:
            r, pred = n["r"], n["pred"]
            if not pred:
                continue
            pred["actual_result"] = f"{r.home_score}-{r.away_score}"
            pred["actual_home_score"] = r.home_score
            pred["actual_away_score"] = r.away_score
            best_sel = max(
                [("home", pred["home_win_prob"]),
                 ("draw", pred["draw_prob"]),
                 ("away", pred["away_win_prob"])],
                key=lambda x: x[1],
            )
            # 平局盲点修复已全部停用（2026-08-17，实盘证伪同模式）：
            # balanced/cold（8/13 停用：13 场改判仅 3 场改对）+ R1 league_draw
            # （8/17 停用：8/13 起 8 场 0 中，5 场把正确 argmax 改错）。
            # 统一纯 argmax，draw_alert 仅作展示标记，见 _pick_direction。
            if r.home_score > r.away_score:
                actual = "home"
            elif r.home_score == r.away_score:
                actual = "draw"
            else:
                actual = "away"
            pred["direction"] = best_sel[0]
            pred["direction_correct"] = best_sel[0] == actual
            top_scores = pred.get("top_scores", [])
            if top_scores and isinstance(top_scores[0], list):
                ps = f"{top_scores[0][0]}-{top_scores[0][1]}"
                pred["predicted_score"] = ps
                pred["score_correct"] = ps == f"{r.home_score}-{r.away_score}"
                # 比分命中位置闭环（2026-08-05）：完整记录实际比分在候选列表中的排名
                # 1=top1命中 ... 0=未进候选；top3/top5/top8 是"推荐档"命中标记
                _rank = 0
                for _i, _it in enumerate(top_scores):
                    if (isinstance(_it, (list, tuple)) and len(_it) >= 2
                            and int(_it[0]) == r.home_score and int(_it[1]) == r.away_score):
                        _rank = _i + 1
                        break
                pred["score_rank"] = _rank
                pred["score_top3_hit"] = 1 <= _rank <= 3
                pred["score_top5_hit"] = 1 <= _rank <= 5
                pred["score_top8_hit"] = 1 <= _rank <= 8
                # 双源比分命中（2026-08-05）：记录实际比分在 DJYY 源 / MC 源的各自排名，
                # 账本累积后比较两源命中率 → 数据决定融合权重
                for _src_key, _src_ts in (("djyy_score_rank", pred.get("djyy_top_scores")),
                                          ("mc_score_rank", pred.get("mc_top_scores"))):
                    _srank = 0
                    if _src_ts:
                        for _i, _it in enumerate(_src_ts):
                            if (isinstance(_it, (list, tuple)) and len(_it) >= 2
                                    and int(_it[0]) == r.home_score and int(_it[1]) == r.away_score):
                                _srank = _i + 1
                                break
                    pred[_src_key] = _srank
                # 盘口信号命中（2026-08-05）：market_signal 最强方向 == 实际方向？
                # 累积验证"盘口信号到底有没有用"，网页展示命中率
                _msig = pred.get("market_signal") or {}
                if _msig:
                    _sig_dir = max(_msig, key=lambda k: _msig.get(k, 0))
                    pred["market_signal_dir"] = _sig_dir
                    pred["market_signal_hit"] = _sig_dir == actual
                else:
                    pred["market_signal_dir"] = None
                    pred["market_signal_hit"] = None
            _touch.setdefault(n["date"], []).append(pred["match_id"])
        _updated = 0
        for _d, _mids in _touch.items():
            _pf = daily_root / _d / "predictions.json"
            if not _pf.exists():
                continue
            try:
                _pl = json.loads(_pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            _by_mid = {p.get("match_id"): p for p in _pl}
            _chg = 0
            for _m in _mids:
                _src = pred_by_mid.get(_m)
                if _m in _by_mid and _src:
                    _new = _src.get("actual_result")
                    _cur = _by_mid[_m].get("actual_result")
                    # 修正条件：不仅 None 要写，比分变化（如进行中 0-0 → 终场 3-0）也要覆盖
                    if _new is not None and _new != _cur:
                        _by_mid[_m].update(_src)
                        _chg += 1
            _pf.write_text(json.dumps(_pl, ensure_ascii=False, indent=2))
            _updated += _chg
        print(f"  ✓ 已更新 {_updated} 场预测赛果")

    # 9) CPPI 更新（已在结算前加载，直接更新）
    print("\n[4/5] CPPI 资产更新...")
    cppi.update(running_bankroll)
    cppi.save()
    print(f"  ✓ 资产: {bankroll:.0f} → {running_bankroll:.0f}  (PnL: {total_pnl:+.2f})")

    # 10) 复盘（每次结算都重新生成，绝不因 review.json 存在而冻结）
    print("\n[5/5] 赛后复盘...")
    from engine.review.post_match import PostMatchReviewer, ReviewLedger
    reviewer = PostMatchReviewer(ROOT / "data", pred_cfg.get("review", {}))
    review_report = reviewer.review_day(target_date.isoformat())
    if review_report.get("n_matches", 0) > 0:
        print(f"  ✓ 复盘: {review_report['n_matches']}场, 命中率{review_report.get('hit_rate', 0):.0%}")
        src_b = review_report.get("source_brier", {})
        print(f"    Brier: model={src_b.get('model', '?')} market={src_b.get('market', '?')} djyy={src_b.get('djyy', '?')} final={src_b.get('final', '?')}")
        _layered = review_report.get("layered", {})
        _ll = _layered.get("log_loss_final")
        if _ll is not None:
            print(f"    LogLoss(final): {_ll}")
        _gf = _layered.get("goal_framework", {})
        if _gf.get("n"):
            print(f"    进球框架: {_gf.get('hits')}/{_gf.get('n')} ({_gf.get('hits', 0) / _gf.get('n', 1):.0%})")
        _bands = _layered.get("prob_bands", {})
        if _bands:
            _band_str = " | ".join(f"{k}:{v['hit_rate']:.0%}({v['n']})" for k, v in _bands.items())
            print(f"    概率分段: {_band_str}")
        _fres = _layered.get("freshness_groups", {})
        if _fres:
            _fres_str = " | ".join(f"{k}:{v['hit_rate']:.0%}({v['n']})" for k, v in _fres.items())
            print(f"    新鲜度分层: {_fres_str}")
        for bias in review_report.get("biases", []):
            print(f"    ⚠ 偏差: {bias['dimension']}:{bias['key']} {bias['outcome']} gap={bias['gap']:+.3f}")
    else:
        print("  - 无可复盘数据")

    # 10.5) 高置信反向样本库（2026-08-06，借鉴 MBS 8/2 AIK 案例）
    # 高置信 + 市场同向 + 结果反向 → 独立归档，不归因于模型-市场分歧
    try:
        from engine.review.high_conf_reversals import archive as hcr_archive
        _hcr = hcr_archive(
            ROOT / "data" / "state" / "review_ledger.jsonl",
            ROOT / "data" / "state" / "high_conf_reversals.jsonl",
            ROOT / "data" / "daily",
        )
        if _hcr["total_archived"]:
            print(f"  ⚠ 高置信反向样本库: 累计 {_hcr['total_archived']} 场 "
                  f"(本次新增 {_hcr['new_archived']})")
    except Exception as _e:
        print(f"  - 高置信反向样本库跳过: {_e}")

    # 11) 重型校准：仅在有新赛果时执行（避免每次定时任务都跑全套）
    if not new_items:
        print("\n  ⏭ 无新增赛果，跳过校准/优化步骤（快速退出）")
        print(f"\n{'='*60}")
        print("  结算完成 ✓ (无新增)")
        print(f"{'='*60}")
        return

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

    # Temperature Scaling 重新拟合
    print("\n  [校准更新] Temperature Scaling...")
    ledger_path = ROOT / "data" / "state" / "review_ledger.jsonl"
    all_records = []
    if ledger_path.exists():
        for line in ledger_path.read_text().strip().split("\n"):
            if line.strip():
                try:
                    all_records.append(json.loads(line))
                except Exception:
                    continue
    if len(all_records) >= 30:
        ts_probs = np.array([r.get("final_prob", [0.33, 0.34, 0.33]) for r in all_records])
        ts_actuals = np.array([r.get("actual_idx", 0) for r in all_records])
        temp_scaler = TemperatureScaler(ROOT / "data" / "models" / "temperature.json")
        temp_scaler.fit(ts_probs, ts_actuals)
    else:
        print(f"    样本不足 ({len(all_records)} < 30)")

    # Isotonic 校准拟合（2026-08-12 补：此前模块存在但从未被拟合，
    # 导致 final 概率无最终校准层，融合后 Brier 0.6494 差于纯市场 0.6339）
    print("\n  [校准更新] Isotonic...")
    if len(all_records) >= 30:
        cal_probs = np.array([r.get("final_prob", [0.33, 0.34, 0.33]) for r in all_records])
        cal_actuals = np.array([r.get("actual_idx", 0) for r in all_records])
        from engine.prediction.isotonic_cal import IsotonicCalibrator, CalibrationConfig
        _cal_cfg = CalibrationConfig(**{k: v for k, v in pred_cfg.get("calibration", {}).items()
                                        if k in CalibrationConfig.__dataclass_fields__})
        _calibrator = IsotonicCalibrator(ROOT / "data" / "models" / "isotonic_cal.pkl", config=_cal_cfg)
        _calibrator.fit(cal_probs, cal_actuals)
    else:
        print(f"    样本不足 ({len(all_records)} < 30)")

    # Rho MLE 拟合
    print("\n  [校准更新] Rho MLE...")
    rho_fitter = RhoFitter(ROOT / "data" / "state" / "match_history.db")
    rho_result = rho_fitter.fit()
    if rho_result["rho"] is not None:
        # 写入 config
        config_path = ROOT / "config" / "prediction.json"
        if config_path.exists():
            cfg = json.loads(config_path.read_text())
            cfg.setdefault("prediction", {})["rho"] = rho_result["rho"]
            config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            print(f"    ✓ rho={rho_result['rho']} 已写入 config")

    # Walk-forward 回测
    print("\n  [校准更新] Walk-forward 回测...")
    from engine.backtest.walk_forward import WalkForwardEvaluator
    wf_eval = WalkForwardEvaluator(ROOT / "data")
    wf_report = wf_eval.evaluate()
    wf_path = ROOT / "data" / "state" / "walk_forward_report.json"
    wf_path.write_text(json.dumps(wf_report, indent=2, ensure_ascii=False))
    metrics = wf_report.get("metrics", {})
    draw_a = wf_report.get("draw_analysis", {})
    strat = wf_report.get("strategy_comparison", {})
    print(f"    命中率: {metrics.get('hit_rate', 0):.1%} | "
          f"Brier: {metrics.get('brier', 0):.4f} | "
          f"RPS: {metrics.get('rps', 0):.4f} | "
          f"ECE: {metrics.get('ece', 0):.4f}")
    print(f"    平局: 实际{draw_a.get('actual_draw_rate', 0):.0%} "
          f"预测{draw_a.get('predicted_draw_rate', 0):.0%} "
          f"最高{draw_a.get('max_draw_prob', 0):.0%}")
    best_strat = max(strat.items(), key=lambda x: x[1]["hit_rate"])
    print(f"    最优策略: {best_strat[0]} ({best_strat[1]['hit_rate']:.1%})")

    print(f"\n{'='*60}")
    print(f"  结算完成 ✓ ({len(new_items)} 场新增)")
    print(f"{'='*60}")


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
        target = date.fromisoformat(beijing_today())
    else:
        target = date.fromisoformat(args.date)

    if args.settle:
        run_settlement(target)
    else:
        run_daily_pipeline(target, predict_only=args.predict_only)


if __name__ == "__main__":
    main()
