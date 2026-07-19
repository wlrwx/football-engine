"""主入口 - 每日预测流水线"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.sources.manager import SourceManager
from engine.prediction.ensemble import EnsembleModel
from engine.prediction.dixon_coles import DixonColesConfig
from engine.prediction.monte_carlo import MonteCarloConfig
from engine.prediction.base import TeamRating
from engine.strategy.kelly import KellyStrategy
from engine.integrity.decision_bundle import DecisionBundle
from engine.integrity.plan_lock import PlanLock
from engine.learning.elo_updater import EloUpdater


def load_config(name: str) -> dict:
    path = ROOT / "config" / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def run_daily_pipeline(target_date: date):
    """执行每日完整流水线"""
    print(f"{'='*60}")
    print(f"  每日预测流水线 - {target_date.isoformat()}")
    print(f"{'='*60}")

    # 1. 加载配置
    pred_cfg = load_config("prediction")
    strat_cfg = load_config("strategy")

    # 2. 获取数据
    print("\n[1/6] 获取赛程数据...")
    source_mgr = SourceManager(ROOT / "data")
    fixtures, manifest = source_mgr.fetch_fixtures(target_date)
    print(f"  ✓ 获取 {len(fixtures)} 场比赛 (来源: {manifest.source})")

    # 3. 加载球队评级
    print("\n[2/6] 加载球队评级...")
    elo_updater = EloUpdater(ROOT / "data" / "models" / "team_ratings.json")

    # 4. 预测
    print("\n[3/6] 运行预测模型...")
    dc_cfg = DixonColesConfig(**{k: v for k, v in pred_cfg.get("prediction", {}).items()
                                  if k in DixonColesConfig.__dataclass_fields__})
    mc_cfg = MonteCarloConfig(simulations=pred_cfg.get("prediction", {}).get("monte_carlo_simulations", 50000))
    weights = pred_cfg.get("ensemble", {"dixon_coles_weight": 0.6, "monte_carlo_weight": 0.4})
    model = EnsembleModel(
        dc_config=dc_cfg,
        mc_config=mc_cfg,
        weights={"dixon_coles": weights.get("dixon_coles_weight", 0.6),
                 "monte_carlo": weights.get("monte_carlo_weight", 0.4)},
    )

    predictions = []
    for fixture in fixtures:
        home_rating = elo_updater.get_rating(fixture.home_team)
        away_rating = elo_updater.get_rating(fixture.away_team)

        market_odds = None
        if fixture.home_odds and fixture.draw_odds and fixture.away_odds:
            market_odds = (fixture.home_odds, fixture.draw_odds, fixture.away_odds)

        pred = model.predict(
            home=home_rating,
            away=away_rating,
            market_odds=market_odds,
            handicap=fixture.handicap,
        )
        pred.match_id = fixture.match_id
        pred.competition = fixture.competition

        predictions.append({
            "match_id": pred.match_id,
            "competition": pred.competition,
            "home_team": pred.home_team,
            "away_team": pred.away_team,
            "home_win_prob": pred.home_win_prob,
            "draw_prob": pred.draw_prob,
            "away_win_prob": pred.away_win_prob,
            "home_xg": pred.home_xg,
            "away_xg": pred.away_xg,
            "home_odds": fixture.home_odds,
            "draw_odds": fixture.draw_odds,
            "away_odds": fixture.away_odds,
            "handicap": fixture.handicap,
            "confidence": pred.confidence,
        })

    print(f"  ✓ 完成 {len(predictions)} 场预测")

    # 5. 生成投注计划
    print("\n[4/6] 生成投注计划...")
    strategy = KellyStrategy(ROOT / "config" / "strategy.json")
    plan = strategy.evaluate_candidates(predictions)
    plan.date = target_date.isoformat()
    print(f"  ✓ 单注 {len(plan.singles)} 个, 总投入 {plan.total_stake} 元")

    # 6. 创建决策包 + 锁定
    print("\n[5/6] 创建不可变决策包...")
    bundle_mgr = DecisionBundle(ROOT / "data" / "daily" / target_date.isoformat())
    bundle = bundle_mgr.create(
        date_str=target_date.isoformat(),
        import_manifest=manifest.__dict__,
        predictions=predictions,
        betting_plan={
            "singles": [{"match_id": s.match_id, "selection": s.selection,
                         "stake": s.stake, "odds": s.odds} for s in plan.singles],
            "total_stake": plan.total_stake,
        },
        config_prediction=pred_cfg,
        config_strategy=strat_cfg,
    )
    print(f"  ✓ 决策包 SHA-256: {bundle['bundle_sha256'][:16]}...")

    # 7. 锁定计划
    print("\n[6/6] 锁定计划...")
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
    output_dir = ROOT / "data" / "daily" / target_date.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "predictions.json").write_text(
        json.dumps(predictions, indent=2, ensure_ascii=False)
    )

    print(f"\n{'='*60}")
    print(f"  流水线完成 ✓")
    print(f"{'='*60}")

    return predictions, plan


def run_settlement(target_date: date):
    """执行结算 + Elo 更新"""
    print(f"\n[结算] {target_date.isoformat()}")

    source_mgr = SourceManager(ROOT / "data")
    results = source_mgr.fetch_results(target_date)
    if not results:
        print("  ⚠ 无比赛结果")
        return

    elo_updater = EloUpdater(ROOT / "data" / "models" / "team_ratings.json")
    for r in results:
        elo_updater.update(r.home_team, r.away_team, r.home_score, r.away_score)
        print(f"  {r.home_team} {r.home_score}-{r.away_score} {r.away_team} ✓")

    elo_updater.save()
    print(f"  ✓ Elo 已更新 ({len(results)} 场)")


def main():
    parser = argparse.ArgumentParser(description="Sporttery Engine")
    parser.add_argument("--date", default="today", help="目标日期 (YYYY-MM-DD 或 today)")
    parser.add_argument("--settle", action="store_true", help="执行结算")
    parser.add_argument("--predict-only", action="store_true", help="仅预测不锁定")
    args = parser.parse_args()

    if args.date == "today":
        target = date.today()
    else:
        target = date.fromisoformat(args.date)

    if args.settle:
        run_settlement(target)
    else:
        run_daily_pipeline(target)


if __name__ == "__main__":
    main()
