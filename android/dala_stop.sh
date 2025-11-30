#!/data/data/com.termux/files/usr/bin/bash

# Configuration
LOG_FILE=~/.logs/epub-server.log
PID_FILE=~/.cache/epub-server.pid

# Function to log with timestamp
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Check if PID file exists
if [ ! -f "$PID_FILE" ]; then
    log "WARNING: PID file not found, attempting to find server process"
    echo "PID file not found. Searching for running server..."

    # Try to find and kill by process name
    if pkill -f "python.*server.py"; then
        log "Server process killed by name"
        echo "Server stopped"
    else
        log "No server process found"
        echo "No server process found"
    fi

    termux-wake-unlock
    exit 0
fi

# Read PID
SERVER_PID=$(cat "$PID_FILE")

# Check if process is running
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    log "WARNING: Server process (PID: $SERVER_PID) not running"
    echo "Server process not found"
    rm "$PID_FILE"
    termux-wake-unlock
    exit 0
fi

log "Stopping server (PID: $SERVER_PID)"
echo "Stopping server (PID: $SERVER_PID)..."

# Try graceful shutdown first (SIGTERM)
if kill "$SERVER_PID" 2>/dev/null; then
    # Wait up to 5 seconds for graceful shutdown
    for i in {1..5}; do
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            log "Server stopped gracefully"
            echo "Server stopped successfully"
            rm "$PID_FILE"
            termux-wake-unlock
            exit 0
        fi
        sleep 1
    done

    # Force kill if still running
    log "Server did not stop gracefully, forcing shutdown"
    echo "Forcing shutdown..."
    kill -9 "$SERVER_PID" 2>/dev/null
    sleep 1
fi

# Verify shutdown
if kill -0 "$SERVER_PID" 2>/dev/null; then
    log "ERROR: Failed to stop server (PID: $SERVER_PID)"
    echo "ERROR: Failed to stop server"
    exit 1
else
    log "Server stopped (forced)"
    echo "Server stopped (forced)"
    rm "$PID_FILE"
fi

# Release wake lock
termux-wake-unlock
log "Wake lock released"
