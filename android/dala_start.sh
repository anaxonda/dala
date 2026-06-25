#!/data/data/com.termux/files/usr/bin/bash
set -u

LOG_DIR="$HOME/.logs"
CACHE_DIR="$HOME/.cache/dala"
LOG_FILE="$LOG_DIR/dala-server.log"
PID_FILE="$CACHE_DIR/server.pid"
PORT="${DALA_PORT:-8000}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

mkdir -p "$LOG_DIR" "$CACHE_DIR" "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"
export LOGLEVEL="${LOGLEVEL:-INFO}"

if [ -f "$PID_FILE" ]; then
    old_pid="$(cat "$PID_FILE")"
    if kill -0 "$old_pid" 2>/dev/null; then
        log "Server already running (PID: $old_pid)"
        echo "Dala server already running (PID: $old_pid)"
        sleep 2
        exit 0
    fi
    rm -f "$PID_FILE"
fi

if ! command -v dala-server >/dev/null 2>&1; then
    log "FATAL: dala-server not found in PATH"
    echo "dala-server not found."
    echo "Install Dala first:"
    echo "  sh install-dala.sh"
    sleep 3
    exit 1
fi

if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    log "Wake lock acquired"
fi

log "Starting Dala server on port $PORT"
nohup dala-server --no-open --port "$PORT" >> "$LOG_FILE" 2>&1 &
server_pid=$!
echo "$server_pid" > "$PID_FILE"

sleep 1
if kill -0 "$server_pid" 2>/dev/null; then
    log "Server started successfully (PID: $server_pid)"
    echo "Dala server started (PID: $server_pid)"
    echo "Open http://127.0.0.1:$PORT/"
    sleep 2
    exit 0
fi

log "ERROR: Server failed to start"
rm -f "$PID_FILE"
if command -v termux-wake-unlock >/dev/null 2>&1; then
    termux-wake-unlock
fi
echo "Dala server failed to start. Check $LOG_FILE"
sleep 3
exit 1
