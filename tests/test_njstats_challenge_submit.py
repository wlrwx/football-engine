"""预测挑战提交器（scripts/njstats_challenge/submit.js）的回归测试。

为什么值得测：
这个脚本是 Mac Air 上**唯一无法上云**的任务（需浏览器登录态）。
它出问题不会让 CI 变红——只会让 Air 每天静默少提几场，
等周日看榜才发现。所以它的纯函数部分必须在主线 CI 里锁住。

测的是 Node 脚本里的纯函数，用 node 子进程跑真实文件（不是重写一份 Python 副本）：
  - weekKey()：周界必须按北京时间（见下方时区用例）
  - pickBest()/validateHandicapSign()：EV 择优 + 让球符号校验 + 幂等
  - protobuf 编解码：v3.1 起的协议层，wire format 细节最容易静默腐烂
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUBMIT_JS = ROOT / "scripts" / "njstats_challenge" / "submit.js"

pytestmark = pytest.mark.skipif(
    not SUBMIT_JS.exists(), reason="submit.js 不存在"
)


def _node(script: str, env: dict | None = None) -> str:
    """在 node 里执行一段脚本，把被测函数 load 进来。"""
    src = f"""
    const fs = require('fs');
    let src = fs.readFileSync({str(SUBMIT_JS)!r}, 'utf-8');
    // 2026-10-23：正则从 /^#!.*\\n/ 改为 /^#!.*\\r?\\n/。
    // JS 的 `.` 不匹配 \\r，Windows（core.autocrlf=true）检出成 CRLF 时
    // 旧正则剥不掉 shebang → new Function 直接 SyntaxError（26 例全挂）。
    src = src.replace(/^#!.*\\r?\\n/, '');        // 去掉 shebang，new Function 不认
    // 只取纯函数段（到 main() 之前），避开文件系统/网络副作用
    const cut = src.indexOf('async function main');
    const body = src.slice(0, cut);
    // ROOT 的基准：submit.js 优先用 process.argv[1]，被 new Function 载入时
    // argv[1] 是 node -e 的占位符，所以这里显式喂真实路径进去。
    process.argv[1] = {str(SUBMIT_JS)!r};
    const mod = new Function(body + '; return {{weekKey, pickBest, validateHandicapSign, handicapProb, loadCandidates, ROOT, STATE_DIR, encodeSubmitReq, encodeMatchListReq, encodeWeekQuotaReq, encodeEnvelope, decodeEnvelope, decodeName, readFields, firstField, allFields, num, str, normName, normalizeTeamNames, matchPool, fetchPool, fetchQuota, weekIdOf, isSubmitOk, isYouth, PLAY_TYPE, LIST_TYPE}};')();
    {script}
    """
    p = subprocess.run(["node", "-e", src], capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace", env=env)
    if p.returncode != 0:
        raise AssertionError(f"node 失败: {p.stderr}")
    return p.stdout.strip()


def test_state_dir_falls_back_to_os_homedir_without_HOME():
    """HOME 未定义时必须回退 os.homedir()，不能在模块加载时崩。

    2026-10-28：Windows 的计划任务 / PowerShell 不设 HOME（Git Bash 才设），
    原实现 path.join(process.env.HOME, '.football-engine') 直接 TypeError。
    """
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    out = _node("console.log(JSON.stringify({ dir: mod.STATE_DIR }));", env=env)
    d = json.loads(out)["dir"]
    assert Path(d).is_absolute(), d
    assert d.replace("\\", "/").endswith(".football-engine"), d


def test_weekkey_is_beijing_not_utc():
    """北京时间周一 07:00 必须算作本周一，不能因 UTC 还是周日而退回上周。

    这是实测会发生的边界：用 `new Date()` + getDay() 时，
    Beijing Mon 07:00 = UTC Sun 23:00 → getDay()==0 → 被当成上一个周一，
    周一早上的提交会记到上周配额里（周一部分被重复计算、一部分凭空消失）。
    """
    out = _node("""
    console.log(JSON.stringify({
      mon_0700: mod.weekKey(new Date('2026-10-12T07:00:00+08:00')),
      mon_0800: mod.weekKey(new Date('2026-10-12T08:00:00+08:00')),
      tue:      mod.weekKey(new Date('2026-10-13T10:00:00+08:00')),
      sun_late: mod.weekKey(new Date('2026-10-18T23:00:00+08:00')),
      next_mon: mod.weekKey(new Date('2026-10-19T00:30:00+08:00')),
    }));
    """)
    d = json.loads(out)
    assert d["mon_0700"] == "2026-10-12"
    assert d["mon_0800"] == "2026-10-12"
    assert d["tue"] == "2026-10-12"
    assert d["sun_late"] == "2026-10-12"
    assert d["next_mon"] == "2026-10-19"


def test_pickbest_one_market_per_match():
    """同一场只出一票（STATUS.md 实测规则：每场仅一市场，提过即消失）。"""
    out = _node("""
    const cands = [
      {match_id:'M1', market:'1x2', sel:'home', odds:2.0, model_prob:0.55, line:null},
      {match_id:'M1', market:'handicap', sel:'away', odds:1.9, model_prob:0.60, line:1.0},
      {match_id:'M2', market:'1x2', sel:'draw', odds:3.0, model_prob:0.40, line:null},
    ];
    const picks = mod.pickBest(cands, new Set());
    console.log(JSON.stringify(picks));
    """)
    picks = json.loads(out)
    m1 = [p for p in picks if p["match_id"] == "M1"]
    assert len(m1) == 1, f"同一场出了 {len(m1)} 票，违反「每场仅一市场」"
    assert m1[0]["model_prob"] == 0.60, "应留模型概率最高的那一票"


def test_pickbest_respects_odds_band():
    """赔率必须在 [1.60, 2.31] 内（EV 策略）。"""
    out = _node("""
    const cands = [
      {match_id:'LOW',  market:'1x2', sel:'home', odds:1.50, model_prob:0.70, line:null},
      {match_id:'HIGH', market:'1x2', sel:'home', odds:2.50, model_prob:0.70, line:null},
      {match_id:'OK',   market:'1x2', sel:'home', odds:2.00, model_prob:0.55, line:null},
    ];
    console.log(JSON.stringify(mod.pickBest(cands, new Set()).map(p=>p.match_id)));
    """)
    ids = json.loads(out)
    assert ids == ["OK"]


def test_pickbest_skips_already_submitted():
    """幂等：本周已提过的场次不能再提（提过即消失）。"""
    out = _node("""
    const cands = [
      {match_id:'M1', market:'1x2', sel:'home', odds:2.0, model_prob:0.55, line:null},
      {match_id:'M2', market:'1x2', sel:'away', odds:1.8, model_prob:0.50, line:null},
    ];
    console.log(JSON.stringify(mod.pickBest(cands, new Set(['M1'])).map(p=>p.match_id)));
    """)
    assert json.loads(out) == ["M2"]


def test_handicap_sign_guard_rejects_absurd_line():
    """让球线符号错位防护：|line|>5 必须拒绝（符号反了系统静默记成相反的一场）。"""
    with pytest.raises(AssertionError):
        _node("""
        mod.validateHandicapSign({market:'handicap', line: 8.0});
        console.log('should not reach');
        """)


def _latest_predictions_day() -> str:
    """最新一个**真的写着 predictions.json** 的日期目录。

    2026-11-09 00:30 实测：daily-forecast 先生成 `import_manifest.json`、后写
    `predictions.json`，所以 `sorted(...)[-1]` 在北京零点后的窗口里挑到的是个空目录
    → submit.js 抛「无预测文件」→ 主线 CI 每天凌晨必红两例（ubuntu+windows 各一次）。
    测试的前提是"仓库里有可解析的预测"，不是"恰好跑到今天这一步"，所以要跳过而不是失败。
    """
    daily = ROOT / "data" / "daily"
    days = [p.name for p in daily.iterdir()
            if p.is_dir() and (p / "predictions.json").exists()] if daily.exists() else []
    if not days:
        pytest.skip("无带 predictions.json 的 data/daily 目录（每日预测尚未落库）")
    return sorted(days)[-1]


def test_latest_predictions_day_skips_half_written_today(tmp_path, monkeypatch):
    """把 00:30 那个窗口做成用例：今天只有 import_manifest，昨天有预测。"""
    daily = tmp_path / "data" / "daily"
    (daily / "2026-11-08").mkdir(parents=True)
    (daily / "2026-11-08" / "predictions.json").write_text("[]", encoding="utf-8")
    (daily / "2026-11-09").mkdir()
    (daily / "2026-11-09" / "import_manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert _latest_predictions_day() == "2026-11-08"

    (daily / "2026-11-09" / "predictions.json").write_text("[]", encoding="utf-8")
    assert _latest_predictions_day() == "2026-11-09"

    for p in daily.iterdir():
        for f in p.glob("predictions.json"):
            f.unlink()
    with pytest.raises(pytest.skip.Exception):
        _latest_predictions_day()


def test_loadcandidates_on_real_data():
    """用仓库里真实的 predictions.json 跑一遍，确认字段路径没过时。"""
    day = _latest_predictions_day()
    out = _node(f"""
    const cands = mod.loadCandidates({day!r});
    console.log(JSON.stringify({{n: cands.length, day: {day!r},
      markets: [...new Set(cands.map(c=>c.market))]}}));
    """)
    d = json.loads(out)
    assert d["n"] > 0, f"{d['day']} 无候选——predictions.json 字段路径可能已变"
    assert set(d["markets"]) <= {"1x2", "handicap"}


def test_handicap_prob_uses_fused_not_raw():
    """让球候选必须用融合后概率，不能退回原始模型概率。

    2026-10-15 修复的直接回归：首批 5 注结算后复盘发现
    莱比锡红牛 vs 埃因霍温（让球 -1）的提交用的是
    `handicap_away_prob`（raw p=0.7951），而不是
    `handicap_ev.probs.away`（融合后 0.4062）——相差近一倍。

    raw 算出 EV=+0.6896 排到当日第一，比赛 1:0 走盘记 0 分。
    而 `handicap_ev.recommended` 对这场是 False（best_edge 0.0032），
    投注层根本没推荐它，是提交器自建口径把它排上去的。
    """
    out = _node("""
    const p = {
      match_id: 'M1', home_team: '莱比锡红牛', away_team: '埃因霍温',
      handicap: -1,
      handicap_home_odds: 2.03, handicap_away_odds: 2.03,
      handicap_home_prob: 0.0886, handicap_away_prob: 0.7951,
      handicap_ev: {probs: {home: 0.3683, draw: 0.2256, away: 0.4062},
                    best_edge: 0.0032, recommended: false},
    };
    const cands = mod.loadCandidates ? null : null;
    console.log(JSON.stringify({
      fused: mod.handicapProb(p, 'away'),
      raw_would_be: p.handicap_away_prob,
    }));
    """)
    d = json.loads(out)
    assert abs(d["fused"] - 0.4062) < 1e-9, (
        f"让球概率应取 handicap_ev.probs.away=0.4062，实得 {d['fused']}；"
        f"raw 值 0.7951 会把 EV 从 +0.075 抬到 +0.6896"
    )


def test_handicap_prob_falls_back_to_raw_when_no_ev():
    """老预测文件没有 handicap_ev 时必须退回 raw（不能让候选凭空消失）。

    否则历史 predictions.json 一跑 loadCandidates 就 0 候选，
    报错信息指向"无预测文件"，而真实原因是概率来源字段缺失。
    """
    out = _node("""
    const p = {match_id: 'M1', handicap: 1,
      handicap_home_odds: 1.9, handicap_away_odds: 1.9,
      handicap_home_prob: 0.55, handicap_away_prob: 0.60};
    console.log(JSON.stringify({
      home: mod.handicapProb(p, 'home'), away: mod.handicapProb(p, 'away'),
      ev_no_probs: mod.handicapProb({handicap_home_prob: 0.3,
          handicap_ev: {best_edge: 0.01}}, 'home'),
    }));
    """)
    d = json.loads(out)
    assert d["home"] == 0.55 and d["away"] == 0.60
    assert d["ev_no_probs"] == 0.3, "handicap_ev 存在但 probs 缺失时应回退"


def test_loadcandidates_handicap_uses_fused_prob_end_to_end():
    """端到端：真实 predictions.json 跑 loadCandidates，让球概率必须来自融合。

    莱比锡红牛 vs 埃因霍温（2026-10-13，让球 -1）是这次修复的实证样本：
    融合后 p(away)=0.4062，raw=0.7951。这个测试若挂，说明概率又退回
    原始模型值了——正是走盘丢分那一注的成因。
    """
    day = "2026-10-13"
    f = ROOT / "data" / "daily" / day / "predictions.json"
    if not f.exists():
        pytest.skip(f"无 {day}/predictions.json")
    preds = json.loads(f.read_text(encoding="utf-8"))
    hc = [p for p in preds
          if p.get("handicap") is not None
          and p.get("handicap_away_odds") and p.get("handicap_away_prob")]
    if not hc:
        pytest.skip(f"{day} 无让球候选")
    out = _node(f"""
    const cands = mod.loadCandidates({day!r});
    console.log(JSON.stringify({{
      handicap: cands.filter(c => c.market === 'handicap')
                    .map(c => ({{id: c.match_id, sel: c.sel, model_prob: c.model_prob}})),
    }}));
    """)
    d = json.loads(out)
    by_id = {p["match_id"]: p for p in hc}
    for c in d["handicap"]:
        p = by_id.get(c["id"])
        if p is None:
            continue
        ev = (p.get("handicap_ev") or {}).get("probs") or {}
        want = ev.get(c["sel"])
        if want is None:
            continue
        assert abs(c["model_prob"] - want) < 1e-9, (
            f"{c['id']} {c['sel']}：让球概率应取 handicap_ev.probs.{c['sel']}={want}，"
            f"实得 {c['model_prob']}（raw 会让 EV 虚高一倍）"
        )


def test_root_points_at_repo_root():
    """ROOT 必须解析到仓库根（含 data/daily），不能因 __dirname 失效而跑偏。

    实测踩过：原实现 `path.resolve(__dirname, '..', '..')`，在源码被
    `new Function` 载入的上下文里 __dirname 不存在，ROOT 静默变成
    CWD 的上两级 → loadCandidates 报「无预测文件」，而真实原因是
    路径基准丢了。这个错误信息毫无指向性，必须锁住。
    """
    out = _node("""
    console.log(JSON.stringify({
      root: mod.ROOT,
      has_daily: require('fs').existsSync(mod.ROOT + '/data/daily'),
    }));
    """)
    d = json.loads(out)
    assert d["has_daily"], f"ROOT={d['root']} 下没有 data/daily，路径基准错了"


# ---------------------------------------------------------------- protobuf 协议层（v3.1）

def test_protobuf_submit_req_wire_format():
    """ReqGuessingSubmit 的 wire format 必须与逆向出的 schema 逐字节一致。

    schema（从 NJStats 前端 bundle 的 protobuf-es 生成代码读出）：
      field 1: guessingId  string   (tag 0x0a)
      field 2: playType    enum     (tag 0x10)
      field 3: pick        int32    (tag 0x18)

    协议层坏了不会报错——服务端只会回一个业务错误码，
    而我们会以为是「模型选错了场次」。所以字节序必须锁死。
    """
    out = _node("""
    const b = mod.encodeSubmitReq({guessingId: 'abc', playType: 5, pick: 3});
    console.log(b.toString('hex'));
    """)
    # 0a 03 'abc' → 10 05 → 18 03
    assert out == "0a0361626310051803"


def test_protobuf_envelope_wraps_params():
    """外层信封 {params(1), page(2)}，params 是内层消息的 bytes。"""
    out = _node("""
    const inner = mod.encodeSubmitReq({guessingId: 'x', playType: 4, pick: 1});
    const env = mod.encodeEnvelope(inner);
    console.log(JSON.stringify({hex: env.toString('hex'), len: env.length}));
    """)
    d = json.loads(out)
    assert d["hex"].startswith("0a"), "外层第一个字段必须是 field1(params)"
    assert d["hex"].endswith("1200"), "外层第二个字段必须是空 page(2)"


def test_protobuf_decode_envelope_real_response():
    """用真实抓到的响应字节解码：08 ff..01 12 0c「请先登录」。

    这串字节是 2026-10-13 实测 curl 拿到的（合法 protobuf + 无 token）。
    code 的 varint 是 ff ff ff ff ff ff ff ff 01 = -1（有符号 64 位）。
    如果解码器把 code 解成无符号大数，调用方就永远判断不出未登录。
    """
    out = _node("""
    const hex = '08ffffffffffffffffff01120ce8afb7e58588e799bbe5bd95';
    const buf = Buffer.from(hex, 'hex');
    const r = mod.decodeEnvelope(buf);
    // out[1] 原始是 BigInt，JSON.stringify 不接受，先转字符串
    console.log(JSON.stringify({code: r.code, msg: r.msg, raw1: String(r[1])}));
    """)
    d = json.loads(out)
    assert d["code"] == -1, f"code 应为 -1（未登录），实得 {d['code']}"
    assert d["msg"] == "请先登录"


def test_play_type_enum_matches_frontend():
    """GuessingPlayType 枚举必须与前端一致（JC_SPF=4 / JC_RQ=5）。

    竞彩只有这两个口（前端 Yn() 只对 jingcaiSpf/jingcaiRq 生成提交），
    大小球 GPT_DX=3 存在但不用于竞彩——STATUS.md「大小球观察不提交」对上了。
    """
    out = _node("console.log(JSON.stringify(mod.PLAY_TYPE))")
    d = json.loads(out)
    assert d["JC_SPF"] == 4
    assert d["JC_RQ"] == 5
    assert d["DX"] == 3, "大小球枚举存在但不用（只观察不提交）"


def test_pick_encoding_is_index_plus_one():
    """pick 必须是 1-based（逆向自前端 Yn(): pick = index + 1）。

    写成 0-based 不会报错，只会静默提交到错误的方向。
    """
    day = _latest_predictions_day()
    out = _node(f"""
    const cands = mod.loadCandidates({day!r});
    const bad = cands.filter(c => c.pick < 1 || c.pick > 3);
    console.log(JSON.stringify({{n: cands.length, bad: bad.length}}));
    """)
    d = json.loads(out)
    assert d["n"] > 0
    assert d["bad"] == 0, f"{d['bad']} 个候选的 pick 越界（应为 1..3）"


# ---------------------------------------------------------------- 池匹配与盘口口径（v3.2）

def _pool_fixture():
    """构造一个含竞彩盘口和不含竞彩盘口的池子样例。"""
    return [
        {   # 带竞彩盘口（欧冠）
            'guessingId': 'p1AU', 'weekId': 202642, 'league': '欧冠',
            'home': '比利亚雷亚尔', 'away': '那不勒斯', 'kickoff': '',
            'odds': {'4': {'1': '2.04', '2': '3.40', '3': '2.90'},
                     '5': {'1': '4.05', '2': '3.95', '3': '1.59', '4': '-1'},
                     '1': {'1': '2.10', '2': '3.30', '3': '3.00'}},
        },
        {   # 只有欧赔口径
            'guessingId': 'Pb7j', 'weekId': 202642, 'league': '加拿职',
            'home': 'HFX流浪者', 'away': '骑兵', 'kickoff': '',
            'odds': {'1': {'1': '3.5', '2': '3.70', '3': '1.85'},
                     '2': {'1': '0.90', '2': '+0.5', '3': '0.90'},
                     '3': {'1': '0.95', '2': '3', '3': '0.85'}},
        },
    ]


def test_matchpool_prefers_jc_caliber():
    """有竞彩盘口的场次必须选竞彩口径（JC_SPF/JC_RQ），否则 EV 算错对象。

    竞彩口径的赔率才是竞彩实际开盘价；拿欧赔价算 EV 再按竞彩口径提交，
    等于用另一个市场的价格决定下注。
    """
    out = _node("""
    const pool = %s;
    const cands = [
      {match_id:'M1', market:'1x2', sel:'home', odds:2.04, model_prob:0.5,
       line:null, home:'比利亚雷亚尔', away:'那不勒斯', competition:'欧冠'},
      {match_id:'M2', market:'handicap', sel:'home', odds:1.92, model_prob:0.55,
       line:1.0, home:'HFX流浪者', away:'骑兵', competition:'加拿职'},
    ];
    const r = mod.matchPool(cands, pool);
    console.log(JSON.stringify(r.matched.map(m => ({
      mid: m.match_id, gid: m.guessingId, pt: m.play_type, jc: m.jc_available,
    }))));
    """ % json.dumps(_pool_fixture(), ensure_ascii=False))
    rows = json.loads(out)
    by_mid = {r['mid']: r for r in rows}
    assert by_mid['M1']['gid'] == 'p1AU'
    assert by_mid['M1']['pt'] == 4, "有 jcSpf 必须用 JC_SPF(4)"
    assert by_mid['M1']['jc'] is True
    assert by_mid['M2']['pt'] == 2, "无竞彩盘口必须退回欧赔 RQ(2)"
    assert by_mid['M2']['jc'] is False


def test_matchpool_reports_unmatched_explicitly():
    """未命中的场次必须显式报出来（联赛不重叠时不能静默）。"""
    out = _node("""
    const pool = %s;
    const cands = [
      {match_id:'X', market:'1x2', sel:'home', odds:2.0, model_prob:0.5,
       line:null, home:'马尔默', away:'卡尔马', competition:'瑞典超'},
    ];
    const r = mod.matchPool(cands, pool);
    console.log(JSON.stringify({m: r.matched.length, u: r.unmatched.length}));
    """ % json.dumps(_pool_fixture(), ensure_ascii=False))
    d = json.loads(out)
    assert d == {"m": 0, "u": 1}


def test_weekid_encoding_matches_real_value():
    """weekId 编码必须与实测一致：2026-10-13 → 202642（YYYY+ISO周号）。"""
    out = _node("""
    console.log(JSON.stringify({
      oct13: mod.weekIdOf(new Date('2026-10-13T10:00:00+08:00')),
      oct12: mod.weekIdOf(new Date('2026-10-12T10:00:00+08:00')),
      jan1:   mod.weekIdOf(new Date('2026-01-01T10:00:00+08:00')),
    }));
    """)
    d = json.loads(out)
    assert d["oct13"] == 202642
    assert d["oct12"] == 202642   # 同一周
    assert 202601 <= d["jan1"] <= 202653


def test_num_treats_missing_field_as_zero_not_nan():
    """protobuf 缺字段必须解成 0，不能是 NaN。

    实测踩过：usedCount 字段未返回时 Number(undefined)=NaN，
    配额显示成「已用 NaN/28」。缺字段的语义是 0。
    """
    out = _node("""
    console.log(JSON.stringify({
      undef: mod.num(undefined),
      null: mod.num(null),
      big: mod.num(123n),
      neg: mod.num(BigInt.asIntN(64, 18446744073709551615n)),
    }));
    """)
    d = json.loads(out)
    assert d["undef"] == 0
    assert d["null"] == 0
    assert d["big"] == 123
    assert d["neg"] == -1


def test_quota_response_decoded_with_real_bytes():
    """用实测的配额响应字节解码：maxCount=28, remainCount=28, usedCount 缺省=0。

    实测 hex: 08 92af0c 10 1c 20 1c
      field1 weekId=202642, field2 maxCount=28, field4 remainCount=28
      （field3 usedCount 未返回 → 必须解成 0，不是 NaN）
    """
    out = _node("""
    const buf = Buffer.from('0892af0c101c201c', 'hex');
    console.log(JSON.stringify({
      weekId: mod.num(mod.firstField(buf, 1)),
      maxCount: mod.num(mod.firstField(buf, 2)),
      usedCount: mod.num(mod.firstField(buf, 3)),
      remainCount: mod.num(mod.firstField(buf, 4)),
    }));
    """)
    d = json.loads(out)
    assert d == {"weekId": 202642, "maxCount": 28, "usedCount": 0, "remainCount": 28}


def test_pool_response_decodes_names_and_ids():
    """用实测的开放池响应解出 222 场 + 中文队名 + guessingId。

    这是整个链路的地基：guessingId 解错，提交就打到错误的场次
    （protobuf 里它只是个字符串，写错不报错）。
    """
    # 用本机缓存（没有就跳过；池子由 --pool 拉取）
    pool_cache = Path.home() / '.football-engine' / 'njstats_pool.json'
    if not pool_cache.exists():
        pytest.skip("无池缓存（先跑 --pool）")
    out = _node("""
    const rows = JSON.parse(fs.readFileSync(process.env.HOME + '/.football-engine/njstats_pool.json','utf-8')).rows;
    console.log(JSON.stringify({
      n: rows.length,
      withGid: rows.filter(r => r.guessingId && r.guessingId.length > 0).length,
      withCnName: rows.filter(r => r.home && r.away).length,
      jcRows: rows.filter(r => r.odds && (r.odds['4'] || r.odds['5'])).length,
    }));
    """)
    d = json.loads(out)
    assert d['n'] > 100, f"池子只有 {d['n']} 场，接口可能变了"
    assert d['withGid'] == d['n'], "有场次缺 guessingId"
    assert d['withCnName'] == d['n'], "有场次缺中文队名（解码层级错了）"
    assert d['jcRows'] > 0, "没有一场带竞彩盘口——口径择优逻辑失去意义"


# ---------------------------------------------------------------- 队名别名匹配（v3.3）

def test_abbreviation_variants_match_via_alias():
    """竞彩简称必须能匹配到池子的全称（2026-10-13 实测的 6 个失败案例）。

    这是"有注提不了"的直接原因：手写简化归一化只认字面相等，
    8 场欧冠里 6 场匹配失败。改用 engine/team_aliases.py 后 5/5 命中。
    """
    out = _node("""
    const pool = [
      {guessingId:'p1AU', league:'欧冠', home:'比利亚雷亚尔', away:'那不勒斯', odds:{}},
      {guessingId:'XkAU', league:'欧冠', home:'莱比锡红牛', away:'埃因霍温', odds:{}},
      {guessingId:'6xAU', league:'欧冠', home:'加拉塔萨雷', away:'巴塞罗那', odds:{}},
      {guessingId:'OxAU', league:'欧冠', home:'马德里竞技', away:'曼彻斯特联', odds:{}},
      {guessingId:'uHAU', league:'欧冠', home:'朗斯', away:'葡萄牙体育', odds:{}},
    ];
    const cands = [
      {match_id:'A', market:'1x2', sel:'home', odds:2.04, model_prob:0.5, line:null,
       home:'比利亚雷', away:'那不勒斯', competition:'欧冠'},
      {match_id:'B', market:'1x2', sel:'home', odds:1.60, model_prob:0.5, line:null,
       home:'莱红牛', away:'埃因霍温', competition:'欧冠'},
      {match_id:'C', market:'1x2', sel:'home', odds:2.10, model_prob:0.5, line:null,
       home:'加拉塔萨', away:'巴萨', competition:'欧冠'},
      {match_id:'D', market:'1x2', sel:'home', odds:1.78, model_prob:0.5, line:null,
       home:'马竞', away:'曼联', competition:'欧冠'},
      {match_id:'E', market:'1x2', sel:'home', odds:2.27, model_prob:0.5, line:null,
       home:'朗斯', away:'里斯本', competition:'欧冠'},
    ];
    const r = mod.matchPool(cands, pool);
    console.log(JSON.stringify({
      hit: r.matched.length, miss: r.unmatched.length,
      gids: r.matched.map(m => m.guessingId).sort(),
    }));
    """)
    d = json.loads(out)
    assert d["hit"] == 5, f"只命中 {d['hit']}/5——译名变体没匹配上"
    assert d["miss"] == 0
    assert d["gids"] == ["6xAU", "OxAU", "XkAU", "p1AU", "uHAU"]


def test_alias_does_not_merge_different_teams():
    """别名不能过度合并——不同队必须匹配不上。

    项目文档明确列了危险对（维京 vs 维京古、哥德堡 vs 哥德堡盖斯…），
    合并了会把注提到错误的场次，而且 protobuf 不会报错。
    """
    out = _node("""
    const pool = [
      {guessingId:'V1', league:'欧冠', home:'维京古', away:'某队', odds:{}},
    ];
    const cands = [
      {match_id:'X', market:'1x2', sel:'home', odds:2.0, model_prob:0.5, line:null,
       home:'维京', away:'某队', competition:'欧冠'},
    ];
    const r = mod.matchPool(cands, pool);
    console.log(JSON.stringify({hit: r.matched.length}));
    """)
    assert json.loads(out)["hit"] == 0, "维京与维京古被错误合并"


def test_real_today_matches_pool():
    """用真实数据端到端验证：今日欧冠必须全部命中池子。

    这是价值兑付的验收线——从"0 命中"到"有注可提"。
    数据缺失时跳过（CI 上可能没有当日 predictions）。
    """
    pool_cache = Path.home() / ".football-engine" / "njstats_pool.json"
    if not pool_cache.exists():
        pytest.skip("无池缓存（先跑 --pool）")
    day = _latest_predictions_day()

    out = _node(f"""
    const pool = JSON.parse(require('fs').readFileSync(process.env.HOME + '/.football-engine/njstats_pool.json','utf-8')).rows;
    const cands = mod.loadCandidates({day!r}).filter(c => c.market === '1x2');
    const r = mod.matchPool(cands, pool);
    const byVia = {{}};
    for (const m of r.matched) byVia[m.matched_via] = (byVia[m.matched_via] || 0) + 1;
    console.log(JSON.stringify({{cands: cands.length, hit: r.matched.length, via: byVia}}));
    """)
    d = json.loads(out)
    if d["cands"] == 0:
        pytest.skip("当日无 1X2 候选")
    # 至少要有命中（具体数量随当天赛程变）
    assert d["hit"] > 0, f"{d['cands']} 个候选全部未命中池子——别名表可能退化"


# ---------------------------------------------------------------- 提交成功判定（v3.4）

def test_success_is_msg_suc_not_code_zero():
    """成功判定必须是 msg='suc'，不是 code===0。

    【真实踩坑，2026-10-13】GMI_SUBMIT **成功时不返回 code 字段**。
    实测 5 注全部真实提交成功（服务端配额 usedCount 0→5），
    但脚本写 `code === 0` 就报「❌ HTTP 200 code=undefined suc」，
    把成功当失败。

    危险在于方向反了：成功记成失败 → 状态文件不更新 →
    下一轮重复提交同一场（每场仅一市场，重复必被拒）。
    """
    out = _node("""
    console.log(JSON.stringify({
      realSuccess: mod.isSubmitOk({status:200, code:undefined, msg:'suc'}),
      realSuccessCap: mod.isSubmitOk({status:200, code:undefined, msg:'SUC'}),
      notLoggedIn: mod.isSubmitOk({status:200, code:-1, msg:'请先登录'}),
      invalidMatch: mod.isSubmitOk({status:200, code:-1, msg:'比赛信息无效'}),
      httpError: mod.isSubmitOk({status:500, code:undefined, msg:'suc'}),
      codeZero: mod.isSubmitOk({status:200, code:0, msg:''}),
    }));
    """)
    d = json.loads(out)
    assert d["realSuccess"] is True, "msg=suc 必须判成功（这是实测的真实形态）"
    assert d["realSuccessCap"] is True, "msg 大小写不敏感"
    assert d["notLoggedIn"] is False, "「请先登录」必须判失败"
    assert d["invalidMatch"] is False, "「比赛信息无效」必须判失败"
    assert d["httpError"] is False, "HTTP 非 200 必须判失败"
    assert d["codeZero"] is True, "兼容 code=0 的旧形态"


def test_failure_reports_reason():
    """失败时必须能拿到原因（便于日志定位是 token 过期还是场次无效）。"""
    out = _node("""
    const r = {status:200, code:-1, msg:'请先登录'};
    console.log(JSON.stringify({ok: mod.isSubmitOk(r), msg: r.msg}));
    """)
    d = json.loads(out)
    assert d["ok"] is False
    assert "登录" in d["msg"]


# ---------------------------------------------------------------- 青年队误配防护（2026-10-14）

def test_youth_squad_names_are_detected():
    """U19/U21/Under-19 必须判青年队；普通队名不能误判。

    2026-10-14 实测背景：开放池里欧冠比赛同时存在**成年队与 U19 两个版本**
    （U19 版通常早 7 小时开赛）。我们只预测成年队，若匹配到 U19 的
    guessingId，提交照样成功（protobuf 不校验），注就静默提到错误的场次。
    """
    out = _node("""
    console.log(JSON.stringify({
      u19:      mod.isYouth('马德里竞技U19'),
      u21:      mod.isYouth('托特纳姆热刺U21'),
      under19: mod.isYouth('Racing Club Lens Under 19'),
      under21: mod.isYouth('AS Roma Under 21'),
      hyphen:   mod.isYouth('拜仁慕尼黑U-19'),
      senior1:  mod.isYouth('马德里竞技'),
      senior2:  mod.isYouth('曼彻斯特联'),
      senior3:  mod.isYouth('纽卡斯尔喷气机'),
      senior4:  mod.isYouth('维京'),
      empty:    mod.isYouth(''),
      loneU:    mod.isYouth('U'),
    }));
    """)
    d = json.loads(out)
    for key in ("u19", "u21", "under19", "under21", "hyphen"):
        assert d[key] is True, f"{key} 必须判为青年队"
    for key in ("senior1", "senior2", "senior3", "senior4", "empty", "loneU"):
        assert d[key] is False, f"{key} 不能误判为青年队"


def test_matchpool_never_matches_youth_squad():
    """候选不得匹配到池内青年队的 guessingId（哪怕队名完全相同）。

    2026-10-14 实测：池内欧冠同时有成年队与 U19 两版（U19 早 7 小时开赛）。
    三级索引当前都区分得了（normalize/loose/normName 都不删 "U19" 后缀），
    所以这是**防御性**用例——锁的是"万一哪天 loose 规则把后缀删了"或
    "池子改名成等价写法"时不会静默提错场次。真实数据里 U19 与成年队
    的名字永远不同，本条测的是最坏情况（候选名 = 池内青年队名）。
    """
    out = _node("""
    const pool = [
      {guessingId:'youth-1', weekId:1, league:'欧青联', home:'阿森纳U19', away:'里尔U19', kickoff:'2026-10-13T20:00:00Z', odds:{'1':{'3':'2.10'}}},
    ];
    const cands = [{match_id:'y', home:'阿森纳U19', away:'里尔U19', market:'1x2', model_prob:0.4, odds:1.8}];
    const r = mod.matchPool(cands, pool);
    console.log(JSON.stringify({matched: r.matched.length, unmatched: r.unmatched}));
    """)
    d = json.loads(out)
    assert d["matched"] == 0, "青年队候选不得匹配成功"
    assert len(d["unmatched"]) == 1
    assert d["unmatched"][0]["reason"] == "pool_match_is_youth_squad"


def test_matchpool_prefers_senior_over_youth_when_both_present():
    """成年队与青年队同时在池内时，必须命中成年队那条。"""
    out = _node("""
    const pool = [
      {guessingId:'youth-1', weekId:1, league:'欧青联', home:'阿森纳U19', away:'里尔U19', kickoff:'2026-10-13T20:00:00Z', odds:{'1':{'3':'2.10'}}},
      {guessingId:'senior-1', weekId:1, league:'欧冠', home:'阿森纳', away:'里尔', kickoff:'2026-10-13T19:00:00Z', odds:{'1':{'3':'1.78'}}},
    ];
    const cands = [{match_id:'2026-10-13_周二006', home:'阿森纳', away:'里尔', market:'1x2', model_prob:0.4, odds:1.8}];
    const r = mod.matchPool(cands, pool);
    console.log(JSON.stringify({n: r.matched.length, gid: r.matched[0] && r.matched[0].guessingId, league: r.matched[0] && r.matched[0].pool_league}));
    """)
    d = json.loads(out)
    assert d["n"] == 1, "只应命中一场"
    assert d["gid"] == "senior-1", "必须命中成年队，不能是青年队"
    assert d["league"] == "欧冠"
