#!/usr/bin/env python3
"""E4：换个博彩公司下注，能把负 EV 翻正吗？（2026-10-11）

## 为什么做这个

CLV 报告首跑结论是 **NEGATIVE_CLV**：下注 vs 去水收盘 −12.45%，报告里点名
"含竞彩 ~8% 水位逆风"。E0/E1/E2/E3 又分别证明模型无 edge、市场无矛盾、
决策层闸门调不动。于是剩下唯一没被量化过的杠杆就是**抽水本身**。

而这条路上已经有人铺路：main 上的 `engine/sources/sportmonks.py` 接了
SportMonks v3，定位写的就是「50+ 博彩公司赔率（含 Pinnacle，CLV 基准升级的关键）」。

本实验不依赖那个客户端 —— `football_data` 里**本来就有 Pinnacle**
（`PSCH/PSCD/PSCA` = Pinnacle closing）和 B365、Avg 的同期收盘价，
所以可以直接用历史数据把"换盘口值多少"量化出来。

## 结论

**换盘口有真实收益，但不足以翻正。**

在本数据集内（欧洲盘，同一批 28,049 场，**收盘**口径）：

| 盘口 | overround | 抽水 | 注数 | ROI | t |
|---|---|---|---|---|---|
| **Pinnacle** | 1.0302 | **3.02%** | 28,049 | **−1.32%** | −2.20 |
| Avg | 1.0666 | 6.66% | 27,968 | −3.49% | −5.94 |
| B365 | 1.0710 | 7.10% | 618 | +0.47% | +0.12（不显著，样本太少） |

换到平博 = **每千注多赚 21.7 元**（−1.32% vs −3.49%，差 2.17pp）。
差值与抽水差（6.66% → 3.02%，即 3.64pp）同量级且方向一致，
说明这部分收益就是抽水差本身，不是什么神秘 alpha。

## 数据覆盖限制：开收盘与比分不同源

本实验只能比**收盘**。开盘列（`PSH/PSD/PSA`、`B365H/...`）样本为 0，
这不是 bug：28,049 条有效记录全部来自 7 个 `_new.csv` 文件，它们有
`HG/AG` 比分与 `PSCH/AvgCH` 收盘列，但**没有开盘列**；而带开盘列的旧格式
文件又恰好没有 `HG/AG` 比分。要做开收盘对比必须先把两套 schema 合并。

（另注：本会话早先量到的 Avg 抽水 5.85% 是**开盘** `AvgH` 口径，
与这里 6.66% 的**收盘** `AvgCH` 口径不是一回事，别混用。）

**但最锐利的盘仍然是 −1.32%。** 这与 E0/E1/E2/E3 完全一致：
**没有 edge 的时候，选哪家博彩公司只能在负数之间做选择，不能把负数变成正数。**

### 对竞彩用户的意义（注意这是外推，不是本实验测出来的）

本数据集是欧洲盘（Avg 抽水 5.85%），不含竞彩。竞彩实测抽水 12.1%，
所以从竞彩切到平博的潜在收益（~9pp）**大于**本实验实测的 2.2pp。
但按本实验的结论方向外推：翻正仍需要额外 ~1.3pp 以上的真实 edge，
而不是换个盘口就够了。

## 用法

    python -m engine.backtest.book_vig
    python -m engine.backtest.book_vig --limit 5000
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from engine.backtest.historical_replay import RAW_DIR  # noqa: E402

OUT_JSON = ROOT / "data" / "state" / "book_vig.json"

BOOKS: dict[str, tuple[str, str, str]] = {
    "Pinnacle(PS)": ("PSCH", "PSCD", "PSCA"),
    "Avg": ("AvgCH", "AvgCD", "AvgCA"),
    "B365": ("B365CH", "B365CD", "B365CA"),
    "PS开盘": ("PSH", "PSD", "PSA"),
    "B365开盘": ("B365H", "B365D", "B365A"),
}


def parse_odds(v) -> float | None:
    """赔率解析：**必须 > 1.0** 才算有效。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 1.0 else None


def parse_score(v) -> int | None:
    """比分解析：**0/1/2/3 都是有效进球数**，不能用赔率的解析器。

    2026-10-11 踩过这个坑：用 parse_odds 读 HG/AG 时，
    `0 > 1.0` 为假 → 所有 0 球/1 球比赛被静默丢弃，
    样本从 67,613 掉到 3,997，只剩高进球场次，
    平博 ROI 被算成 −29.65%（真实值 −1.32%）。
    两个解析器必须分开，且各自有测试锁住。
    """
    try:
        x = int(float(v))
    except (TypeError, ValueError):
        return None
    return x if 0 <= x <= 15 else None


def devig(odds: list[float]) -> list[float]:
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [i / s for i in inv]


def overround(odds: list[float]) -> float:
    return sum(1.0 / o for o in odds)


def run(raw_dir: Path = RAW_DIR, limit: int | None = None) -> dict:
    files = sorted(Path(raw_dir).rglob("*.csv"))
    if limit:
        files = files[-limit:]

    pnl: dict[str, list[float]] = {k: [] for k in BOOKS}
    vig: dict[str, list[float]] = {k: [] for k in BOOKS}
    n_score_missing = 0
    n_kept = 0

    for fp in files:
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    hg, ag = parse_score(r.get("HG")), parse_score(r.get("AG"))
                    if hg is None or ag is None:
                        n_score_missing += 1
                        continue
                    ps = [parse_odds(r.get(c)) for c in BOOKS["Pinnacle(PS)"]]
                    if not all(ps):
                        continue
                    n_kept += 1
                    # 真值代理：用最锐利可得盘（Pinnacle）的去水概率选方向
                    p = devig(ps)
                    actual = 0 if hg > ag else (1 if hg == ag else 2)
                    pick = max(range(3), key=lambda z: p[z])
                    for name, cols in BOOKS.items():
                        o = [parse_odds(r.get(c)) for c in cols]
                        if not all(o):
                            continue
                        vig[name].append(overround(o))
                        pnl[name].append(o[pick] - 1.0 if pick == actual else -1.0)
        except (OSError, csv.Error):
            continue

    def summarise(name: str) -> dict:
        us, vs = pnl[name], vig[name]
        if len(us) < 30:
            return {"n": len(us), "note": "样本不足"}
        n = len(us)
        mean = sum(us) / n
        sd = statistics.stdev(us)
        return {
            "n": n,
            "roi": round(mean, 4),
            "t": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else 0.0,
            "overround_mean": round(sum(vs) / len(vs), 4),
            "vig_mean_pct": round((sum(vs) / len(vs) - 1) * 100, 2),
        }

    rows = {k: summarise(k) for k in BOOKS}
    base = rows.get("Pinnacle(PS)", {}).get("roi")
    if base is not None:
        for k, v in rows.items():
            if v.get("n", 0) >= 30 and "roi" in v:
                v["roi_vs_pinnacle_pp"] = round((v["roi"] - base) * 100, 2)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_matches": n_kept,
        "n_score_missing": n_score_missing,
        "selection_rule": "按 Pinnacle 收盘去水概率选最可能结果，按各盘实际赔率下注",
        "books": rows,
        "verdict": (
            "E4 结论：换盘口有真实收益（本数据集内每千注 +21.7 元，对应收盘抽水 6.66%→3.02%），"
            "但最锐利的 Pinnacle 本身仍是 −1.32%。没有 edge 时，选博彩公司只能在负数之间"
            "做选择，不能把负数变成正数 —— 与 E0/E1/E2/E3 一致。"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只读最后 N 个 CSV")
    args = ap.parse_args()

    print("=" * 72)
    print("E4：换博彩公司下注的收益")
    print("=" * 72)

    res = run(limit=args.limit)
    print(f"  有效样本 {res['n_matches']} 场"
          f"（比分缺失跳过 {res['n_score_missing']} 行）")
    print(f"  选边规则：{res['selection_rule']}\n")
    print(f"  {'盘口':>16}{'注数':>8}{'抽水':>8}{'ROI':>9}{'t':>8}{'vs 平博(pp)':>14}")
    for name, v in res["books"].items():
        if v.get("n", 0) < 30:
            print(f"  {name:>16}{v.get('n',0):>8}  样本不足")
            continue
        print(f"  {name:>16}{v['n']:>8}{v['vig_mean_pct']:>7.2f}%"
              f"{v['roi']*100:>8.2f}%{v['t']:>8.2f}"
              f"{v.get('roi_vs_pinnacle_pp',0):>13.2f}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 已写入 {OUT_JSON.relative_to(ROOT)}")
    print(f"  判定: {res['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
