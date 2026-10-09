from __future__ import annotations
"""联赛独立参数 - 每个联赛维护自己的参数组

设计原则:
  - 每个联赛有独立的 (base_goals, home_adv_weight, market_blend_weight)
  - 初始值从 DJYY league-matrix 的场均数据做先验
  - 由 optimizer 根据该联赛历史命中率独立调参
  - 持久化到 data/state/league_params.json
  - 新联赛/样本不足时 fallback 到全局默认值
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class LeagueParam:
    """单个联赛的参数组"""
    base_goals: float = 1.35          # 基础进球期望（影响DC模型λ）
    home_adv_weight: float = 1.0      # 主场优势权重
    market_blend_weight: float = 0.28 # 市场赔率混合权重
    # xG 校准（2026-08-05 账本 113 场实证：不同联赛偏差方向相反）
    #   挪超 -0.43/场（低估→>1）vs 巴西杯 +1.38/场（高估→<1），一刀切校准会害低估联赛
    #   系数 = 实际场均总进球 / 预测场均总进球（账本回算，范围 0.55-1.3）
    xg_calibration: float = 1.0
    # 平局基线（该联赛**真实平局率**，即实际打平的场次占比）
    #
    # 【语义修正 2026-10-09】此前本字段的注释写「该联赛实际平局率」，
    # 但取值实际来自 draw_predictions/draw_hits（= 系统判平的场次里真打平的
    # 比例 = **判平精度**），两者是完全不同的量：
    #     判平精度  巴甲 6/10 = 0.60   ← 旧值含义
    #     真实平局率 巴甲 4652 场 = 0.266  ← 本字段应有的含义
    # 融合步骤 7 用 target_d = draw_baseline × draw_strength 抬升平局概率，
    # 于是巴甲每场平局概率被钉死在 0.5025，而真实平局率仅 0.266。
    # 该错误自我强化：判平越多 → 样本越多 → 若判平常错则精度看似更高。
    #
    # 现在该值一律从权威数据源（league_matrix.json / matches.csv）读取，
    # 且要求样本量达 draw_baseline_min_n，否则回落默认 0.25。
    draw_baseline: float = 0.25
    # 安全阀：判平抬升强度的上限（见 draw_strength）
    draw_strength_cap: float = 0.45
    # 平局基线与权威实测值的最大容许偏差；超过则在加载时迁移修正
    draw_baseline_tolerance: float = 0.08
    # 平局基线可信度所需的最小场次数（低于此数不得用于抬升）
    draw_baseline_min_n: int = 100
    # 平局基线的样本场次数（由权威数据源回填，见 _backfill_draw_baseline_samples）
    draw_baseline_samples: int = 0
    # 判平反馈（2026-08-05 结构升级：判平不是 0/1 开关，而是连续强度，随反馈学习）
    #   draw_predictions: 该联赛被判平的场次数（含基线抬升导致）
    #   draw_hits:        其中实际打平的场次数
    #   强度由 draw_strength() 计算：反馈足且准 → 强抬升；样本少/不准 → 温和试探（不清零）
    draw_predictions: int = 0
    draw_hits: int = 0
    # 自适应统计
    total_predictions: int = 0
    total_hits: int = 0
    avg_overround: float = 0.0        # 该联赛平均溢水
    last_updated: str = ""

    @property
    def hit_rate(self) -> float:
        if self.total_predictions == 0:
            return 0.0
        return self.total_hits / self.total_predictions

    @property
    def draw_precision(self) -> float:
        """判平精度（命中/判平次数）"""
        if self.draw_predictions == 0:
            return 0.0
        return self.draw_hits / self.draw_predictions

    def draw_strength(self) -> float:
        """判平抬升强度（连续自适应，永不硬关闭）

        原则：判平错了不是"关掉"，而是降强度继续试探，等反馈累积再上调。
        - 有可靠正反馈（判平>=4 且命中>=3，如巴甲 6/10、美职联 6/11）→ 强抬升 0.85
        - 有反馈但样本/精度不足（如瑞典超 0/2、巴西杯 0/1）→ 温和试探 0.35
          （基线×0.35 通常低于模型平局概率，不会硬翻盘，但保留继续积累反馈的机会）
        - 无判平反馈 → 温和 0.40 先试探

        【安全阀 2026-10-09】无论上面怎么判，返回值不得超过 draw_strength_cap。
        旧实现最高可返回 0.85，叠加一个错误的 draw_baseline 就能把平局概率
        抬到离谱位置。抬升强度应当是「温和修正」，不是「重新定向」。
        """
        s = self._raw_draw_strength()
        return min(s, self.draw_strength_cap)

    def _raw_draw_strength(self) -> float:
        if self.draw_predictions >= 4 and self.draw_hits >= 3:
            return 0.85
        if self.draw_predictions >= 2:
            return 0.35
        return 0.40


@dataclass
class LeagueParamsConfig:
    """联赛参数配置"""
    min_samples_for_adapt: int = 20   # 低于此数用默认值
    adapt_learning_rate: float = 0.05 # 参数调整步长
    # 调整范围限制（防止跑飞）
    base_goals_range: tuple = (0.8, 2.0)
    home_adv_range: tuple = (0.5, 1.5)
    market_blend_range: tuple = (0.1, 0.5)
    # 全局默认（新联赛 fallback）
    default_base_goals: float = 1.35
    default_home_adv: float = 1.0
    default_market_blend: float = 0.28


# 联赛场均进球先验（来自 DJYY league-matrix 典型值）
# 用于初始化，后续由 optimizer 覆盖
LEAGUE_PRIORS = {
    "英超": {"base_goals": 1.45, "home_adv_weight": 0.95, "market_blend_weight": 0.30},
    "西甲": {"base_goals": 1.35, "home_adv_weight": 1.05, "market_blend_weight": 0.28},
    "德甲": {"base_goals": 1.55, "home_adv_weight": 1.00, "market_blend_weight": 0.30},
    "意甲": {"base_goals": 1.25, "home_adv_weight": 1.00, "market_blend_weight": 0.25},
    "法甲": {"base_goals": 1.30, "home_adv_weight": 1.05, "market_blend_weight": 0.25},
    "欧冠": {"base_goals": 1.40, "home_adv_weight": 0.90, "market_blend_weight": 0.32},
    "欧联": {"base_goals": 1.35, "home_adv_weight": 0.95, "market_blend_weight": 0.30},
    "世界杯": {"base_goals": 1.30, "home_adv_weight": 0.70, "market_blend_weight": 0.30},
    "瑞超": {"base_goals": 1.50, "home_adv_weight": 1.05, "market_blend_weight": 0.25},
    "挪超": {"base_goals": 1.55, "home_adv_weight": 1.10, "market_blend_weight": 0.22},
    "韩K联": {"base_goals": 1.30, "home_adv_weight": 1.00, "market_blend_weight": 0.22},
    "墨西哥联": {"base_goals": 1.35, "home_adv_weight": 1.10, "market_blend_weight": 0.22},
    "中超": {"base_goals": 1.40, "home_adv_weight": 1.10, "market_blend_weight": 0.20},
    "日职": {"base_goals": 1.35, "home_adv_weight": 1.00, "market_blend_weight": 0.22},
}

# 2026-08-05 账本 113 场回算的联赛 xG 校准系数 + 平局基线
# 系数 = 实际场均总进球 / 预测场均总进球（<1=高估需下调, >1=低估需上调）
# 平局基线 = 该联赛实际平局率（模型判平几乎为 0 → 用基线兜底改判）
LEAGUE_CALIBRATION_PRIORS = {
    # xG 校准系数（<1 = 之前模型高估需下调）
    # 【2026-10-09】dixon_coles 的 attack/defense 量纲 bug 已修（改用 log 形式），
    # 这些系数原本是在给该 bug 打补丁。其绝对值需重新回测校准，
    # 在重校完成前先回到 1.0（不过度下调），由 ablation_replay 每周重拟合。
    "K1联赛":   {"xg_calibration": 1.0,  "draw_baseline": 0.28},
    "挪超":     {"xg_calibration": 1.0,  "draw_baseline": 0.26},
    "芬超":     {"xg_calibration": 1.0,  "draw_baseline": 0.25},
    "瑞典超":   {"xg_calibration": 1.0,  "draw_baseline": 0.26},
    "欧冠":     {"xg_calibration": 1.0,  "draw_baseline": 0.22},
    "美职联":   {"xg_calibration": 1.0,  "draw_baseline": 0.24},
    "巴甲":     {"xg_calibration": 1.0,  "draw_baseline": 0.27},
    "欧罗巴":   {"xg_calibration": 1.0,  "draw_baseline": 0.24},
    "巴西杯":   {"xg_calibration": 1.0,  "draw_baseline": 0.25},
    "瑞超":     {"xg_calibration": 1.0,  "draw_baseline": 0.25},
}

# 联赛平局率权威实测值（来源：data/historical/matches.csv 全量赛果统计）
# 用途：当 league_matrix.json 缺该联赛时的兼底。与上表差异时以本表为准。
#   BRAZIL_SERIE_A  4652 场 → 0.266
#   PREMIER_LEAGUE  4564 场 → 0.238
#   LA_LIGA         4564 场 → 0.259
#   SERIE_A         4566 场 → 0.259
#   BUNDESLIGA      3697 场 → 0.250
#   LIGUE_1         4266 场 → 0.255
#   PRIMEIRA_LIGA   3685 场 → 0.245
#   EREDIVISIE      3654 场 → 0.235
#   CHAMPIONS_LEAGUE 2563 场 → 0.219
#   ARGENTINE_PRIMERA 4638 场 → 0.298
DRAW_RATE_TRUTH = {
    "巴甲": 0.266, "英超": 0.238, "西甲": 0.259, "意甲": 0.259,
    "德甲": 0.250, "法甲": 0.255, "葡超": 0.245, "荷甲": 0.235,
    "欧冠": 0.219, "阿甲": 0.298, "欧联": 0.241,
    # league_matrix.json 实测（2026-08-13 快照）
    "美职联": 0.234,   # n=269
    "挪超": 0.186,     # n=129
    "瑞典超": 0.260,   # n=127
}

# 真实联赛平局率的经验区间（来源：matches.csv 20 个联赛实测，范围 0.219-0.333）
# 超出此区间的 draw_baseline 一律视为历史错值，在加载时归一。
DRAW_RATE_PLAUSIBLE_MIN = 0.18
DRAW_RATE_PLAUSIBLE_MAX = 0.36
DRAW_RATE_FALLBACK = 0.25

# 联赛名 → matches.csv competition 字段（用于回退统计）
COMPETITION_ALIASES = {
    "巴甲": "BRAZIL_SERIE_A", "英超": "PREMIER_LEAGUE", "西甲": "LA_LIGA",
    "意甲": "SERIE_A", "德甲": "BUNDESLIGA", "法甲": "LIGUE_1",
    "葡超": "PRIMEIRA_LIGA", "荷甲": "EREDIVISIE", "欧冠": "CHAMPIONS_LEAGUE",
    "阿甲": "ARGENTINE_PRIMERA_DIVISION", "欧联": "EUROPA_LEAGUE",
}


class LeagueParamsManager:
    """联赛独立参数管理器

    用法:
        mgr = LeagueParamsManager(state_path)
        params = mgr.get_params("英超")
        # 用 params.base_goals 替代全局 base_goals
        mgr.record_result("英超", hit=True)
        mgr.adapt("英超")  # optimizer 调用
    """

    def __init__(self, state_path: Path, config: Optional[LeagueParamsConfig] = None):
        self.state_path = state_path
        self.config = config or LeagueParamsConfig()
        self._params: dict[str, LeagueParam] = {}
        self._load()

    def _load(self):
        """加载持久化状态（旧文件缺新字段时用先验补齐，含判平反馈字段）"""
        if self.state_path.exists():
            try:
                raw = json.loads(self.state_path.read_text())
                for league, data in raw.items():
                    # 旧版本字段缺失 → 用账本先验补齐（2026-08-05 新增字段）
                    if "xg_calibration" not in data or "draw_baseline" not in data:
                        calib = LEAGUE_CALIBRATION_PRIORS.get(league, {})
                        data.setdefault("xg_calibration", calib.get("xg_calibration", 1.0))
                        data.setdefault("draw_baseline", calib.get("draw_baseline", 0.25))
                    # 判平反馈字段（结构升级：判平强度自适应，不硬关闭）
                    data.setdefault("draw_predictions", 0)
                    data.setdefault("draw_hits", 0)
                    self._params[league] = LeagueParam(**data)
            except Exception:
                pass
        self._migrate_draw_baseline()
        self._backfill_draw_baseline_samples()

    def _migrate_draw_baseline(self):
        """修正历史上写错的 draw_baseline（2026-10-09）

        背景：旧实现把「判平精度」（draw_hits/draw_predictions）写进了
        draw_baseline 字段，并把注释标为「该联赛实际平局率」。这两个是完全
        不同的量：巴甲判平精度 0.60，而巴甲**真实**平局率是 0.266（4652 场）。
        融合层用 target_d = draw_baseline × draw_strength 抬升平局概率，
        导致巴甲每场平局概率被钉死在 0.5025。

        由于该值是被持久化到 league_params.json 的，光改代码里的先验表
        不会生效 —— 旧值已经在磁盘上。这里做一次性迁移：
        任何与权威实测值（DRAW_RATE_TRUTH）偏差超过 draw_baseline_tolerance
        的联赛，一律改写为实测值。

        该迁移是幂等的：改写后偏差为 0，下次运行不再触发。
        """
        corrected = []
        for league, param in self._params.items():
            truth = DRAW_RATE_TRUTH.get(league)
            if truth is None:
                # 无权威实测值的联赛：若基线偏离「真实联赛平局率」的经验区间则归一。
                # 经验区间依据 matches.csv 全量（4652 场巴甲 0.266 / 4564 英超 0.238 /
                # 4566 意甲 0.259 / 4266 法甲 0.255），20 个联赛实测范围 0.219-0.333。
                # 旧阈值 0.45 形同虚设 —— 瑞典超 0.43、美职联 0.55 这类错值能直接通过。
                if (param.draw_baseline > DRAW_RATE_PLAUSIBLE_MAX
                        or param.draw_baseline < DRAW_RATE_PLAUSIBLE_MIN):
                    corrected.append((league, param.draw_baseline,
                                      DRAW_RATE_FALLBACK))
                    param.draw_baseline = DRAW_RATE_FALLBACK
                continue
            if abs(param.draw_baseline - truth) > self.config.draw_baseline_tolerance:
                corrected.append((league, param.draw_baseline, truth))
                param.draw_baseline = truth
        if corrected:
            self.save()
            self._last_migration = corrected

    def _backfill_draw_baseline_samples(self, league_matrix_path=None,
                                        matches_csv_path=None):
        """回填平局基线的样本场次数。

        draw_baseline 的可信度取决于它是由多少场比赛统计出来的。
        样本不足的联赛不应据此抬升平局概率（否则一个拍脑袋的常数会
        直接主导输出）。数据来源按优先级：
          1. league_matrix.json 的 matches 字段
          2. matches.csv 实际统计
        两者都拿不到 → 样本数为 0 → get_effective_draw_baseline 返回 0.0
        → 融合层跳过该步骤。
        """
        # 1) league_matrix.json
        if league_matrix_path is None:
            league_matrix_path = self.state_path.parent.parent / "league_matrix.json"
        lm = None
        try:
            p = Path(league_matrix_path)
            if p.exists():
                lm = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            lm = None
        if lm:
            for entry in lm.get("leagues", []):
                name = entry.get("name_zh")
                if name and name in self._params and entry.get("matches") is not None:
                    self._params[name].draw_baseline_samples = int(entry["matches"])

        # 2) matches.csv 实测（可累加，覆盖上一步）
        if matches_csv_path is None:
            matches_csv_path = self.state_path.parent.parent / "historical" / "matches.csv"
        try:
            import csv as _csv
            p = Path(matches_csv_path)
            if p.exists():
                with p.open(encoding="utf-8") as f:
                    for row in _csv.DictReader(f):
                        comp = row.get("competition")
                        if not comp:
                            continue
                        for zh, alias in COMPETITION_ALIASES.items():
                            if alias == comp and zh in self._params:
                                self._params[zh].draw_baseline_samples += 1
        except Exception:
            pass

    def save(self):
        """持久化"""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for league, param in self._params.items():
            data[league] = {
                "base_goals": param.base_goals,
                "home_adv_weight": param.home_adv_weight,
                "market_blend_weight": param.market_blend_weight,
                "xg_calibration": param.xg_calibration,
                "draw_baseline": param.draw_baseline,
                "draw_predictions": param.draw_predictions,
                "draw_hits": param.draw_hits,
                "total_predictions": param.total_predictions,
                "total_hits": param.total_hits,
                "avg_overround": param.avg_overround,
                "last_updated": param.last_updated,
            }
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def get_params(self, league: str) -> LeagueParam:
        """获取联赛参数（无则用先验/默认初始化）"""
        if league in self._params:
            return self._params[league]

        # 用先验初始化
        prior = LEAGUE_PRIORS.get(league, {})
        calib = LEAGUE_CALIBRATION_PRIORS.get(league, {})
        param = LeagueParam(
            base_goals=prior.get("base_goals", self.config.default_base_goals),
            home_adv_weight=prior.get("home_adv_weight", self.config.default_home_adv),
            market_blend_weight=prior.get("market_blend_weight", self.config.default_market_blend),
            xg_calibration=calib.get("xg_calibration", 1.0),
            draw_baseline=calib.get("draw_baseline", 0.25),
        )
        self._params[league] = param
        return param

    def get_base_goals(self, league: str) -> float:
        """获取联赛 base_goals（供 DC 模型使用）"""
        return self.get_params(league).base_goals

    def get_home_adv(self, league: str) -> float:
        """获取联赛主场优势权重"""
        return self.get_params(league).home_adv_weight

    def get_market_blend(self, league: str) -> float:
        """获取联赛市场混合权重"""
        return self.get_params(league).market_blend_weight

    def get_xg_calibration(self, league: str) -> float:
        """获取联赛 xG 校准系数（账本实证：挪超 1.13 低估 / 巴西杯 0.55 高估）"""
        return self.get_params(league).xg_calibration

    def get_draw_baseline(self, league: str) -> float:
        """获取联赛平局基线（**真实平局率**，非判平精度）

        2026-10-09：返回值经 _migrate_draw_baseline 校正，与实测值偏差
        不得超过 draw_baseline_tolerance。历史值（美职联 0.55 / 巴甲 0.60）
        已在加载时自动修正为实测值（0.234 / 0.266）。
        """
        return self.get_params(league).draw_baseline

    def get_effective_draw_baseline(self, league: str) -> float:
        """融合层应使用的平局基线（带可信度门控）。

        返回 0.0 表示「该联赛的平局基线不可信，不应据此抬升平局概率」。
        融合层拿到 0.0 时必须跳过 league_draw_baseline 步骤。
        """
        p = self.get_params(league)
        if p.draw_baseline_samples < self.config.draw_baseline_min_n:
            return 0.0
        return p.draw_baseline

    def get_draw_strength(self, league: str) -> float:
        """获取判平抬升强度（连续自适应，由该联赛判平反馈驱动，不硬关闭）"""
        return self.get_params(league).draw_strength()

    def record_draw_result(self, league: str, was_draw: bool, hit: bool):
        """记录判平反馈：was_draw=该场最终是否判平, hit=判平且实际打平"""
        if not was_draw:
            return
        param = self.get_params(league)
        param.draw_predictions += 1
        if hit:
            param.draw_hits += 1
        self.save()

    def record_result(self, league: str, hit: bool, overround: float = 0.0):
        """记录一场预测结果"""
        from datetime import date
        param = self.get_params(league)
        param.total_predictions += 1
        if hit:
            param.total_hits += 1
        if overround > 0:
            # 指数移动平均
            alpha = 0.1
            param.avg_overround = (1 - alpha) * param.avg_overround + alpha * overround
        param.last_updated = date.today().isoformat()
        self.save()

    def adapt(self, league: str):
        """自适应调参（由 optimizer 定期调用）

        规则:
          - 命中率 < 45%: 增大 market_blend_weight（更信任市场）
          - 命中率 > 60%: 减小 market_blend_weight（更信任模型）
          - 高溢水联赛: 减小 market_blend（市场定价偏差大）
        """
        param = self.get_params(league)
        cfg = self.config

        if param.total_predictions < cfg.min_samples_for_adapt:
            return  # 样本不足，不调整

        hr = param.hit_rate
        lr = cfg.adapt_learning_rate

        # 命中率低 → 更信任市场
        if hr < 0.45:
            param.market_blend_weight = min(
                cfg.market_blend_range[1],
                param.market_blend_weight + lr,
            )
        # 命中率高 → 更信任模型
        elif hr > 0.60:
            param.market_blend_weight = max(
                cfg.market_blend_range[0],
                param.market_blend_weight - lr,
            )

        # 高溢水 → 市场定价偏差大，降低市场权重
        if param.avg_overround > 0.12:
            param.market_blend_weight = max(
                cfg.market_blend_range[0],
                param.market_blend_weight - lr * 0.5,
            )

        # 范围限制
        param.base_goals = max(cfg.base_goals_range[0],
                               min(cfg.base_goals_range[1], param.base_goals))
        param.home_adv_weight = max(cfg.home_adv_range[0],
                                    min(cfg.home_adv_range[1], param.home_adv_weight))
        param.market_blend_weight = max(cfg.market_blend_range[0],
                                        min(cfg.market_blend_range[1], param.market_blend_weight))

        self.save()

    def adapt_all(self):
        """对所有联赛执行自适应"""
        for league in self._params:
            self.adapt(league)

    def update_from_league_matrix(self, matrix: dict):
        """从 DJYY league-matrix 更新先验（场均进球等）

        Args:
            matrix: DJYY /data/league-matrix.json 的内容
        """
        if not matrix:
            return

        # matrix 格式取决于实际结构，尝试提取场均进球
        for league_name, stats in matrix.items():
            if not isinstance(stats, dict):
                continue
            avg_goals = stats.get("avg_goals") or stats.get("average_goals")
            if avg_goals and isinstance(avg_goals, (int, float)):
                param = self.get_params(league_name)
                # 只在样本不足时用 league-matrix 更新
                if param.total_predictions < self.config.min_samples_for_adapt:
                    # 场均进球 / 2 ≈ 每队期望进球（base_goals 的物理含义）
                    param.base_goals = round(avg_goals / 2, 3)

        self.save()

    def summary(self) -> dict:
        """所有联赛参数摘要"""
        result = {}
        for league, param in sorted(self._params.items()):
            result[league] = {
                "base_goals": param.base_goals,
                "home_adv": param.home_adv_weight,
                "market_blend": param.market_blend_weight,
                "hit_rate": round(param.hit_rate, 3),
                "n": param.total_predictions,
            }
        return result
