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
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUBMIT_JS = ROOT / "scripts" / "njstats_challenge" / "submit.js"

pytestmark = pytest.mark.skipif(
    not SUBMIT_JS.exists(), reason="submit.js 不存在"
)


def _node(script: str) -> str:
    """在 node 里执行一段脚本，把被测函数 load 进来。"""
    src = f"""
    const fs = require('fs');
    let src = fs.readFileSync({str(SUBMIT_JS)!r}, 'utf-8');
    src = src.replace(/^#!.*\\n/, '');            // 去掉 shebang，new Function 不认
    // 只取纯函数段（到 main() 之前），避开文件系统/网络副作用
    const cut = src.indexOf('async function main');
    const body = src.slice(0, cut);
    // ROOT 的基准：submit.js 优先用 process.argv[1]，被 new Function 载入时
    // argv[1] 是 node -e 的占位符，所以这里显式喂真实路径进去。
    process.argv[1] = {str(SUBMIT_JS)!r};
    const mod = new Function(body + '; return {{weekKey, pickBest, validateHandicapSign, loadCandidates, ROOT, encodeSubmitReq, encodeEnvelope, decodeEnvelope, PLAY_TYPE}};')();
    {script}
    """
    p = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"node 失败: {p.stderr}")
    return p.stdout.strip()


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


def test_loadcandidates_on_real_data():
    """用仓库里真实的 predictions.json 跑一遍，确认字段路径没过时。"""
    daily = ROOT / "data" / "daily"
    if not daily.exists():
        pytest.skip("无 data/daily")
    day = sorted(p.name for p in daily.iterdir() if p.is_dir())[-1]
    out = _node(f"""
    const cands = mod.loadCandidates({day!r});
    console.log(JSON.stringify({{n: cands.length, day: {day!r},
      markets: [...new Set(cands.map(c=>c.market))]}}));
    """)
    d = json.loads(out)
    assert d["n"] > 0, f"{d['day']} 无候选——predictions.json 字段路径可能已变"
    assert set(d["markets"]) <= {"1x2", "handicap"}


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
    daily = ROOT / "data" / "daily"
    if not daily.exists():
        pytest.skip("无 data/daily")
    day = sorted(p.name for p in daily.iterdir() if p.is_dir())[-1]
    out = _node(f"""
    const cands = mod.loadCandidates({day!r});
    const bad = cands.filter(c => c.pick < 1 || c.pick > 3);
    console.log(JSON.stringify({{n: cands.length, bad: bad.length}}));
    """)
    d = json.loads(out)
    assert d["n"] > 0
    assert d["bad"] == 0, f"{d['bad']} 个候选的 pick 越界（应为 1..3）"
