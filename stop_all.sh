#!/bin/bash

# Mini Search - Stop Script v2.0
# Gracefully stops all Mini Search services

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

print_header() {
    echo -e "${BLUE}==========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}==========================================${NC}"
}

print_status() {
    echo -e "${BLUE}[*]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[+]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

print_error() {
    echo -e "${RED}[-]${NC} $1"
}

echo ""
print_header "Mini Search - Ukončování systému v2.0"
echo ""

# Function to kill process by name
kill_process() {
    local PROCESS_NAME=$1
    print_status "Ukončuji $PROCESS_NAME..."
    
    # Try pkill first
    pkill -f "$PROCESS_NAME" 2>/dev/null || true
    sleep 1
    
    # Check if still running
    if pgrep -f "$PROCESS_NAME" > /dev/null 2>&1; then
        print_warning "První pokus neúspěšný, zkouším silnější metodu..."
        pkill -9 -f "$PROCESS_NAME" 2>/dev/null || true
        sleep 1
    fi
    
    # Verify
    if pgrep -f "$PROCESS_NAME" > /dev/null 2>&1; then
        print_error "Nepodařilo se ukončit $PROCESS_NAME"
        return 1
    else
        print_success "$PROCESS_NAME ukončen"
        return 0
    fi
}

# Kill all Mini Search processes
kill_process "app.py"
kill_process "search_ui.py"
kill_process "crawler_engine.py"

# Also try by port
for PORT in 8070 8095; do
    print_status "Kontrola portu $PORT..."
    if command -v lsof &> /dev/null; then
        if lsof -i :$PORT > /dev/null 2>&1; then
            print_warning "Port $PORT je stále obsazený"
            fuser -k $PORT/tcp 2>/dev/null || true
            sleep 1
            if lsof -i :$PORT > /dev/null 2>&1; then
                print_error "Port $PORT zůstává obsazený"
            else
                print_success "Port $PORT uvolněn"
            fi
        else
            print_success "Port $PORT je volný"
        fi
    elif command -v netstat &> /dev/null; then
        if netstat -tuln | grep ":$PORT " > /dev/null 2>&1; then
            print_warning "Port $PORT je obsazený"
            pkill -9 -f ":$PORT" 2>/dev/null || true
            sleep 1
        else
            print_success "Port $PORT je volný"
        fi
    else
        print_info "Není dostupný lsof ani netstat, přeskakuji kontrolu portů"
    fi
done

echo ""
print_header "Všechny služby Mini Search ukončeny"
echo ""

# Show remaining processes
print_status "Zbývající Python procesy:"
echo "------------------------------------------"
if command -v ps &> /dev/null; then
    ps aux | grep python | grep -v grep | grep -v ".sh" | while read line; do
        if [ -n "$line" ]; then
            echo "  $line"
        fi
    done
else
    print_info "ps není dostupný"
fi
echo "------------------------------------------"
echo ""

print_success "✅ Ukončování dokončeno"
echo ""
