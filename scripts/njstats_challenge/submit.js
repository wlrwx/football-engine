#!/usr/bin/env node
/**
 * NJStats 预测挑战提交器（v3.1，2026-10-13 协议实证版）
 *
 * ⚠️ 本文件是 **Mac Air 上 `_vault/njstats_challenge_submit.js` 的仓库重建版**。
 * 原文件从未入仓库（`_vault/` 在 .gitignore），只存在于 Air 本地磁盘。
 * Air 重装后按本文件 + README.md 恢复，**不要凭记忆重写**。
 *
 * ## v3 → v3.1 改了什么（重要）
 * v3 的 API 端点是**推断**的（`POST /api/challenge/predict` JSON）。
 * 2026-10-13 直接从 NJStats 前端 bundle 逆向出真实协议并实测通过：
 *   - 真实端点是 `POST /njserver/openApi/`（**不是** /api/...）
 *   - Content-Type 是 `application/x-protobuf`（**不是** JSON）
 *   - 鉴权是 `Authorization: Bearer <token>` + `modId`/`msgId` 头
 *   - 已实测：合法 protobuf + 无 token → HTTP 200 + 「请先登录」，
 *     协议链路完全正确，只差登录态
 *
 * ## 提交规则（2026-10-12 实测，来自 STATUS.md）
 * 1. **每场仅一市场**：同一场提过即消失，不能既提 1X2 又提让球
 * 2. **低赔带豁免只适用胜平负**（让球没有低赔豁免）
 * 3. **EV 策略**：只提赔率 ∈ [1.60, 2.31] 且模型概率最高的场次
 * 4. **每日 ≤5 场，周配额 28**（前端硬编码文案「每周28次预测机会」已佐证）
 * 5. 跨 胜平负/让球 择优（大小球只观察不提交）
 * 6. 让球按水位口径记录（主队-1@0.95）
 *
 * ## 用法
 *   node scripts/njstats_challenge/submit.js --date 2026-10-13          # dry-run
 *   node scripts/njstats_challenge/submit.js --date 2026-10-13 --submit # 真提交
 *   node scripts/njstats_challenge/submit.js --status                   # 看本周配额
 *   node scripts/njstats_challenge/submit.js --probe                    # 探活（不需登录）
 *
 * 登录态：需要 Air 上浏览器已登录 NJStats。token 存
 * ~/.football-engine/njstats_token.txt（一行 Bearer token）。
 * 本脚本**不**处理登录——那部分必须人工在 Air 上完成一次。
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
const TOKEN_FILE = path.join(STATE_DIR, 'njstats_token.txt');
const VALUE_MODEL_FILE = path.join(STATE_DIR, 'njstats_value_model.json');

// 2026-10-13 从前端 bundle 逆向并实测确认的协议常量
const PROTO_URL = 'https://www.njstats.cn/njserver/openApi/';
const MODULE_ID = '1012';   // MI_GUESSING
const MSG_SUBMIT = '2';     // GMI_SUBMIT

// GuessingPlayType 枚举（逆向自前端 bundle）
const PLAY_TYPE = {
  NONE: 0,
  SPF: 1,          // 胜平负（欧赔）
  RQ: 2,           // 让球（欧赔）
  DX: 3,           // 大小球
  JC_SPF: 4,       // 竞彩胜平负  ← 我们用这个
  JC_RQ: 5,        // 竞彩让球    ← 我们用这个
};

// EV 策略参数（STATUS.md 记录值）
const ODDS_MIN = 1.60;
const ODDS_MAX = 2.31;
const MAX_PER_DAY = 5;
const MAX_PER_WEEK = 28;

// ---------------------------------------------------------------- 工具

function log(msg) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

function readJson(p, fallback) {
  try { return JSON.parse(fs.readFileSync(p, 'utf-8')); }
  catch { return fallback; }
}

function writeJson(p, obj) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(obj, null, 2));
}

/**
 * 本周一（Asia/Shanghai）的日期串，作为周配额的 key。
 *
 * 必须按**北京时间**算周界：挑战按自然周结算，竞彩赛日也是北京时间口径。
 * 直接用 `new Date()` + getDay() 会踩时区坑——北京时间周一 07:00 对应
 * UTC 周日 23:00，getDay() 返回 0（周日）→ 被算成上一个周一，
 * 周一早上的提交会记到上周的配额里，周一部分提交被重复计算、周一部分凭空消失。
 */
function weekKey(d = new Date()) {
  const cn = new Date(d.getTime() + 8 * 3600 * 1000);   // shift 到 +08:00 再取 UTC 字段
  const day = cn.getUTCDay() === 0 ? 7 : cn.getUTCDay(); // 周日→7
  cn.setUTCDate(cn.getUTCDate() - (day - 1));
  return cn.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------- protobuf 手工编解码

/** varint 编码（protobuf wire type 0） */
function varint(n) {
  const out = [];
  let v = BigInt(n);
  while (true) {
    const b = Number(v & 0x7fn);
    v >>= 7n;
    if (v) out.push(b | 0x80);
    else { out.push(b); break; }
  }
  return Buffer.from(out);
}

/** protobuf 字段：length-delimited（wire type 2） */
function fBytes(no, buf) {
  const head = Buffer.from([(no << 3) | 2]);
  return Buffer.concat([head, varint(buf.length), buf]);
}

/** protobuf 字段：varint（wire type 0） */
function fVarint(no, v) {
  return Buffer.concat([Buffer.from([(no << 3) | 0]), varint(v)]);
}

/**
 * ReqGuessingSubmit 手工编码（protobuf-es 生成代码的等价物）：
 *   field 1: guessingId  string
 *   field 2: playType    enum (varint)
 *   field 3: pick        int32 (varint)
 */
function encodeSubmitReq({ guessingId, playType, pick }) {
  return Buffer.concat([
    fBytes(1, Buffer.from(String(guessingId), 'utf-8')),
    fVarint(2, playType),
    fVarint(3, pick),
  ]);
}

/** 外层信封：{ params(1, bytes), page(2, msg) } */
function encodeEnvelope(params) {
  return Buffer.concat([fBytes(1, params), fBytes(2, Buffer.alloc(0))]);
}

/** 最小 protobuf 解码器：只解外层的 code(1,varint) / msg(2,string) */
function decodeEnvelope(buf) {
  let i = 0;
  const out = {};
  while (i < buf.length) {
    const tag = buf[i]; i += 1;
    const no = tag >> 3, wt = tag & 7;
    if (wt === 0) {
      let v = 0n, shift = 0n;
      while (true) {
        const x = buf[i]; i += 1;
        v |= BigInt(x & 0x7f) << shift; shift += 7n;
        if (!(x & 0x80)) break;
      }
      out[no] = v;
    } else if (wt === 2) {
      let ln = 0, shift = 0;
      while (true) {
        const x = buf[i]; i += 1;
        ln |= (x & 0x7f) << shift; shift += 7;
        if (!(x & 0x80)) break;
      }
      out[no] = buf.slice(i, i + ln); i += ln;
    } else break;
  }
  if (out[2]) out.msg = out[2].toString('utf-8');
  if (out[1] !== undefined) out.code = Number(BigInt.asIntN(64, out[1]));
  return out;
}

// ---------------------------------------------------------------- 读取预测

/**
 * 从 data/daily/<date>/predictions.json 提取可提交候选。
 *
 * 返回 [{ match_id, market, sel, odds, model_prob, line, play_type, pick, guessing_id }]
 *
 * ⚠️ guessingId 的来源：真实协议的 key 是 NJStats 侧的 guessingId，
 * **不是**我们的 match_id。当前实现用 match_id 占位——正确做法是先调
 * GMI_MATCH_LIST(msgId=1) 拿 guessingId 再映射（见 README「已知未修」）。
 */
function loadCandidates(dateStr) {
  const f = path.join(ROOT, 'data', 'daily', dateStr, 'predictions.json');
  if (!fs.existsSync(f)) throw new Error(`无预测文件: ${f}（先跑 daily-forecast）`);
  const preds = readJson(f, []);
  const out = [];

  for (const p of preds) {
    if (p.odds_synthetic) continue;            // 合成赔率不可作为提交依据
    const sides = [
      ['home', p.home_odds, p.home_win_prob],
      ['draw', p.draw_odds, p.draw_prob],
      ['away', p.away_odds, p.away_win_prob],
    ];
    for (const [sel, odds, prob] of sides) {
      if (odds && prob) out.push({
        match_id: p.match_id, market: '1x2', sel,
        odds: Number(odds), model_prob: Number(prob), line: null,
        play_type: PLAY_TYPE.JC_SPF,
        pick: { home: 1, draw: 2, away: 3 }[sel],   // 逆向自前端 Yn()：pick = index+1
        guessing_id: p.match_id,                    // ⚠ 占位，见上
      });
    }
    if (p.handicap != null && p.handicap_home_odds && p.handicap_away_odds) {
      if (p.handicap_home_prob) out.push({
        match_id: p.match_id, market: 'handicap', sel: 'home',
        odds: Number(p.handicap_home_odds), model_prob: Number(p.handicap_home_prob),
        line: Number(p.handicap), play_type: PLAY_TYPE.JC_RQ,
        pick: 1, guessing_id: p.match_id,
      });
      if (p.handicap_away_prob) out.push({
        match_id: p.match_id, market: 'handicap', sel: 'away',
        odds: Number(p.handicap_away_odds), model_prob: Number(p.handicap_away_prob),
        line: Number(p.handicap), play_type: PLAY_TYPE.JC_RQ,
        pick: 3, guessing_id: p.match_id,
      });
    }
  }
  return out;
}

// ---------------------------------------------------------------- 择优

/**
 * 让球线符号校验（防符号错位）—— STATUS.md 明确记录这是 v2 提交器
 * 专门加的一道校验，所以必须保留。
 *
 * 竞彩让球口径：line 是**主队让球数**，负数=主队让球（主队强），
 * 正数=主队被让球。符号反了系统不会报错，只会静默记成相反的一场。
 */
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

/**
 * EV 择优：
 *   - 只保留 odds ∈ [1.60, 2.31]
 *   - 每场只留模型概率最高的一票（「每场仅一市场」）
 *   - 按 EV = p*odds - 1 降序，截到每日上限
 */
function pickBest(cands, alreadySubmitted) {
  const byMatch = new Map();
  for (const c of cands) {
    if (c.odds < ODDS_MIN || c.odds > ODDS_MAX) continue;
    if (alreadySubmitted.has(c.match_id)) continue;    // 提过即消失
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

/** 探活：不带 token 发一个合法 protobuf，期望拿到「请先登录」。 */
async function probe() {
  const body = encodeEnvelope(encodeSubmitReq({
    guessingId: 'probe', playType: PLAY_TYPE.JC_SPF, pick: 1,
  }));
  const res = await fetch(PROTO_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-protobuf',
      'modId': MODULE_ID,
      'msgId': MSG_SUBMIT,
    },
    body,
  });
  const buf = Buffer.from(await res.arrayBuffer());
  const env = decodeEnvelope(buf);
  log(`HTTP ${res.status} code=${env.code} msg=${env.msg}`);
  if (res.status === 200 && env.msg && env.msg.includes('登录')) {
    log('✅ 协议链路正确（端点/protobuf/编码都对），只差登录态');
    return true;
  }
  log('⚠ 响应不符合预期，协议可能已变动');
  return false;
}

async function submitOne(cand, token) {
  const body = encodeEnvelope(encodeSubmitReq({
    guessingId: cand.guessing_id, playType: cand.play_type, pick: cand.pick,
  }));
  const res = await fetch(PROTO_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-protobuf',
      'modId': MODULE_ID,
      'msgId': MSG_SUBMIT,
      'Authorization': `Bearer ${token}`,
    },
    body,
  });
  const buf = Buffer.from(await res.arrayBuffer());
  const env = decodeEnvelope(buf);
  return { ok: res.status === 200 && env.code === 0, status: res.status, code: env.code, msg: env.msg };
}

// ---------------------------------------------------------------- 主流程

async function main() {
  const args = process.argv.slice(2);
  const dateStr = args.includes('--date')
    ? args[args.indexOf('--date') + 1]
    : new Date().toISOString().slice(0, 10);
  const dryRun = !args.includes('--submit');
  const showStatus = args.includes('--status');

  if (args.includes('--probe')) { await probe(); return; }

  const state = readJson(STATE_FILE, { weeks: {} });
  const wk = weekKey();
  state.weeks[wk] = state.weeks[wk] || { submitted: [], count: 0 };
  const usedThisWeek = state.weeks[wk].count || 0;

  if (showStatus) {
    log(`本周（${wk}）已提交 ${usedThisWeek}/${MAX_PER_WEEK}`);
    log(`已提场次: ${(state.weeks[wk].submitted || []).join(', ') || '（无）'}`);
    return;
  }

  if (usedThisWeek >= MAX_PER_WEEK) {
    log(`⚠ 本周配额已用尽（${usedThisWeek}/${MAX_PER_WEEK}），不提交`);
    return;
  }

  const cands = loadCandidates(dateStr);
  const already = new Set(state.weeks[wk].submitted || []);
  const picks = pickBest(cands, already);

  if (picks.length === 0) {
    log(`${dateStr}: 无符合 EV 策略（odds∈[${ODDS_MIN},${ODDS_MAX}]）的候选`);
    return;
  }

  log(`${dateStr}: 选出 ${picks.length} 场（本周已用 ${usedThisWeek}/${MAX_PER_WEEK}）`);
  for (const p of picks) {
    const line = p.line == null ? '' : ` line=${p.line > 0 ? `+${p.line}` : p.line}`;
    const pt = p.play_type === PLAY_TYPE.JC_SPF ? 'JC_SPF' : 'JC_RQ';
    log(`  ${p.match_id}  ${pt}/${p.sel}${line}  odds=${p.odds}  p=${p.model_prob.toFixed(4)}  pick=${p.pick}  EV=${p.ev > 0 ? '+' : ''}${p.ev.toFixed(4)}`);
  }

  if (dryRun) {
    log('dry-run：加 --submit 真提交');
    return;
  }

  // 登录态检查
  if (!fs.existsSync(TOKEN_FILE)) {
    throw new Error(
      `无登录态: ${TOKEN_FILE} 不存在。\n` +
      `预测挑战必须浏览器登录态，无法在 CI/Air 无人值守完成——\n` +
      `请在 Air 上打开 NJStats 登录后，从 DevTools 复制 Bearer token 到该文件（一行）。\n` +
      `先用 --probe 验证协议链路（不需 token）。`
    );
  }
  const token = fs.readFileSync(TOKEN_FILE, 'utf-8').trim();

  for (const p of picks) {
    log(`  提交 ${p.match_id} ...`);
    const r = await submitOne(p, token);
    if (!r.ok) {
      log(`  ❌ HTTP ${r.status} code=${r.code} ${r.msg}`);
      continue;
    }
    state.weeks[wk].submitted.push(p.match_id);
    state.weeks[wk].count = (state.weeks[wk].count || 0) + 1;
    writeJson(STATE_FILE, state);
    log(`  ✅ 已记录（本周 ${state.weeks[wk].count}/${MAX_PER_WEEK}）`);
  }

  if (fs.existsSync(VALUE_MODEL_FILE)) {
    const vm = readJson(VALUE_MODEL_FILE, { bets: [] });
    vm.bets = vm.bets || [];
    for (const p of picks) {
      vm.bets.push({
        date: dateStr, match_id: p.match_id, market: p.market,
        sel: p.sel, odds: p.odds, model_prob: p.model_prob,
        play_type: p.play_type, pick: p.pick,
        submitted_at: new Date().toISOString(),
      });
    }
    writeJson(VALUE_MODEL_FILE, vm);
  }
}

main().catch(e => { console.error(`❌ ${e.message}`); process.exit(1); });
