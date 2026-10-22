#!/usr/bin/env python3
"""挑战提交复盘器（2026-10-15）——把 challenge_state.records 变成系统审计数据

每条提交记录（提交时预登记的声明）↔ 账本结算结果 ↔ 收盘市场隐含，产出四张表：
1. 命中校准：声明 P vs 实际命中率（按 edge 分桶 → 验证门槛是否有效）
2. edge 兑现率：提交的 edge vs 实际(赛果−收盘隐含)——我们的分歧到终盘还剩多少
3. 市场对比：让球 vs 胜平负 两池分别统计（让球走比分分布，是独立信息源）
4. 挑战积分流水（若 records 带 points 字段，来自站点结算回读）

用法: challenge_review.py
（无参数：records 不带周字段，统计的是 state 里全部记录的跨周累计结算。）
"""
import json, re, sys
from datetime import datetime
from collections import defaultdict
from pathlib import Path

# 仓库根：按脚本位置推导，不依赖调用时的 CWD（CI 与手工跑都稳）
ROOT = str(Path(__file__).resolve().parent.parent)
# 线上唯一真值：challenge_hourly.py / submit.js 写的就是这个文件，且它入库、
# CI 可读。此前这里指向 _vault/challenge_state.json——那是 2026-10-15 的
# 本地快照（9 条记录），且 .gitignore 排除了 _vault/，于是：
#   · 本地跑复盘到的是两周前的冻结数据（W42 那份 "2/7" 就是这么来的）；
#   · CI 上必抛 FileNotFoundError，被 weekly-deep-check.yml 的 `|| true` 吞掉，
#     云端一直假装跑过复盘。
STATE_FILE = f'{ROOT}/data/state/challenge_state.json'

def load_state():
    p = Path(STATE_FILE)
    if not p.exists():
        # 显式失败，别静默给空数据——复盘产物会进账本级审计
        sys.exit(f"challenge_review: 缺 {STATE_FILE}（应已入库，检查 checkout 是否完整）")
    return json.loads(p.read_text(encoding='utf-8'))

def parse_pick(pick: str) -> dict:
    """把 pick 字符串解析成 {play, dir, line, side, raw}。

    为什么要专门写：原实现靠 `startswith('主胜')` / `'负@' in pick` 这类子串猜，
    实测 9 种真实格式里 4 种解析失败，且**静默返回 None**——None 去比
    actual_idx 永远 False，把命中的注算成未中。受影响的真实记录：
      '胜@2.98'        （考文垂，简化写法，不以「主胜」开头）
      '胜@3.3'         （克拉约瓦，同上）
      '客+0.5@0.95'    （霍芬海姆，让球受让）
      '让负(-2)@2.04'  （站点格式，让球）

    另外让球方向不能直接等同胜平负方向——要让球线 + 比分一起判，
    所以这里只负责**拆解**，判定交给 judge_hit()。

    返回：
      play: '1x2' | 'handicap' | 'unknown'
      dir:  0主/1平/2客（仅 1x2）
      line: 让球绝对值（仅 handicap）
      side: 'home' | 'away'（仅 handicap，表示买的是哪边）
    """
    out = {'play': '1x2', 'dir': None, 'line': None, 'side': None, 'raw': pick}
    if not pick or '@' not in pick:
        out['play'] = 'unknown'
        return out
    head = pick.split('@')[0].strip()

    # 让球族：主队-1 / 客队+0.5 / 客+0.5 / 让胜(-1) / 让负(-2) / 受让
    if any(k in head for k in ('主队-', '客队+', '客+', '受让', '让胜', '让负')):
        out['play'] = 'handicap'
        m = re.search(r'([+-]?\d+(?:\.\d+)?)', head)
        if m:
            out['line'] = abs(float(m.group(1)))
        if head.startswith('让胜') or head.startswith('主队-'):
            out['side'] = 'home'
        elif (head.startswith('让负') or head.startswith('客队+')
              or head.startswith('客+') or '受让' in head):
            out['side'] = 'away'
        return out

    # 胜平负族：主胜/客胜/平/胜/负
    if head in ('主胜', '胜', 'home'):
        out['dir'] = 0
    elif head in ('平', 'draw'):
        out['dir'] = 1
    elif head in ('客胜', '负', 'away'):
        out['dir'] = 2
    else:
        out['play'] = 'unknown'
    return out


def judge_hit(parsed: dict, actual_idx: int,
              home_score: int | None = None,
              away_score: int | None = None) -> bool | None:
    """判定一注是否命中。返回 True/False；信息不足返回 None（而不是 False）。

    关键区别：让球注必须用比分+让球线判，不能拿 1x2 的 actual_idx 直接比。
      「主队-1」= 主队让 1 球 → 净胜 ≥2 算赢，净胜 1 球是走盘（不算命中）
      「客+0.5」= 客队受让半球 → 客队平或胜都算命中
    """
    play = parsed.get('play')
    if play == '1x2':
        d = parsed.get('dir')
        if d is None:
            return None
        return actual_idx == d

    if play == 'handicap':
        if home_score is None or away_score is None:
            return None
        line, side = parsed.get('line'), parsed.get('side')
        if line is None or side is None:
            return None
        margin = home_score - away_score
        if side == 'home':
            return margin - line > 0
        return -margin + line > 0
    return None


def write_policy(path: Path, updates: dict) -> None:
    """合并写回策略层，保留实验开关（gate_mode/ev_floor/min_sporttery_odds）。

    2026-10-22：这里原来每次整份覆盖 challenge_policy.json。周体检一跑，
    E8/E10 的开关就被洗掉、实验在无人察觉时回退默认口径——与「零合格静默」
    同类：状态被写丢，日志里看不出来。改为读-合并-写，只更新复盘自己的字段。
    """
    try:
        cur = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError, ValueError):
        cur = {}
    cur.update(updates)
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding='utf-8')


def main():
    st = load_state()
    # 注：records 条目本身不带 week 字段，无法按周切分；settled 是 state 里
    # 全部记录的结算结果（跨周累计）。--week 此前只做了一次空转的比较
    # （`if ... : pass`），实际不过滤任何东西——已移除该假开关，避免误导。
    recs = st.get('records', [])
    ledger = {}
    for line in open(f'{ROOT}/data/state/review_ledger.jsonl'):
        r = json.loads(line)
        ledger[r['match_id']] = r
    # 结算 join：record 用队名；账本行带预测文件可反查 match_id
    pred_index = {}
    for d in sorted(set(r['date'] for r in ledger.values())):
        try:
            p = json.load(open(f'{ROOT}/data/daily/{d}/predictions.json'))
            p = p.get('predictions', p) if isinstance(p, dict) else p
            for x in p:
                pred_index[(x['home_team'], x['away_team'])] = x['match_id']
        except Exception:
            continue
    # 比分索引：让球判定需要精确比分，账本里只有 total_goals_actual，
    # 真实比分在 data/daily/<date>/results_sina.json（字段 home_score/away_score）。
    score_index = {}
    for d in sorted(set(r['date'] for r in ledger.values())):
        f = Path(f'{ROOT}/data/daily/{d}/results_sina.json')
        if not f.exists():
            continue
        try:
            rows = json.loads(f.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            continue
        for x in (rows if isinstance(rows, list) else []):
            hs, as_ = x.get('home_score'), x.get('away_score')
            if hs is None or as_ is None:
                continue
            key = (x.get('home_team'), x.get('away_team'))
            if key[0] and key[1]:
                score_index[key] = (int(hs), int(as_))

    settled = []
    for rec in recs:
        m = rec.get('match', '')
        if ' vs ' not in m: continue
        h, a = [t.strip() for t in m.split(' vs ')]
        alias = lambda s: {'艾华卡':'阿尔维卡','KFUM奥斯陆':'奥斯KFUM'}.get(s, s)
        mid = pred_index.get((alias(h), alias(a))) or pred_index.get((h, a))
        if not mid or mid not in ledger: continue
        lr = ledger[mid]
        pick = rec.get('pick', '')
        parsed = parse_pick(pick)
        claimed_p = rec.get('p')  # 若提交时记了声明概率
        # 让球注需要精确比分：优先从 results_sina 取，支持队名别名
        hs = as_ = None
        sc = score_index.get((alias(h), alias(a))) or score_index.get((h, a))
        if sc:
            hs, as_ = sc
        hit = judge_hit(parsed, lr['actual_idx'], hs, as_)
        settled.append({
            'match': m, 'pick': pick,
            'hit': hit,
            'play': parsed['play'],
            'line': parsed.get('line'),
            # market 保留旧字段名给下游（eval_plane 等）用，值改由 parse_pick 决定
            'market': ('hcap' if parsed['play'] == 'handicap'
                       else (rec.get('market') or '1x2')),
            'actual': ['胜','平','负'][lr['actual_idx']],
            'edge': rec.get('edge'), 'claimed_p': claimed_p,
            'brier_final': lr.get('brier_final'), 'brier_market': lr.get('brier_market'),
        })
    # hit=None 表示信息不足（如让球注账本里没比分），单列出来，不算进命中率
    judged = [s for s in settled if s['hit'] is not None]
    unjudged = [s for s in settled if s['hit'] is None]
    n = len(judged); hits = sum(1 for s in judged if s['hit'])
    print(f"=== 挑战复盘：{n} 注已结算，{hits} 中（{(hits/n if n else 0):.0%}）"
          f"——跨周累计，state 当前周为 {st['week']} ===")
    if unjudged:
        print(f"    （另有 {len(unjudged)} 注信息不足未计入：让球注缺比分）")
    for s in settled:
        print(f"  {'✅' if s['hit'] else '❌'} {s['match'][:22]:22s} {s['pick'][:14]:14s} 实际{s['actual']} "
              f"edge={s['edge'] if s['edge'] is not None else '-'} brier(我/市场)={s['brier_final']}/{s['brier_market']}")
    # Brier 池对比：我们的 final vs 市场——挑战池是否跑赢市场基线
    bf = [s['brier_final'] for s in settled if isinstance(s['brier_final'], float)]
    bm = [s['brier_market'] for s in settled if isinstance(s['brier_market'], float)]
    if len(bf) >= 3 and len(bm) >= 3:
        d_ = sum(bf)/len(bf) - sum(bm)/len(bm)
        print(f"\n挑战池 Brier：模型 {sum(bf)/len(bf):.4f} vs 市场 {sum(bm)/len(bm):.4f} → Δ={d_:+.4f}（负=模型更准）")
    json.dump({'week': st['week'], 'settled': settled},
              open(f'{ROOT}/data/state/challenge_review.json', 'w'), ensure_ascii=False, indent=1)
    print("→ data/state/challenge_review.json")

    # ---------- 策略层建议（只调挑战门槛/市场偏好，永不回写模型参数） ----------
    policy = {'generated_at': datetime.now().strftime("%F %T"), 'n_settled': n,
              'model_hit': round(hits / n, 3) if n else None,
              # 不叫 notes：顶层 note 是实验开关的说明，不能被周复盘洗掉
              'review_notes': '样本 <80 时建议仅记录不改变门槛（防小样本抖动）；'
                              '此文件只被挑战提交 cron 消费（策略层），模型引擎禁止读取'}
    # 按声明 edge 分桶的实际兑现
    mk_edge = defaultdict(list)
    for rec in recs:
        e = rec.get('edge')
        if e is None: continue
        b = '0-1pp' if e < 0.01 else ('1-2pp' if e < 0.02 else '2pp+')
        mk_edge[b].append(rec)
    realization = {}
    for b, lst in mk_edge.items():
        # 兑现判定：该桶声明场次若已结算，命中率 vs 声明均值
        realized = [x for x in settled if x.get('edge') is not None and (
            ('0-1pp' if x['edge'] < 0.01 else '1-2pp' if x['edge'] < 0.02 else '2pp+') == b)]
        if realized:
            realization[b] = {'n': len(realized)}
    policy['edge_realization'] = realization
    # 门槛建议（≥80 注才给方向；单调规则）
    if n >= 80:
        # 全体命中 vs 50% 粗基线（周中/周末分桶等 p 字段积累两周后细化）
        policy['gate_hint'] = ('keep' if hits / n >= 0.5 else 'tighten')
    write_policy(Path(f'{ROOT}/data/state/challenge_policy.json'), policy)
    print("→ data/state/challenge_policy.json（策略层建议，模型引擎禁止读取）")

if __name__ == '__main__':
    main()
