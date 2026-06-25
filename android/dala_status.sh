#!/data/data/com.termux/files/usr/bin/bash
set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LOG_FILE="$HOME/.logs/dala-server.log"
PID_FILE="$HOME/.cache/dala/server.pid"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

server_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

show_status() {
    clear
    echo -e "${BLUE}======================================"
    echo -e "        Dala Server Status"
    echo -e "======================================${NC}"
    echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""

    if server_running; then
        server_pid="$(cat "$PID_FILE")"
        uptime="$(ps -p "$server_pid" -o etime= 2>/dev/null | xargs)"
        echo -e "${GREEN}Status: RUNNING${NC}"
        echo "PID: $server_pid"
        echo "Uptime: ${uptime:-unknown}"
        echo "URL: http://127.0.0.1:${DALA_PORT:-8000}/"
        echo ""
        ps -p "$server_pid" -o pid,ppid,%cpu,%mem,cmd 2>/dev/null || true
    else
        echo -e "${RED}Status: NOT RUNNING${NC}"
        if pgrep -f "dala.server|dala-server" >/dev/null 2>&1; then
            echo -e "${YELLOW}Warning: found a Dala server process without the PID file.${NC}"
        fi
    fi

    echo ""
    if [ -f "$LOG_FILE" ]; then
        log_size="$(du -h "$LOG_FILE" | cut -f1)"
        log_lines="$(wc -l < "$LOG_FILE")"
        echo "Log: $LOG_FILE ($log_size, $log_lines lines)"
        echo "Recent logs:"
        tail -n 10 "$LOG_FILE" | sed 's/^/  /'
    else
        echo "Log: $LOG_FILE (not created yet)"
    fi

    echo ""
    echo -e "${BLUE}======================================"
    echo -e "  ${GREEN}[Enter/q]${NC} Close"
    echo -e "  ${GREEN}[r]${NC} Refresh"
    echo -e "  ${GREEN}[l]${NC} View full log"
    echo -e "  ${GREEN}[t]${NC} Tail log"
    if server_running; then
        echo -e "  ${YELLOW}[s]${NC} Stop server"
    else
        echo -e "  ${GREEN}[S]${NC} Start server"
    fi
    echo -e "  ${YELLOW}[D]${NC} Restart with DEBUG logging"
    echo -e "======================================${NC}"
}

while true; do
    show_status
    read -r -n 1 -s choice
    echo ""
    case "$choice" in
        ""|q|Q)
            clear
            exit 0
            ;;
        r|R)
            ;;
        l|L)
            if [ -f "$LOG_FILE" ]; then
                less +G "$LOG_FILE"
            else
                echo "Log file not found"
                sleep 2
            fi
            ;;
        t|T)
            if [ -f "$LOG_FILE" ]; then
                echo "Following logs. Press Ctrl+C to stop."
                sleep 1
                tail -f "$LOG_FILE"
            else
                echo "Log file not found"
                sleep 2
            fi
            ;;
        s|S)
            if server_running; then
                "$SCRIPT_DIR/dala_stop.sh"
            else
                "$SCRIPT_DIR/dala_start.sh"
            fi
            sleep 2
            ;;
        d|D)
            if server_running; then
                "$SCRIPT_DIR/dala_stop.sh"
                sleep 1
            fi
            LOGLEVEL=DEBUG "$SCRIPT_DIR/dala_start.sh"
            sleep 2
            ;;
        *)
            ;;
    esac
done
