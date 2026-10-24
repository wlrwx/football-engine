from __future__ import annotations
"""增强预测模块 - 移植自 lottery-football 的核心算法"""
import csv
import hashlib
import math
import unicodedata
from pathlib import Path

import numpy as np


# ============================================================
# 赛事类型权重（借鉴 lottery-football）
# ============================================================
MATCH_TYPE_WEIGHTS = {
    "OFFICIAL": 1.0,
    "LEAGUE": 1.0,
    "CUP": 1.0,
    "WORLD_CUP": 1.0,
    "CHAMPIONS_LEAGUE": 1.0,
    "INTERNATIONAL_FRIENDLY": 0.5,
    "CLUB_FRIENDLY": 0.3,
    "FRIENDLY": 0.3,
}


# ============================================================
# 收缩公式（Empirical Bayes shrinkage toward 1.0）
# ============================================================
SHRINK_WEIGHT = 8.0  # 8场=50%收缩, 16场=67%置信度


def shrink_rating(raw_value: float, weight: float) -> float:
    """
    将原始攻防值向联赛平均(1.0)收缩。
    weight 越大（比赛越多），越信任原始值。
    借鉴 lottery-football: confidence = weight / (weight + 8.0)
    """
    confidence = weight / (weight + SHRINK_WEIGHT)
    return 1.0 + (raw_value - 1.0) * confidence


# ============================================================
# 时间衰减（几何插值，借鉴 DixonColesWeightModel）
# ============================================================
TIME_DECAY_ANCHORS = [
    (0, 1.000), (7, 0.987), (30, 0.946), (90, 0.844),
    (180, 0.712), (365, 0.507), (730, 0.258),
]


def time_decay_weight(days_ago: int) -> float:
    """
    分段几何插值时间衰减。
    在每个区间内做 log-space 线性插值（等价于几何插值）。
    半衰期约 365 天。
    """
    if days_ago <= 0:
        return 1.0
    anchors = TIME_DECAY_ANCHORS
    if days_ago >= anchors[-1][0]:
        # 外推：用最后一段的衰减率
        d0, w0 = anchors[-2]
        d1, w1 = anchors[-1]
        rate = math.log(w1) - math.log(w0)
        span = d1 - d0
        extra = days_ago - d1
        return w1 * math.exp(rate * extra / span)

    for i in range(len(anchors) - 1):
        d0, w0 = anchors[i]
        d1, w1 = anchors[i + 1]
        if d0 <= days_ago <= d1:
            progress = (days_ago - d0) / (d1 - d0)
            log_w = math.log(w0) + progress * (math.log(w1) - math.log(w0))
            return math.exp(log_w)

    return anchors[-1][1]


# ============================================================
# 交锋因子 H2H（借鉴 lottery-football）
# ============================================================
def _same_team(name_a: str, name_b: str) -> bool:
    """队名同一性判定：先走 aliases 归一（"皇马"→"皇家马德里" 这类短名
    别名只有 aliases 表认得），再兜底 canonical + 子串匹配。
    归一化失败时退化为原始字符串比较，不抛异常。
    """
    try:
        from engine.team_aliases import normalize_team

        a, b = normalize_team(name_a), normalize_team(name_b)
    except Exception:
        a, b = name_a, name_b
    return a == b or team_names_match(a, b)


def head_to_head_factor(
    home_team: str,
    away_team: str,
    h2h_history: list[dict],
) -> float:
    """
    从历史交锋计算调整因子。
    加权平均净胜球 × 0.04，限制在 [0.90, 1.10]。
    乘到主队 lambda，除到客队 lambda。
    """
    if not h2h_history:
        return 1.0

    total_diff = 0.0
    total_weight = 0.0

    for match in h2h_history:
        days = match.get("days_ago", 365)
        match_type = match.get("match_type", "OFFICIAL")
        w = time_decay_weight(days) * MATCH_TYPE_WEIGHTS.get(match_type, 1.0)

        # 计算净胜球（从主队视角）
        # 2026-10-23 修复：原来用精确字符串比较，队名别名不一致时（如
        # "皇马" vs "皇家马德里"）会走 else 分支把净胜球算反，修正方向错误。
        # 统一走 team_aliases.normalize_team（短名别名只有 aliases 表认得，
        # 子串规则对 <=3 字的短名无效），两边都认不出来时跳过该场，
        # 宁可不加权也不猜方向。
        home_key = match.get("home_team", "")
        if _same_team(home_key, home_team):
            diff = match["home_score"] - match["away_score"]
        elif _same_team(home_key, away_team):
            diff = match["away_score"] - match["home_score"]
        else:
            continue

        total_diff += diff * w
        total_weight += w

    if total_weight == 0:
        return 1.0

    avg_diff = total_diff / total_weight
    factor = 1.0 + avg_diff * 0.04
    return max(0.90, min(1.10, factor))


# ============================================================
# Top-3 比分预测（从蒙特卡洛结果提取）
# ============================================================
def top_score_predictions(
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    top_n: int = 3,
) -> list[tuple[int, int, float]]:
    """
    从蒙特卡洛模拟结果中提取最可能的比分。
    返回 [(home_score, away_score, probability), ...]
    """
    n = len(home_goals)
    # 统计每个比分的出现次数
    scores = home_goals * 20 + away_goals  # 编码为唯一整数
    unique, counts = np.unique(scores, return_counts=True)

    # 取 top N
    top_idx = np.argsort(counts)[::-1][:top_n]
    results = []
    for idx in top_idx:
        encoded = unique[idx]
        h = int(encoded // 20)
        a = int(encoded % 20)
        prob = counts[idx] / n
        results.append((h, a, round(float(prob), 4)))

    return results


def top_total_goals(
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    top_n: int = 3,
) -> list[tuple[int, float]]:
    """最可能的总进球数"""
    totals = home_goals + away_goals
    unique, counts = np.unique(totals, return_counts=True)
    top_idx = np.argsort(counts)[::-1][:top_n]
    n = len(totals)
    return [(int(unique[idx]), round(float(counts[idx]) / n, 4)) for idx in top_idx]


def total_goals_from_xg(home_xg: float, away_xg: float) -> list[tuple[int, float]]:
    """从 xG 直接算完整总进球分布（0~7+球，泊松求和）。

    2026-08-14 修复：原 total_goals 来自 MC 模拟的 top-6 截断分布，
    期望值 ~1.84 球，与模型自己的 xG ~2.74 球严重矛盾（系统性低估进球，
    导致总进球 EV 偏向 0/1/2 球低比分）。改为用 xG 泊松直接算完整分布，
    与 xG 口径一致，且含 6+/7+ 尾部分布。
    """
    if home_xg <= 0 and away_xg <= 0:
        return []
    lam = max(0.1, home_xg) + max(0.1, away_xg)
    out = []
    for g in range(0, 8):
        p = math.exp(-lam) * (lam ** g) / math.factorial(g)
        out.append((g, round(float(p), 4)))
    # 8+ 并入 7+
    p7_plus = 1.0 - sum(p for _, p in out)
    if p7_plus > 0:
        out[7] = (7, round(out[7][1] + p7_plus, 4))
    return out


# ============================================================
# 确定性种子（保证可复现）
# ============================================================
def _stable_hash(text: str) -> int:
    """跨进程稳定的字符串哈希。

    2026-10-23 修复：原实现用内置 `hash()`。Python 对 str 的哈希受
    PYTHONHASHSEED 随机化影响（默认随机），同一 match_id 在不同进程里
    得到的种子不同 → "相同输入永远相同输出" 只在单进程内成立。
    改为 sha256 截断，跨进程/跨机器稳定。
    """
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def build_seed(match_id: str, home_xg: float, away_xg: float, simulations: int) -> int:
    """
    确定性种子：相同输入永远产生相同输出（跨进程亦然）。
    借鉴 lottery-football 的 seed 构建方式。
    """
    seed = 1125899906842597
    seed = seed * 31 + _stable_hash(str(match_id))
    seed = seed * 31 + _stable_hash(f"{round(home_xg, 4):.4f}")
    seed = seed * 31 + _stable_hash(f"{round(away_xg, 4):.4f}")
    seed = seed * 31 + int(simulations)
    return abs(seed) % (2**63)


# ============================================================
# 模糊队名匹配（借鉴 lottery-football 的 canonicalTeamName）
# ============================================================
def canonical_team_name(name: str) -> str:
    """
    规范化队名：NFKC → 大写 → 去后缀/标点 → 去 FC/SC 前后缀。
    """
    import re
    s = unicodedata.normalize("NFKC", name).upper()
    # 去掉俱乐部后缀
    s = s.replace("足球俱乐部", "").replace("俱乐部", "")
    # 去掉标点和空格
    s = re.sub(r"[\s·•.．,，''`´()（）\[\]【】\-_/&]+", "", s)
    # 去掉 FC/SC/CF 前缀（后接汉字时）
    s = re.sub(r"^(FC|SC|CF)(?=[\u4e00-\u9fff])", "", s)
    # 去掉尾部缩写
    s = re.sub(r"(AIF|FC|SC|CF|SK|FK|IF|BK|FF)$", "", s)
    return s


def team_names_match(name_a: str, name_b: str) -> bool:
    """
    判断两个队名是否匹配。
    精确匹配 或 子串包含（最短 >= 4 字符）。
    """
    a = canonical_team_name(name_a)
    b = canonical_team_name(name_b)
    if a == b:
        return True
    min_len = min(len(a), len(b))
    if min_len >= 4:
        return a in b or b in a
    return False


# ============================================================
# H2H 历史加载器
# ============================================================
class H2HLoader:
    """从历史数据加载交锋记录"""

    def __init__(self, matches_path: Path):
        self.matches_path = matches_path
        self._cache: dict[str, list[dict]] = {}
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        if not self.matches_path.exists():
            self._loaded = True
            return

        from datetime import date
        today = date.today()

        with open(self.matches_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                home = row.get("home_team", "")
                away = row.get("away_team", "")
                if not home or not away:
                    continue

                match_date = row.get("date", "")
                try:
                    from datetime import datetime
                    d = datetime.strptime(match_date, "%Y-%m-%d").date()
                    days_ago = (today - d).days
                except (ValueError, TypeError):
                    days_ago = 365

                entry = {
                    "home_team": home,
                    "away_team": away,
                    "home_score": int(row.get("home_score", 0)),
                    "away_score": int(row.get("away_score", 0)),
                    "days_ago": days_ago,
                    "match_type": row.get("match_type", "OFFICIAL"),
                }

                # 双向索引
                key1 = f"{home}|{away}"
                key2 = f"{away}|{home}"
                self._cache.setdefault(key1, []).append(entry)
                self._cache.setdefault(key2, []).append(entry)

        self._loaded = True

    def get_h2h(self, home_team: str, away_team: str, max_matches: int = 50) -> list[dict]:
        """获取两队交锋历史（最多 50 场）"""
        self._ensure_loaded()
        key = f"{home_team}|{away_team}"
        matches = self._cache.get(key, [])
        # 按时间排序（最近的在前）
        matches.sort(key=lambda m: m["days_ago"])
        return matches[:max_matches]
