#!/data/data/com.termux/files/usr/bin/bash

# Configuration
PROJECT_DIR=~/dala
VENV_PYTHON=$PROJECT_DIR/.venv/bin/python
LOG_DIR=~/.logs
LOG_FILE=$LOG_DIR/epub-server.log
PID_FILE=~/.cache/epub-server.pid
SERVER_SCRIPT=server.py

# Function to log with timestamp
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Create necessary directories
mkdir -p "$LOG_DIR" ~/.cache

# Check if server is already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        log "ERROR: Server already running (PID: $OLD_PID)"
        echo "Server already running (PID: $OLD_PID)"
        sleep 2
        exit 0
    else
        log "Removing stale PID file"
        rm "$PID_FILE"
    fi
fi

# Acquire wake lock
termux-wake-lock
log "Wake lock acquired"

# Change to project directory
if ! cd "$PROJECT_DIR"; then
    log "FATAL: Failed to cd to $PROJECT_DIR"
    termux-wake-unlock
    sleep 2
    exit 1
fi

# Validate Python interpreter
if [ ! -x "$VENV_PYTHON" ]; then
    log "FATAL: Python not found or not executable at $VENV_PYTHON"
    termux-wake-unlock
    sleep 2
    exit 1
fi

# Validate server script
if [ ! -f "$SERVER_SCRIPT" ]; then
    log "FATAL: $SERVER_SCRIPT not found in $PROJECT_DIR"
    termux-wake-unlock
    sleep 2
    exit 1
fi

# Set environment variables
export UV_CACHE_DIR=$HOME/.cache/uv
export UV_LINK_MODE=copy

# Log startup
log "Starting EPUB server from $PROJECT_DIR"

# Start server with nohup to detach from terminal
nohup "$VENV_PYTHON" "$SERVER_SCRIPT" >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!

# Save PID
echo "$SERVER_PID" > "$PID_FILE"

# Brief verification
sleep 0.5
if kill -0 "$SERVER_PID" 2>/dev/null; then
    log "Server started successfully (PID: $SERVER_PID)"
    echo "✓ Server started (PID: $SERVER_PID)"
else
    log "ERROR: Server failed to start"
    rm "$PID_FILE"
    termux-wake-unlock
    echo "✗ Server failed to start"
    sleep 2
    exit 1
fi

# Exit quickly so Termux window closes (or user can navigate away)
sleep 1
exit 0
