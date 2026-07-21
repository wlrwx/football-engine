#!/bin/bash
# football-engine 盘口实时监控快速启动脚本

cd "$(dirname "$0")"/..

LOG_FILE="./logs/realtime_monitor_run.log"
PYTHON="/Users/dykily/.hermes/hermes-agent/venv/bin/python3"

mkdir -p logs

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动实时监控..." >> "$LOG_FILE"

$PYTHON scripts/realtime_odds_monitor.py 2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=${PIPESTATUS[0]}

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 执行成功" >> "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 执行失败，退出码: $EXIT_CODE" >> "$LOG_FILE"
fi

echo "---" >> "$LOG_FILE"
