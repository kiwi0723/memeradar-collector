#!/bin/bash
# Collector launcher — 启动多链信号采集器
# 用法: bash collector_launcher.sh [start|stop|status]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/.collector.pid"
LOG_FILE="$SCRIPT_DIR/collector.log"

case "${1:-start}" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
      echo "Collector already running (PID $(cat $PID_FILE))"
      exit 1
    fi
    echo "Starting collector..."
    cd "$SCRIPT_DIR"
    nohup python3 collector.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (PID $!)"
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      PID=$(cat "$PID_FILE")
      kill "$PID" 2>/dev/null && echo "Stopped (PID $PID)" || echo "Process not found"
      rm -f "$PID_FILE"
    else
      echo "No PID file found"
    fi
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
      PID=$(cat "$PID_FILE")
      echo "Running (PID $PID)"
      tail -3 "$LOG_FILE" 2>/dev/null
    else
      echo "Not running"
      [ -f "$PID_FILE" ] && echo "(stale PID file)"
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|status}"
    exit 1
    ;;
esac
