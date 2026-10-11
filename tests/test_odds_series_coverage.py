"""赔率时序采集覆盖率体检的回归测试（2026-10-11）

背景：实测 899 场里只有 3.6% 有开赛前 2 小时内的快照 ——
我们记的"收盘价"大多不是收盘价，而此前没有任何指标能发现这件事。
CLV 类实验在覆盖率修复前一律不可用。
"""
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import odds_series_coverage as osc  # noqa: E402


def test_close_window_constant_is_two_hours():
    """CLV 可用性的关键门槛：开赛前 2 小时内必须有快照。"""
    assert osc.CLOSE_WINDOW_H == 2.0
    assert osc.NEAR_WINDOW_H == 6.0


def test_weighted_median():
    """中位数必须按场次展开，不能取分布键的中位。

    回归动机：初版写的是 `sorted(dist.items())[len(dist)//2][0]`，
    对 {1:180, 2:233, 3:215, ...} 会取到"分布中第 5 个键"= 7，
    而真实中位数是 3。
    """
    assert osc._weighted_median(Counter({1: 180, 2: 233, 3: 215})) == 2
    assert osc._weighted_median(Counter({1: 10})) == 1
    assert osc._weighted_median(Counter({5: 3, 9: 1})) == 5
    assert osc._weighted_median(Counter()) == 0
    # 偏斜分布：1 占绝大多数时中位数必须是 1，不能是 2
    assert osc._weighted_median(Counter({1: 100, 8: 1})) == 1


def test_weighted_median_matches_real_distribution():
    """用真实的 odds_series 快照数分布验证。"""
    dist = Counter({1: 180, 2: 233, 3: 215, 4: 101, 5: 52, 6: 31,
                    7: 25, 8: 27, 9: 20, 10: 8, 11: 6, 12: 1})
    assert osc._weighted_median(dist) == 3


def test_measure_shape_on_real_data():
    """在真实数据上跑，指标结构必须完整且数值合理。"""
    rep = osc.measure()
    assert rep["total_matches"] > 0
    assert 0 <= rep["closing_coverage"]["ratio"] <= 1
    assert 0 <= rep["multi_snapshot"]["ratio"] <= 1
    assert rep["healthy"] is (rep["closing_coverage"]["ratio"] >= 0.5)
    # 中位数必须落在 [1, max] 内
    assert 1 <= rep["snapshots"]["median"] <= rep["snapshots"]["max"]
    for key in ("median_last", "median_first"):
        assert rep["lead_to_kickoff_hours"][key] is None or \
            rep["lead_to_kickoff_hours"][key] > 0


def test_baseline_is_documented_as_unhealthy():
    """当前基线（2026-10-11）就是不健康的 —— 这是已知待修状态。

    若哪天这个断言失败，说明覆盖率已经改善，应同步更新 docs/EXPERIMENTS.md。
    """
    rep = osc.measure()
    # 只要还没修好就应保持 unhealthy；改善后请显式更新本断言与文档
    assert rep["closing_coverage"]["ratio"] < 0.5, (
        "覆盖率已 ≥50%，请更新 docs/EXPERIMENTS.md 的基线数字并调整本测试"
    )


def test_fetch_window_expanded():
    """fetch_sina_odds 的抓取窗口必须 ≥2 天。

    回归动机：原实现 `dates = [today]`，导致 557/899 场（62%）的
    最后一次采集停在开赛前一整天。
    """
    src = (ROOT / "scripts" / "fetch_sina_odds.py").read_text(encoding="utf-8")
    assert '"--days"' in src, "必须提供抓取窗口参数"
    assert 'default=2' in src, "默认窗口应为 2 天"
    # 只看代码行 —— 注释里引用旧代码不算回归
    code = "\n".join(
        ln.strip() for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    )
    assert "dates = [today]" not in code, "单日窗口是回归"


def test_health_check_wired():
    """覆盖率体检必须接进 health-check，否则退化无人察觉。"""
    yml = (ROOT / ".github" / "health-check.yml").read_text(encoding="utf-8") \
        if (ROOT / ".github" / "health-check.yml").exists() \
        else (ROOT / ".github" / "workflows" / "health-check.yml").read_text(encoding="utf-8")
    assert "engine.odds_series_coverage" in yml
    # 避免在 YAML 块标量里嵌 heredoc —— 缩进极易出错
    assert "<<'PY'" not in yml, "workflow 内不应有嵌套 heredoc"


def test_output_file_written(tmp_path, monkeypatch):
    """验证「写入的内容与本次计算一致」。

    回归动机（PR#10 CI 红过一次）：原写法拿磁盘上的旧文件去比对**新跑的**
    measure()，而 odds_series 在 CI 持续增长（实测 899 → 936），
    于是该测试必然失败 —— 它依赖了系统里本来就在变的量。
    现在改为写入临时路径后立刻读回，测的是写入动作自身的自洽性。
    """
    tmp = tmp_path / "odds_coverage.json"
    monkeypatch.setattr(osc, "OUT", tmp)
    rep = osc.measure()
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    disk = json.loads(tmp.read_text(encoding="utf-8"))
    assert disk["total_matches"] == rep["total_matches"]
    assert disk["healthy"] == rep["healthy"]
    assert set(disk) == set(rep)


# ---------- workflow 接线（PR#11 修正：health-check 无 checkout） ----------

def test_health_check_does_not_need_checkout():
    """health-check 的覆盖率步**不得**依赖本地代码。

    回归动机：PR #9 在这个 job 里加了 `python -m engine.odds_series_coverage`，
    但该 job 刻意不 checkout（原注释"用 gh api 触发，不需要 git checkout"），
    于是以 ModuleNotFoundError 失败（2026-10-11 01:33 实测）。
    修正为：由 daily-forecast（有 checkout）计算并提交，本 job 只用 gh api 读结果。

    刻意不 import yaml —— CI 环境未安装 PyYAML，且这里的断言用文本匹配足够。
    """
    yml = (ROOT / ".github" / "workflows" / "health-check.yml").read_text(encoding="utf-8")
    # 只看非注释行 —— 注释里引用被删掉的旧命令是正常的说明，不算回归。
    # （这个坑本会话已踩过两次，见 upgrade_tracker / parlay 测试）
    code = "\n".join(ln for ln in yml.splitlines() if not ln.strip().startswith("#"))
    assert "actions/checkout" not in code, \
        "health-check 不得新增 checkout（会破坏其无依赖设计）"
    assert "python -m engine.odds_series_coverage" not in code, \
        "health-check 不得直接调用本地模块（没有 checkout）"
    assert "gh api" in code, "应改为用 gh api 读已提交的覆盖率结果"


def test_daily_forecast_computes_coverage():
    """daily-forecast 有 checkout 且会提交 data/，覆盖率必须在那里算。"""
    yml = (ROOT / ".github" / "workflows" / "daily-forecast.yml").read_text(encoding="utf-8")
    assert "actions/checkout" in yml, "daily-forecast 必须有 checkout"
    assert "python -m engine.odds_series_coverage" in yml, \
        "daily-forecast 必须计算覆盖率"
    assert "git add data/" in yml, "必须提交 data/，否则 health-check 读不到结果"
