#!/data/data/com.termux/files/usr/bin/bash
set -u

LOG_DIR="$HOME/.logs"
CACHE_DIR="$HOME/.cache/dala"
LOG_FILE="$LOG_DIR/dala-server.log"
PID_FILE="$CACHE_DIR/server.pid"

log() {
    mkdir -p "$LOG_DIR"
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

release_wake_lock() {
    if command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock
    fi
}

if [ ! -f "$PID_FILE" ]; then
    log "PID file not found; searching for Dala server process"
    echo "PID file not found. Searching for Dala server..."
    if pkill -f "dala.server" || pkill -f "dala-server"; then
        log "Stopped Dala server by process match"
        echo "Dala server stopped"
    else
        log "No Dala server process found"
        echo "No Dala server process found"
    fi
    release_wake_lock
    exit 0
fi

server_pid="$(cat "$PID_FILE")"
if ! kill -0 "$server_pid" 2>/dev/null; then
    log "Stale PID file for $server_pid"
    echo "Dala server process not found"
    rm -f "$PID_FILE"
    release_wake_lock
    exit 0
fi

log "Stopping Dala server (PID: $server_pid)"
echo "Stopping Dala server (PID: $server_pid)..."
kill "$server_pid" 2>/dev/null || true

for _ in 1 2 3 4 5; do
    if ! kill -0 "$server_pid" 2>/dev/null; then
        log "Dala server stopped"
        echo "Dala server stopped"
        rm -f "$PID_FILE"
        release_wake_lock
        exit 0
    fi
    sleep 1
done

log "Dala server did not stop gracefully; forcing shutdown"
kill -9 "$server_pid" 2>/dev/null || true
sleep 1
rm -f "$PID_FILE"
release_wake_lock
echo "Dala server stopped"
