#!/bin/bash
# football-engine 自动预测推送脚本
# 包含重试机制，解决 GitHub 国内网络不稳定问题

cd "$(dirname "$0")"

LOG_FILE="/tmp/football-engine.log"
MAX_RETRIES=5
RETRY_DELAY=30

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "========== 开始执行预测流水线 =========="

# Step 1: 运行预测
log "Step 1/3: 运行预测..."
/Users/dykily/.hermes/hermes-agent/venv/bin/python3 -m engine.main --date today 2>&1 | tee -a "$LOG_FILE"
if [ ${PIPESTATUS[0]} -ne 0 ]; then
    log "❌ 预测执行失败"
    exit 1
fi

# Step 2: 构建页面
log "Step 2/3: 生成页面..."
/Users/dykily/.hermes/hermes-agent/venv/bin/python3 -c "from engine.build_site import build_site; build_site()" 2>&1 | tee -a "$LOG_FILE"

# Step 3: Git 推送（带重试）
log "Step 3/3: Git 推送..."
git add data/ web/ engine/
git commit -m "Auto update: $(date '+%Y-%m-%d %H:%M') 预测数据更新" 2>&1 | tee -a "$LOG_FILE"

# Git push 重试机制
for i in $(seq 1 $MAX_RETRIES); do
    log "  推送尝试 $i/$MAX_RETRIES..."
    git config http.version HTTP/1.1
    git config http.postBuffer 524288000
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
