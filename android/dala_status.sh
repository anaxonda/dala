#!/data/data/com.termux/files/usr/bin/bash

# Configuration
PID_FILE=~/.cache/epub-server.pid
LOG_FILE=~/.logs/epub-server.log

# Colors for better readability
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to display status
show_status() {
    clear

    echo -e "${BLUE}======================================"
    echo "      EPUB Server Status"
    echo "======================================${NC}"
    echo ""
    echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""

    if [ ! -f "$PID_FILE" ]; then
        echo -e "${RED}Status: NOT RUNNING${NC} (no PID file)"
        echo ""

        # Check if any python server process is running anyway
        if pgrep -f "python.*server.py" > /dev/null; then
            echo -e "${YELLOW}⚠ Warning: Found orphaned server process${NC}"
            echo "Consider running stop script to clean up"
        fi
    else
        SERVER_PID=$(cat "$PID_FILE")

        if kill -0 "$SERVER_PID" 2>/dev/null; then
            UPTIME=$(ps -p "$SERVER_PID" -o etime= | xargs)
            echo -e "${GREEN}Status: RUNNING ✓${NC}"
            echo "PID: $SERVER_PID"
            echo "Uptime: $UPTIME"
            echo ""
            echo "Process Info:"
            echo "--------------------------------------"
            ps -p "$SERVER_PID" -o pid,ppid,%cpu,%mem,cmd | tail -n +2
            echo ""

            # Check if log file exists and show size
            if [ -f "$LOG_FILE" ]; then
                LOG_SIZE=$(du -h "$LOG_FILE" | cut -f1)
                LOG_LINES=$(wc -l < "$LOG_FILE")
                echo "Log: $LOG_FILE ($LOG_SIZE, $LOG_LINES lines)"
                echo ""
                echo "Recent Logs (last 10 lines):"
                echo "--------------------------------------"
                tail -n 10 "$LOG_FILE" | sed 's/^/  /'
            fi
        else
            echo -e "${RED}Status: NOT RUNNING${NC} (stale PID file)"
            echo "PID file exists but process $SERVER_PID is not running"
            echo ""
            echo -e "${YELLOW}Recommendation: Run stop script to clean up${NC}"
        fi
    fi

    echo ""
    echo -e "${BLUE}======================================"
    echo "Options:"
    echo "  ${GREEN}[Enter]${NC} - Close"
    echo "  ${GREEN}[l]${NC} - View full logs (less)"
    echo "  ${GREEN}[t]${NC} - Tail logs (follow)"
    echo "  ${GREEN}[r]${NC} - Refresh status"
    echo "  ${GREEN}[c]${NC} - Clear screen"
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "  ${YELLOW}[s]${NC} - Stop server"
    else
        echo "  ${GREEN}[S]${NC} - Start server"
    fi
    echo "  ${GREEN}[h]${NC} - Show help"
    echo "======================================${NC}"
    echo ""
}

# Function to show help
show_help() {
    clear
    echo -e "${BLUE}======================================"
    echo "      Help & Information"
    echo "======================================${NC}"
    echo ""
    echo "Status Script - Manages EPUB server"
    echo ""
    echo "File Locations:"
    echo "  PID:  $PID_FILE"
    echo "  Log:  $LOG_FILE"
    echo ""
    echo "Related Scripts:"
    echo "  Start:  ~/bin/start-server.sh"
    echo "  Stop:   ~/bin/stop-server.sh"
    echo "  Status: ~/bin/status-server.sh"
    echo ""
    echo "Navigation:"
    echo "  - Use single key commands"
    echo "  - Press 'r' to refresh status"
    echo "  - Press 'l' to view full logs"
    echo "  - Press 't' to tail logs in real-time"
    echo ""
    read -n 1 -p "Press any key to return..."
}

# Main loop
while true; do
    show_status

    read -n 1 -s choice
    echo ""

    case "$choice" in
        l|L)
            if [ -f "$LOG_FILE" ]; then
                less +G "$LOG_FILE"  # +G starts at end of file
            else
                echo "Log file not found"
                sleep 2
            fi
            ;;
        t|T)
            if [ -f "$LOG_FILE" ]; then
                echo "Following logs (Ctrl+C to stop)..."
                sleep 1
                tail -f "$LOG_FILE"
            else
                echo "Log file not found"
                sleep 2
            fi
            ;;
        r|R)
            # Just loop again to refresh
            ;;
        c|C)
            clear
            ;;
        s|S)
            if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
                # Server is running, stop it
                echo "Stopping server..."
                if [ -f ~/bin/stop-server.sh ]; then
                    ~/bin/stop-server.sh
                else
                    echo "Stop script not found at ~/bin/stop-server.sh"
                fi
                sleep 2
            else
                # Server not running, start it
                echo "Starting server..."
                if [ -f ~/bin/start-server.sh ]; then
                    ~/bin/start-server.sh
                else
                    echo "Start script not found at ~/bin/start-server.sh"
                fi
                sleep 2
            fi
            ;;
        h|H)
            show_help
            ;;
        ""|q|Q)
            clear
            exit 0
            ;;
        *)
            # Invalid option, just refresh
            ;;
    esac
done
