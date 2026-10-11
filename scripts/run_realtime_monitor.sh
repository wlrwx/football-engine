#!/bin/bash
# football-engine 盘口实时监控快速启动脚本

cd "$(dirname "$0")"/..

LOG_FILE="./logs/realtime_monitor_run.log"
PYTHON="/Users/dykily/.hermes/hermes-agent/venv/bin/python3"

mkdir -p logs

# 代理自愈（2026-10-10）: cron 无人值守, Clash 关闭时推送全失败(凌晨连挂)
heal_proxy() {
    if nc -z -w 2 127.0.0.1 7897 2>/dev/null; then return 0; fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ 代理不通, 自动拉起 Clash Verge..." >> "$LOG_FILE"
    open -a "Clash Verge" 2>/dev/null
    for _i in $(seq 1 12); do
        sleep 5
        if nc -z -w 2 127.0.0.1 7897 2>/dev/null; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ 代理已恢复" >> "$LOG_FILE"
            return 0
        fi
    done
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ 代理 60s 未恢复" >> "$LOG_FILE"
    return 1
}
heal_proxy || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动实时监控..." >> "$LOG_FILE"

$PYTHON scripts/realtime_odds_monitor.py 2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=${PIPESTATUS[0]}

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 执行成功" >> "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 执行失败，退出码: $EXIT_CODE" >> "$LOG_FILE"
fi

echo "---" >> "$LOG_FILE"
