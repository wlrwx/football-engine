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

# Step 0: 代理自愈（2026-08-31）：git 推送走 127.0.0.1:7897（Clash Verge）。
# 代理挂掉时自动拉起 Clash Verge 并等待端口恢复，避免整轮推送静默失败。
if ! nc -z -w 2 127.0.0.1 7897 2>/dev/null; then
    log "⚠ 检测到代理端口 7897 不通，尝试自动启动 Clash Verge..."
    open -a "Clash Verge" 2>/dev/null || log "❌ Clash Verge 启动失败（未安装?）"
    for _i in $(seq 1 12); do
        sleep 5
        if nc -z -w 2 127.0.0.1 7897 2>/dev/null; then
            log "✅ 代理已恢复（等待 $((_i * 5))s）"
            break
        fi
        if [ "$_i" = "12" ]; then log "❌ 代理 60s 内未恢复，本轮继续（推送可能失败）"; fi
    done
fi

# 虚拟环境探测（不再硬编码 /Users/dykily/... 路径）
PY=python3
for _cand in ".venv/bin/python3" "venv/bin/python3" "$HOME/.hermes/hermes-agent/venv/bin/python3"; do
    if [ -x "$_cand" ]; then PY="$_cand"; break; fi
done
log "使用解释器: $PY"

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
    exit 0
fi
git commit -m "Auto update: $(date '+%Y-%m-%d %H:%M') 预测数据更新" 2>&1 | tee -a "$LOG_FILE"

# Git push 重试机制：每次重试前先 pull --rebase，否则面对 Actions 的持续推送
# 永远是非 fast-forward，5 次重试全部失败。
git config http.version HTTP/1.1
git config http.postBuffer 524288000
for i in $(seq 1 $MAX_RETRIES); do
    log "  推送尝试 $i/$MAX_RETRIES..."
    git pull --rebase --autostash origin main 2>&1 | tee -a "$LOG_FILE"
    git push 2>&1 | tee -a "$LOG_FILE"
    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        log "✅ 推送成功！"
        exit 0
    fi
    log "  ⏳ 推送失败，等待 $RETRY_DELAY 秒后重试..."
    sleep $RETRY_DELAY
done

log "❌ 所有重试均失败，请检查网络"
exit 1
