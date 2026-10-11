#!/bin/bash
# football-engine 自动预测推送脚本
# 包含重试机制，解决 GitHub 国内网络不稳定问题

cd "$(dirname "$0")"

LOG_FILE="$HOME/logs/football-engine.log"
mkdir -p "$(dirname "$LOG_FILE")"
MAX_RETRIES=5
RETRY_DELAY=30

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "========== 开始执行预测流水线 =========="

# 兜底清场：上一轮若中断在 pull --rebase 半途（空提交/冲突 stop），残留的
# rebase-merge 会让本轮在 detached HEAD 上提交、推送全灭。数据每小时重生成，远端为准。
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    git rebase --quit 2>/dev/null || git rebase --abort 2>/dev/null
    git fetch origin main -q
    git checkout -B main && git reset --hard origin/main
    log "⚠ 检测到上次残留的 rebase 状态，已清场并对齐远端"
fi

# 代理自愈（2026-08-31，09-18 升级为可复用函数）：git 推送走 127.0.0.1:7897（Clash Verge）。
# 代理挂掉时自动拉起 Clash Verge 并等待端口恢复，避免整轮推送静默失败。
heal_proxy() {
    if nc -z -w 2 127.0.0.1 7897 2>/dev/null; then return 0; fi
    log "⚠ 检测到代理端口 7897 不通，尝试自动启动 Clash Verge..."
    open -a "Clash Verge" 2>/dev/null || log "❌ Clash Verge 启动失败（未安装?）"
    for _i in $(seq 1 12); do
        sleep 5
        if nc -z -w 2 127.0.0.1 7897 2>/dev/null; then
            log "✅ 代理已恢复（等待 $((_i * 5))s）"
            return 0
        fi
    done
    log "❌ 代理 60s 内未恢复"
    return 1
}
heal_proxy || true

# 虚拟环境探测（不再硬编码 /Users/dykily/... 路径）
PY=python3
for _cand in ".venv/bin/python3" "venv/bin/python3" "$HOME/.hermes/hermes-agent/venv/bin/python3"; do
    if [ -x "$_cand" ]; then PY="$_cand"; break; fi
done
log "使用解释器: $PY"

# 系统医生（2026-10-11）：统一体检 + 安全自愈（git 清场/补推、补抓赛果、补结算、
# 代理拉起、refspec 修复）。报告落 data/state/system_health.json，critical 不阻断本脚本。
run_doctor() {
    log "---------- 系统医生体检 ----------"
    "$PY" -m engine.doctor --quiet >> "$LOG_FILE" 2>&1 || true
}

# Step 1: 运行预测
log "Step 1/3: 运行预测..."
"$PY" -m engine.main --date "$(TZ=Asia/Shanghai date +%Y-%m-%d)" 2>&1 | tee -a "$LOG_FILE"
if [ ${PIPESTATUS[0]} -ne 0 ]; then
    log "❌ 预测执行失败"
    exit 1
fi

# Step 2: 构建页面
log "Step 2/3: 生成页面..."
"$PY" -c "from engine.build_site import build_site; build_site()" 2>&1 | tee -a "$LOG_FILE"

# Step 3: Git 提交 + 推送（带重试；只提交数据/页面，不提交代码）
log "Step 3/3: Git 提交推送..."
git add data/ web/
if git diff --cached --quiet; then
    log "✅ 无变更，无需提交"
    run_doctor
    exit 0
fi
git commit -m "Auto update: $(date '+%Y-%m-%d %H:%M') 预测数据更新" 2>&1 | tee -a "$LOG_FILE"

# Git push 重试机制：每次重试前先 pull --rebase，否则面对 Actions 的持续推送
# 永远是非 fast-forward，5 次重试全部失败。
git config http.version HTTP/1.1
git config http.postBuffer 524288000
for i in $(seq 1 $MAX_RETRIES); do
    log "  推送尝试 $i/$MAX_RETRIES..."
    heal_proxy || log "  ⚠ 代理仍未恢复，尝试直连推送"
    # 必须用 PIPESTATUS 判断 git 的退出码：`if ! git pull | tee` 测的是 tee 的，
    # bash 管道默认取末位命令退出码——自愈分支会成为永远不执行的死代码
    # （2026-10-11 两次卡死复发的真根因）。
    git pull --rebase --autostash origin main 2>&1 | tee -a "$LOG_FILE"
    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        # pull --rebase 停在空提交时处于"编辑中"状态，残留 rebase-merge 会让
        # HEAD 游离、push 永远报 not on a branch。quit 清状态后强制回 main 对齐远端。
        git rebase --quit 2>/dev/null || git rebase --abort 2>/dev/null
        log "  ↻ rebase 冲突，对齐远端后重试（监控数据每小时重生成，远端为准）"
        git fetch origin main 2>&1 | tee -a "$LOG_FILE"
        git checkout -B main 2>&1 | tee -a "$LOG_FILE"
        git reset --hard origin/main 2>&1 | tee -a "$LOG_FILE"
    fi
    git push 2>&1 | tee -a "$LOG_FILE"
    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        log "✅ 推送成功！"
        run_doctor
        exit 0
    fi
    log "  ⏳ 推送失败，等待 $RETRY_DELAY 秒后重试..."
    sleep $RETRY_DELAY
done

log "❌ 所有重试均失败，请检查网络"
run_doctor
exit 1
