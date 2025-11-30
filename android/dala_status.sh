#!/data/data/com.termux/files/usr/bin/bash

PID_FILE=~/.cache/epub-server.pid
LOG_FILE=~/.logs/epub-server.log

if [ ! -f "$PID_FILE" ]; then
    echo "Server is NOT running (no PID file)"
    exit 1
fi

SERVER_PID=$(cat "$PID_FILE")

if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Server is RUNNING (PID: $SERVER_PID)"
    echo ""
    echo "Process info:"
    ps -p "$SERVER_PID" -o pid,ppid,cmd,etime
    echo ""
    echo "Recent log entries:"
    tail -n 10 "$LOG_FILE"
    exit 0
else
    echo "Server is NOT running (stale PID file)"
    exit 1
fi
