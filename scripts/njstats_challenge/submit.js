#!/usr/bin/env node
/**
 * NJStats 预测挑战提交器（v3.2，2026-10-13 协议 + guessingId 映射实证版）
 *
 * ⚠️ 本文件是 **Mac Air 上 `_vault/njstats_challenge_submit.js` 的仓库重建版**。
 * 原文件从未入仓库（`_vault/` 在 .gitignore），只存在于 Air 本地磁盘。
 * Air 重装后按本文件 + README.md 恢复，**不要凭记忆重写**。
 *
 * ## 版本史（协议是实证出来的，不是猜的）
 * - v3   ：端点/协议全是推断（POST /api/challenge/predict + JSON）→ **作废**
 * - v3.1 ：逆向前端 bundle 挖出真实协议，curl 实测「请先登录」证明链路对
 * - v3.2 ：用真实 apikey 打通 GMI_MATCH_LIST，解出 222 场的 guessingId 映射；
 *          同时发现两个决定性事实（见下）
 *
 * ## v3.2 发现的两个决定性事实（2026-10-13 实测）
 *
 * 1. **apikey 能读 /njserver/openApi/，但不能提交。**
 *    `?apikey=` 数据 API 的 key 直接当 `apikey` 头发给 openApi 也能过鉴权
 *    （GMI_MATCH_LIST / GMI_WEEK_PREDICT_QUOTA 都返回 suc），
 *    但 GMI_SUBMIT 永远回「请先登录」——带不带 apikey 都一样。
 *    → **提交必须浏览器登录态，这一点没变。**
 *
 * 2. **我们侧与 NJStats 开放池的联赛当前不重叠（但池内有竞彩盘口）。**
 *    实测 listType=OPEN 返回 222 场，联赛分布：欧青联28/欧冠18/欧协联18/
 *    欧联18/亚冠二级16/美职联15/英冠12/亚冠10/土甲10/德丙10/日联杯8/挪甲8…
 *    我们用 2026-10-12 的 predictions.json（10 场：意甲/西甲/巴甲/英超/瑞典超/
 *    葡超/法乙/亚冠精英）匹配，**命中 0 场**——那些联赛池子里一场都没有。
 *    但池子里**有 8 场欧冠带真实竞彩盘口**（jcSpf/jcRq，jcRq 还带让球线
 *    -1/-2/+2/+3），例如「比利亚雷亚尔 vs 那不勒斯」jcSpf 主胜 2.04
 *    就落在我们的 [1.60,2.31] 带内。
 *    → 不是没注可提，是**我们没预测欧冠**。等扩展预测范围，
 *       或等池子开放主流联赛，才有实际意义。
 *
 * ## 提交规则（2026-10-12 实测，来自 STATUS.md）
 * 1. **每场仅一市场**：同一场提过即消失
 * 2. **EV 策略**：只提赔率 ∈ [1.60, 2.31] 且模型概率最高的场次
 * 3. **每日 ≤5 场，周配额 28**（GMI_WEEK_PREDICT_QUOTA 实测 maxCount=28）
 * 4. **盘口口径择优**：池子给了 jc 盘口就用竞彩口径（JC_SPF/JC_RQ），
 *    否则退回欧赔口径（SPF/RQ）——竞彩口径的赔率才是我们要投的注的实价
 *
 * ## 用法
 *   node scripts/njstats_challenge/submit.js --probe          # 探活（不需 key）
 *   node scripts/njstats_challenge/submit.js --pool           # 拉开放池+映射（只需 apikey）
 *   node scripts/njstats_challenge/submit.js --quota          # 看本周配额（只需 apikey）
 *   node scripts/njstats_challenge/submit.js --date 2026-10-13          # dry-run
 *   node scripts/njstats_challenge/submit.js --date 2026-10-13 --submit # 真提交（需 token）
 *   node scripts/njstats_challenge/submit.js --status                   # 本周已提
 *
 * 凭证（都在 ~/.football-engine/，不随 git 走）：
 *   njstats_apikey.txt  数据 API key（拉池子/看配额用）
 *   njstats_token.txt   Bearer token（提交用，必须浏览器登录态）
 */

'use strict';

const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------- 配置

// ROOT 不能用 path.resolve(__dirname, '..', '..')：那样在「源码被 load 进别的
// 上下文」时（测试用 new Function 载入纯函数段、或别人 require 本文件）会失效，
// __dirname 不存在 → 静默解析到 CWD 附近 → 找不到 predictions.json 报
// 「无预测文件」，而真实原因是路径基准丢了。改成优先用 process.argv[1] 的目录。
const SELF_DIR = (() => {
  try {
    if (process.argv[1] && fs.existsSync(process.argv[1])) {
      return path.dirname(path.resolve(process.argv[1]));
    }
  } catch { /* fallthrough */ }
  return __dirname;
})();
const ROOT = path.resolve(SELF_DIR, '..', '..');

const STATE_DIR = path.join(process.env.HOME, '.football-engine');
const STATE_FILE = path.join(STATE_DIR, 'challenge_state.json');
const APIKEY_FILE = path.join(STATE_DIR, 'njstats_apikey.txt');
const TOKEN_FILE = path.join(STATE_DIR, 'njstats_token.txt');
const VALUE_MODEL_FILE = path.join(STATE_DIR, 'njstats_value_model.json');
const POOL_CACHE = path.join(STATE_DIR, 'njstats_pool.json');

// 2026-10-13 从前端 bundle 逆向并实测确认的协议常量
const PROTO_URL = 'https://www.njstats.cn/njserver/openApi/';
const MODULE_ID = '1012';   // MI_GUESSING
const MSG_MATCH_LIST = '1';     // GMI_MATCH_LIST
const MSG_SUBMIT = '2';         // GMI_SUBMIT
const MSG_WEEK_QUOTA = '15';    // GMI_WEEK_PREDICT_QUOTA

// GuessingMatchListType
const LIST_TYPE = { OPEN: 0, GUESSING: 1, FINISHED: 2 };

// GuessingPlayType 枚举（逆向自前端 bundle）
const PLAY_TYPE = {
  NONE: 0, SPF: 1, RQ: 2, DX: 3,
  JC_SPF: 4,   // 竞彩胜平负（开放池中未出现）
  JC_RQ: 5,    // 竞彩让球（开放池中未出现）
};

// EV 策略参数（STATUS.md 记录值）
const ODDS_MIN = 1.60;
const ODDS_MAX = 2.31;
const MAX_PER_DAY = 5;
const MAX_PER_WEEK = 28;

// ---------------------------------------------------------------- 工具

function log(msg) { console.log(`[${new Date().toISOString()}] ${msg}`); }
function readJson(p, fb) { try { return JSON.parse(fs.readFileSync(p, 'utf-8')); } catch { return fb; } }
function writeJson(p, o) { fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, JSON.stringify(o, null, 2)); }
function readKey(p) { try { return fs.readFileSync(p, 'utf-8').trim(); } catch { return ''; } }

/** 本周一（Asia/Shanghai），周配额 key。必须按北京时间算周界。 */
function weekKey(d = new Date()) {
  const cn = new Date(d.getTime() + 8 * 3600 * 1000);
  const day = cn.getUTCDay() === 0 ? 7 : cn.getUTCDay();
  cn.setUTCDate(cn.getUTCDate() - (day - 1));
  return cn.toISOString().slice(0, 10);
}

/** weekId 编码：YYYY + ISO 周号（实测 2026-10-13 → 202642） */
function weekIdOf(d = new Date()) {
  const cn = new Date(d.getTime() + 8 * 3600 * 1000);
  const day = cn.getUTCDay();
  const thursday = new Date(cn.getTime() + (4 - (day || 7)) * 86400000);
  const year = thursday.getUTCFullYear();
  const jan1 = Date.UTC(year, 0, 1);
  const week = Math.ceil(((thursday.getTime() - jan1) / 86400000 + 1) / 7);
  return year * 100 + week;
}

// ---------------------------------------------------------------- protobuf 手工编解码

function varint(n) {
  const out = []; let v = BigInt(n);
  while (true) {
    const b = Number(v & 0x7fn); v >>= 7n;
    if (v) out.push(b | 0x80); else { out.push(b); break; }
  }
  return Buffer.from(out);
}
function fBytes(no, buf) { return Buffer.concat([Buffer.from([(no << 3) | 2]), varint(buf.length), buf]); }
function fVarint(no, v) { return Buffer.concat([Buffer.from([(no << 3) | 0]), varint(v)]); }

function encodeSubmitReq({ guessingId, playType, pick }) {
  return Buffer.concat([
    fBytes(1, Buffer.from(String(guessingId), 'utf-8')),
    fVarint(2, playType), fVarint(3, pick),
  ]);
}
function encodeMatchListReq({ listType, leagueIds = [] }) {
  const parts = [fVarint(1, listType)];
  for (const id of leagueIds) parts.push(fBytes(2, Buffer.from(String(id), 'utf-8')));
  return Buffer.concat(parts);
}
function encodeWeekQuotaReq(weekId) { return fVarint(1, weekId); }
function encodeEnvelope(params) { return Buffer.concat([fBytes(1, params), fBytes(2, Buffer.alloc(0))]); }

/** 通用 protobuf 读：返回 [{no, wireType, value}] */
function readFields(buf) {
  let i = 0; const out = [];
  while (i < buf.length) {
    const tag = buf[i]; i += 1;
    const no = tag >> 3, wt = tag & 7;
    if (wt === 0) {
      let v = 0n, sh = 0n;
      while (true) { const x = buf[i]; i += 1; v |= BigInt(x & 0x7f) << sh; sh += 7n; if (!(x & 0x80)) break; }
      out.push({ no, wt, value: v });
    } else if (wt === 2) {
      let ln = 0, sh = 0;
      while (true) { const x = buf[i]; i += 1; ln |= (x & 0x7f) << sh; sh += 7; if (!(x & 0x80)) break; }
      out.push({ no, wt, value: buf.slice(i, i + ln) }); i += ln;
    } else if (wt === 5) { out.push({ no, wt, value: null }); i += 4; }
    else if (wt === 1) { out.push({ no, wt, value: null }); i += 8; }
    else break;
  }
  return out;
}
function firstField(buf, no) { const f = readFields(buf).find(x => x.no === no); return f ? f.value : undefined; }
function allFields(buf, no) { return readFields(buf).filter(x => x.no === no).map(x => x.value); }
function num(v) {
  // protobuf 缺字段时 value 是 undefined → Number(undefined)=NaN，
  // 会把「未返回」显示成 NaN（实测 usedCount 就这样）。
  // 语义上缺字段应为 0，不是 NaN。
  if (v === undefined || v === null) return 0;
  return typeof v === 'bigint' ? Number(BigInt.asIntN(64, v)) : Number(v);
}
function str(v) { return Buffer.isBuffer(v) ? v.toString('utf-8') : ''; }

/** 外层信封解码：{code, msg, data} */
function decodeEnvelope(buf) {
  const out = {};
  for (const f of readFields(buf)) {
    if (f.no === 1) out.code = num(f.value);
    else if (f.no === 2) out.msg = str(f.value);
    else if (f.no === 5) out.data = f.value;
  }
  return out;
}

/** {id(1), nameEn(2), nameCn(3)} → 中文名 */
function decodeName(buf) { return str(firstField(buf, 3)); }

// ---------------------------------------------------------------- RPC

async function rpc({ msgId, params, apikey, token }) {
  const headers = {
    'Content-Type': 'application/x-protobuf',
    'modId': MODULE_ID,
    'msgId': String(msgId),
  };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  else if (apikey) headers['apikey'] = apikey;
  const res = await fetch(PROTO_URL, { method: 'POST', headers, body: encodeEnvelope(params) });
  const buf = Buffer.from(await res.arrayBuffer());
  return { status: res.status, ...decodeEnvelope(buf) };
}

/**
 * 拉开放池并解出 guessingId ↔ 比赛 的映射。
 * 返回 [{guessingId, weekId, league, home, away, kickoff, odds:{spf,rq,dx}}]
 */
async function fetchPool(apikey) {
  const r = await rpc({
    msgId: MSG_MATCH_LIST, apikey,
    params: encodeMatchListReq({ listType: LIST_TYPE.OPEN }),
  });
  if (r.status !== 200) throw new Error(`HTTP ${r.status} ${r.msg}`);
  if (!r.data) return [];
  const items = allFields(r.data, 1);
  const rows = [];
  for (const it of items) {
    const gid = str(firstField(it, 7));
    const week = num(firstField(it, 8));
    const match = firstField(it, 1);
    const odds = firstField(it, 2);
    const ts = firstField(match, 6);
    const decodeOdds = (b) => {
      if (!b) return null;
      const g = {};
      for (const f of readFields(b)) {
        if (f.wt === 2) {
          const parts = {};
          for (const x of readFields(f.value)) {
            parts[x.no] = x.wt === 2 ? str(x.value) : num(x.value);
          }
          g[f.no] = parts;
        }
      }
      return g;
    };
    rows.push({
      guessingId: gid, weekId: week,
      league: decodeName(firstField(match, 2)),
      home: decodeName(firstField(match, 3)),
      away: decodeName(firstField(match, 4)),
      kickoff: ts ? new Date(num(ts) * 1000).toISOString() : '',
      odds: decodeOdds(odds),
    });
  }
  return rows;
}

/** 本周配额（apikey 可读）：{weekId, maxCount, usedCount, remainCount} */
async function fetchQuota(apikey, weekId) {
  const r = await rpc({ msgId: MSG_WEEK_QUOTA, apikey, params: encodeWeekQuotaReq(weekId) });
  if (!r.data) throw new Error(`配额查询失败: ${r.msg}`);
  return {
    weekId: num(firstField(r.data, 1)),
    maxCount: num(firstField(r.data, 2)),
    usedCount: num(firstField(r.data, 3)),
    remainCount: num(firstField(r.data, 4)),
  };
}

// ---------------------------------------------------------------- 读取预测

/**
 * 从 data/daily/<date>/predictions.json 提取可提交候选（1X2 + 让球）。
 *
 * ⚠️ 本函数只产**我们的**候选；与 NJStats 池的匹配在 matchPool() 里做。
 */
function loadCandidates(dateStr) {
  const f = path.join(ROOT, 'data', 'daily', dateStr, 'predictions.json');
  if (!fs.existsSync(f)) throw new Error(`无预测文件: ${f}（先跑 daily-forecast）`);
  const preds = readJson(f, []);
  const out = [];
  for (const p of preds) {
    if (p.odds_synthetic) continue;
    for (const [sel, odds, prob] of [
      ['home', p.home_odds, p.home_win_prob],
      ['draw', p.draw_odds, p.draw_prob],
      ['away', p.away_odds, p.away_win_prob],
    ]) {
      if (odds && prob) out.push({
        match_id: p.match_id, market: '1x2', sel,
        odds: Number(odds), model_prob: Number(prob), line: null,
        home: p.home_team, away: p.away_team, competition: p.competition,
      });
    }
    if (p.handicap != null && p.handicap_home_odds && p.handicap_away_odds) {
      if (p.handicap_home_prob) out.push({
        match_id: p.match_id, market: 'handicap', sel: 'home',
        odds: Number(p.handicap_home_odds), model_prob: Number(p.handicap_home_prob),
        line: Number(p.handicap), home: p.home_team, away: p.away_team, competition: p.competition,
      });
      if (p.handicap_away_prob) out.push({
        match_id: p.match_id, market: 'handicap', sel: 'away',
        odds: Number(p.handicap_away_odds), model_prob: Number(p.handicap_away_prob),
        line: Number(p.handicap), home: p.home_team, away: p.away_team, competition: p.competition,
      });
    }
  }
  return out;
}

// ---------------------------------------------------------------- 择优

/** 让球线符号校验（防符号错位）—— v2 提交器专门加的，必须保留。 */
function validateHandicapSign(cand) {
  if (cand.market !== 'handicap') return true;
  if (!Number.isFinite(cand.line)) {
    throw new Error(`${cand.match_id}: 让球线缺失或非数字（${cand.line}），拒绝提交`);
  }
  if (Math.abs(cand.line) > 5) {
    throw new Error(`${cand.match_id}: 让球线 ${cand.line} 超出合理范围 ±5，疑似符号错位`);
  }
  return true;
}

/** 队名归一化（中文/英文名混用时的匹配键） */
function normName(s) { return String(s || '').toLowerCase().replace(/[^0-9a-z\u4e00-\u9fff]/g, ''); }

/**
 * 把我们的候选匹配到 NJStats 开放池。
 * 返回 {matched: [...], unmatched: [...]}
 *
 * 匹配键 = (主队中文名, 客队中文名) 归一化后精确相等。
 *
 * **盘口口径择优**（2026-10-13 实测后加）：池子里有些场次带竞彩盘口
 * （jcSpf/jcRq，实测 222 场里有 8 场欧冠有），有些只有欧赔口径（spf/rq/dx）。
 * 提交时必须按**该场实际有的口径**选 playType：
 *   - 有 jcSpf → 1X2 用 GPT_JC_SPF(4)
 *   - 有 jcRq → 让球用 GPT_JC_RQ(5)
 *   - 都没有 → 退回欧赔 GPT_SPF(1)/GPT_RQ(2)
 * 竞彩口径的赔率才是竞彩实际开盘价，用它算 EV 才对得上我们要投的注。
 */
function matchPool(cands, pool) {
  const idx = new Map();
  for (const p of pool) idx.set(`${normName(p.home)}|${normName(p.away)}`, p);
  const matched = [], unmatched = [];
  for (const c of cands) {
    const hit = idx.get(`${normName(c.home)}|${normName(c.away)}`);
    if (!hit) { unmatched.push(c); continue; }
    const odds = hit.odds || {};
    const hasJcSpf = !!odds['4'], hasJcRq = !!odds['5'];
    let playType;
    if (c.market === '1x2') playType = hasJcSpf ? PLAY_TYPE.JC_SPF : PLAY_TYPE.SPF;
    else playType = hasJcRq ? PLAY_TYPE.JC_RQ : PLAY_TYPE.RQ;
    matched.push({
      ...c,
      guessingId: hit.guessingId,
      pool_league: hit.league,
      play_type: playType,
      pool_odds: c.market === '1x2' ? (odds[hasJcSpf ? '4' : '1']) : (odds[hasJcRq ? '5' : '2']),
      jc_available: hasJcSpf || hasJcRq,
    });
  }
  return { matched, unmatched };
}

/** EV 择优：赔率带 + 每场一票 + EV 降序 + 每日上限 */
function pickBest(cands, alreadySubmitted) {
  const byMatch = new Map();
  for (const c of cands) {
    if (c.odds < ODDS_MIN || c.odds > ODDS_MAX) continue;
    if (alreadySubmitted.has(c.match_id)) continue;
    validateHandicapSign(c);
    const cur = byMatch.get(c.match_id);
    if (!cur || c.model_prob > cur.model_prob) byMatch.set(c.match_id, c);
  }
  return [...byMatch.values()]
    .map(c => ({ ...c, ev: c.model_prob * c.odds - 1 }))
    .sort((a, b) => b.ev - a.ev)
    .slice(0, MAX_PER_DAY);
}

// ---------------------------------------------------------------- 提交

async function submitOne(cand, token) {
  const r = await rpc({
    msgId: MSG_SUBMIT, token,
    params: encodeSubmitReq({
      guessingId: cand.guessingId,
      // 竞彩口径优先（池子给了 jc 盘口时），否则欧赔口径
      playType: cand.play_type,
      // pick 语义（逆向自前端 Yn()）：1-based，home=1/draw=2/away=3。
      // jcSpf/jcRq/europeodds 都走 index+1；欧赔 handicap 只允许 1 或 2
      // （走盘不提交），这里统一按三选一，走盘场景由上游让球概率缺失自然排除。
      pick: { home: 1, draw: 2, away: 3 }[cand.sel],
    }),
  });
  return { ok: r.status === 200 && r.code === 0, status: r.status, code: r.code, msg: r.msg };
}

// ---------------------------------------------------------------- 主流程

async function main() {
  const args = process.argv.slice(2);
  const apikey = readKey(APIKEY_FILE);
  const dateStr = args.includes('--date') ? args[args.indexOf('--date') + 1] : new Date().toISOString().slice(0, 10);
  const dryRun = !args.includes('--submit');

  if (args.includes('--probe')) {
    // 探活：合法 protobuf + 无凭证 → 期望「请先登录」
    const r = await rpc({ msgId: MSG_SUBMIT, params: encodeSubmitReq({ guessingId: 'probe', playType: PLAY_TYPE.SPF, pick: 1 }) });
    log(`HTTP ${r.status} code=${r.code} msg=${r.msg}`);
    if (r.status === 200 && r.msg && r.msg.includes('登录')) log('✅ 协议链路正确（端点/protobuf/编码都对），只差登录态');
    else log('⚠ 响应不符合预期，协议可能已变动——重新逆向（搜 ReqGuessingSubmit）');
    return;
  }

  if (args.includes('--quota')) {
    if (!apikey) throw new Error(`无 apikey: ${APIKEY_FILE}（数据 API key，只读）`);
    const q = await fetchQuota(apikey, weekIdOf());
    log(`本周配额 weekId=${q.weekId}: 已用 ${q.usedCount}/${q.maxCount}，剩余 ${q.remainCount}`);
    return;
  }

  if (args.includes('--pool')) {
    if (!apikey) throw new Error(`无 apikey: ${APIKEY_FILE}`);
    log('拉取开放池（listType=OPEN）...');
    const pool = await fetchPool(apikey);
    log(`开放池 ${pool.length} 场`);
    const byLeague = {};
    for (const p of pool) byLeague[p.league] = (byLeague[p.league] || 0) + 1;
    log('联赛分布: ' + Object.entries(byLeague).sort((a, b) => b[1] - a[1]).slice(0, 15)
      .map(([k, v]) => `${k}×${v}`).join('  '));
    writeJson(POOL_CACHE, { fetched_at: new Date().toISOString(), rows: pool });
    log(`已缓存 → ${POOL_CACHE}`);
    return;
  }

  const state = readJson(STATE_FILE, { weeks: {} });
  const wk = weekKey();
  state.weeks[wk] = state.weeks[wk] || { submitted: [], count: 0 };
  const usedThisWeek = state.weeks[wk].count || 0;

  if (args.includes('--status')) {
    log(`本周（${wk}）已提交 ${usedThisWeek}/${MAX_PER_WEEK}`);
    log(`已提场次: ${(state.weeks[wk].submitted || []).join(', ') || '（无）'}`);
    return;
  }
  if (usedThisWeek >= MAX_PER_WEEK) { log(`⚠ 本周配额已用尽（${usedThisWeek}/${MAX_PER_WEEK}）`); return; }

  // 1. 我们的候选
  const cands = loadCandidates(dateStr);
  const already = new Set(state.weeks[wk].submitted || []);
  const picked = pickBest(cands, already);
  log(`${dateStr}: 我们侧候选 ${picked.length} 场（odds∈[${ODDS_MIN},${ODDS_MAX}]）`);

  // 2. 匹配 NJStats 开放池
  let pool = readJson(POOL_CACHE, null);
  if (!pool || !pool.rows) {
    if (!apikey) throw new Error(`无 apikey 且无池缓存（${POOL_CACHE}）——先跑 --pool`);
    log('无池缓存，实时拉取...');
    pool = { rows: await fetchPool(apikey) };
  }
  const { matched, unmatched } = matchPool(picked, pool.rows);
  log(`开放池 ${pool.rows.length} 场；命中 ${matched.length}，未命中 ${unmatched.length}`);
  if (unmatched.length) {
    log(`未命中（联赛不在 NJStats 开放池）: ${unmatched.slice(0, 8).map(c => `${c.competition}:${c.home}vs${c.away}`).join('  ')}${unmatched.length > 8 ? ' …' : ''}`);
  }

  if (matched.length === 0) {
    log('⚠ 无注可提——我们侧没有一场落在 NJStats 开放池内（联赛不重叠）。');
    log('  这不是故障：等 NJStats 开放主流联赛，或我们扩展预测范围到池内联赛。');
    return;
  }

  for (const p of matched) {
    const line = p.line == null ? '' : ` line=${p.line > 0 ? `+${p.line}` : p.line}`;
    const pt = { 4: 'JC_SPF', 5: 'JC_RQ', 1: 'SPF', 2: 'RQ' }[p.play_type] || p.play_type;
    const jc = p.jc_available ? '竞彩口径' : '欧赔口径';
    log(`  ✓ ${p.match_id} ${p.home} vs ${p.away} → guessingId=${p.guessingId}  ${pt}/${p.sel}${line} odds=${p.odds} (${jc}) EV=${p.ev > 0 ? '+' : ''}${p.ev.toFixed(4)}`);
  }
  if (dryRun) { log('dry-run：加 --submit 真提交'); return; }

  // 3. 提交（必须 Bearer token）
  if (!readKey(TOKEN_FILE)) {
    throw new Error(
      `无登录态: ${TOKEN_FILE} 不存在。\n` +
      `GMI_SUBMIT 必须浏览器登录态（apikey 读得到池子和配额，但提交永远回「请先登录」）。\n` +
      `Air 上登录 NJStats → DevTools → 任意 /njserver/openApi/ 请求 → 复制 Bearer token。`
    );
  }
  const token = readKey(TOKEN_FILE);
  for (const p of matched) {
    log(`  提交 ${p.guessingId} (${p.home} vs ${p.away}) ...`);
    const r = await submitOne(p, token);
    if (!r.ok) { log(`  ❌ HTTP ${r.status} code=${r.code} ${r.msg}`); continue; }
    state.weeks[wk].submitted.push(p.match_id);
    state.weeks[wk].count = (state.weeks[wk].count || 0) + 1;
    writeJson(STATE_FILE, state);
    log(`  ✅ 已记录（本周 ${state.weeks[wk].count}/${MAX_PER_WEEK}）`);
  }

  if (fs.existsSync(VALUE_MODEL_FILE)) {
    const vm = readJson(VALUE_MODEL_FILE, { bets: [] });
    vm.bets = vm.bets || [];
    for (const p of matched) {
      vm.bets.push({
        date: dateStr, match_id: p.match_id, guessingId: p.guessingId,
        market: p.market, sel: p.sel, odds: p.odds, model_prob: p.model_prob,
        submitted_at: new Date().toISOString(),
      });
    }
    writeJson(VALUE_MODEL_FILE, vm);
  }
}

main().catch(e => { console.error(`❌ ${e.message}`); process.exit(1); });
