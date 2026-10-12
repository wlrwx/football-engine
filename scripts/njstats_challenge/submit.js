#!/usr/bin/env node
/**
 * NJStats 预测挑战提交器（v3，2026-10-13 重建版）
 *
 * ⚠️ 本文件是 **Mac Air 上 `_vault/njstats_challenge_submit.js` 的仓库重建版**。
 * 原文件从未入仓库（`_vault/` 在 .gitignore），只存在于 Air 本地磁盘。
 * 重建依据：STATUS.md「预测挑战状态」节记录的全部实测规则 + predictions.json 字段。
 * Air 重装后按本文件 + README.md 恢复，**不要凭记忆重写**。
 *
 * ## 为什么必须重建进仓库
 * 原提交器是「黑工」：代码不在 git 里、状态不在 git 里，Air 一出问题，
 * 「每日 11:30 提交」这个唯一无法上云的任务就断了，且仓库里没有任何记录。
 *
 * ## 提交规则（2026-10-12 实测，来自 STATUS.md）
 * 1. **每场仅一市场**：同一场提过即消失，不能既提 1X2 又提让球
 * 2. **低赔带豁免只适用胜平负**（让球没有低赔豁免）
 * 3. **EV 策略**：只提赔率 ∈ [1.60, 2.31] 且模型概率最高的场次
 * 4. **每日 ≤5 场，周配额 28**
 * 5. 跨 胜平负/让球 择优（大小球只观察不提交）
 * 6. 让球按水位口径记录（主队-1@0.95）
 *
 * ## 用法
 *   node scripts/njstats_challenge/submit.js --date 2026-10-13          # dry-run
 *   node scripts/njstats_challenge/submit.js --date 2026-10-13 --submit # 真提交
 *   node scripts/njstats_challenge/submit.js --status                   # 看本周配额
 *
 * 登录态：需要 Air 上浏览器已登录 NJStats（cookie 存 ~/.football-engine/njstats_cookies.json）。
 * 本脚本**不**处理登录——那部分必须人工在 Air 上完成一次。
 */

'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// ---------------------------------------------------------------- 配置

// ROOT 不能用 path.resolve(__dirname, '..', '..')：那样在「源码被 load 进别的
// 上下文」时（测试用 new Function 载入纯函数段、或别人 require 本文件）会失效，
// __dirname 不存在 → 静默解析到 CWD 附近 → 找不到 predictions.json 报
// 「无预测文件」，而真实原因是路径基准丢了。改成优先用 process.argv[1] 的目录。
const SELF_DIR = (() => {
  try {
    // 正常 node 执行: argv[1] = .../scripts/njstats_challenge/submit.js
    if (process.argv[1] && fs.existsSync(process.argv[1])) {
      return path.dirname(path.resolve(process.argv[1]));
    }
  } catch { /* fallthrough */ }
  return __dirname;   // 直接 require 时
})();
const ROOT = path.resolve(SELF_DIR, '..', '..');
const STATE_DIR = path.join(process.env.HOME, '.football-engine');
const STATE_FILE = path.join(STATE_DIR, 'challenge_state.json');
const COOKIE_FILE = path.join(STATE_DIR, 'njstats_cookies.json');
const VALUE_MODEL_FILE = path.join(STATE_DIR, 'njstats_value_model.json');

const API_BASE = 'https://www.njstats.cn/api';   // 以实际抓包为准【易变】

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

// ---------------------------------------------------------------- 读取预测

/**
 * 从 data/daily/<date>/predictions.json 提取可提交候选。
 *
 * 返回 [{ match_id, market, sel, odds, model_prob, line }]
 *   market: '1x2' | 'handicap'
 *   sel:    'home' | 'draw' | 'away'（让球时同为主/客）
 *   line:   让球盘口（如 -1.0），1x2 为 null
 */
function loadCandidates(dateStr) {
  const f = path.join(ROOT, 'data', 'daily', dateStr, 'predictions.json');
  if (!fs.existsSync(f)) throw new Error(`无预测文件: ${f}（先跑 daily-forecast）`);
  const preds = readJson(f, []);
  const out = [];

  for (const p of preds) {
    if (p.odds_synthetic) continue;            // 合成赔率不可作为提交依据
    // --- 1X2 ---
    const sides = [
      ['home', p.home_odds, p.home_win_prob],
      ['draw', p.draw_odds, p.draw_prob],
      ['away', p.away_odds, p.away_win_prob],
    ];
    for (const [sel, odds, prob] of sides) {
      if (odds && prob) out.push({
        match_id: p.match_id, market: '1x2', sel,
        odds: Number(odds), model_prob: Number(prob), line: null,
      });
    }
    // --- 让球（只有 handicap_*_odds/prob 齐全才算候选）---
    if (p.handicap != null && p.handicap_home_odds && p.handicap_away_odds) {
      if (p.handicap_home_prob) out.push({
        match_id: p.match_id, market: 'handicap', sel: 'home',
        odds: Number(p.handicap_home_odds), model_prob: Number(p.handicap_home_prob),
        line: Number(p.handicap),
      });
      if (p.handicap_away_prob) out.push({
        match_id: p.match_id, market: 'handicap', sel: 'away',
        odds: Number(p.handicap_away_odds), model_prob: Number(p.handicap_away_prob),
        line: Number(p.handicap),
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
 * 正数=主队被让球。提交给 NJStats 时若符号反了，系统不会报错，
 * 只会静默记成相反的一场 —— 结算时才发现全错。
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
 *   - 只保留 odds ∈ [1.60, 2.31]（低赔带豁免只适用胜平负，这里统一按此带）
 *   - 每场只留一个市场里模型概率最高的一票（「每场仅一市场」）
 *   - 按 EV = p*odds - 1 降序
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

// ---------------------------------------------------------------- 主流程

async function main() {
  const args = process.argv.slice(2);
  const dateStr = args.includes('--date')
    ? args[args.indexOf('--date') + 1]
    : new Date().toISOString().slice(0, 10);
  const dryRun = !args.includes('--submit');
  const showStatus = args.includes('--status');

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
    log(`  ${p.match_id}  ${p.market}/${p.sel}${line}  odds=${p.odds}  p=${p.model_prob.toFixed(4)}  EV=${p.ev > 0 ? '+' : ''}${p.ev.toFixed(4)}`);
  }

  if (dryRun) {
    log('dry-run：加 --submit 真提交');
    return;
  }

  // 登录态检查（cookie 由 Air 上人工登录一次产生）
  if (!fs.existsSync(COOKIE_FILE)) {
    throw new Error(
      `无登录态: ${COOKIE_FILE} 不存在。\n` +
      `预测挑战必须浏览器登录态，无法在 CI/Air 无人值守完成——\n` +
      `请在 Air 上打开 NJStats 登录后导出 cookie 到该路径，再跑 --submit。`
    );
  }

  // 提交（真实端点以抓包为准【易变】）
  const cookies = readJson(COOKIE_FILE, {});
  for (const p of picks) {
    const body = {
      match_id: p.match_id,
      market: p.market,
      sel: p.sel,
      line: p.line,
      odds: p.odds,
      model_prob: p.model_prob,
    };
    log(`  提交 ${p.match_id} ...`);
    const res = await fetch(`${API_BASE}/challenge/predict`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Cookie': Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; '),
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      log(`  ❌ ${res.status} ${await res.text()}`);
      continue;
    }
    state.weeks[wk].submitted.push(p.match_id);
    state.weeks[wk].count = (state.weeks[wk].count || 0) + 1;
    writeJson(STATE_FILE, state);
    log(`  ✅ 已记录（本周 ${state.weeks[wk].count}/${MAX_PER_WEEK}）`);
  }

  // 战绩模型文件（STATUS.md 提到的 1,667 注战绩）—— 存在即保留，不存在不新建
  if (fs.existsSync(VALUE_MODEL_FILE)) {
    const vm = readJson(VALUE_MODEL_FILE, { bets: [] });
    vm.bets = vm.bets || [];
    for (const p of picks) {
      vm.bets.push({
        date: dateStr, match_id: p.match_id, market: p.market,
        sel: p.sel, odds: p.odds, model_prob: p.model_prob,
        submitted_at: new Date().toISOString(),
      });
    }
    writeJson(VALUE_MODEL_FILE, vm);
  }
}

main().catch(e => { console.error(`❌ ${e.message}`); process.exit(1); });
