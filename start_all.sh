#!/bin/bash

# Mini Search - Startup Script v3.0
# Optimized for Ubuntu 26.04 ARM64 + Termux environment
# Forcefully kills existing processes on ports 8070 and 8095

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

print_header() {
    echo -e "${MAGENTA}==========================================${NC}"
    echo -e "${MAGENTA}$1${NC}"
    echo -e "${MAGENTA}==========================================${NC}"
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

print_info() {
    echo -e "${CYAN}[i]${NC} $1"
}

echo ""
print_header "Mini Search - Spouštění systému v3.0"
echo ""
print_info "Prostředí: Ubuntu 26.04 ARM64 + Termux"
echo ""

# Check if running in venv
if [ -d "venv" ]; then
    print_status "Aktivace virtuálního prostředí..."
    source venv/bin/activate
    print_success "Virtuální prostředí aktivováno"
else
    print_warning "Virtuální prostředí nenalezeno, používám systémový Python"
fi

echo ""

# Function to forcefully kill process on port
kill_port() {
    local PORT=$1
    print_status "Násilné ukončení procesů na portu $PORT..."
    
    # Try multiple methods to kill processes
    
    # Method 1: fuser (most reliable)
    if command -v fuser &> /dev/null; then
        print_status "Používám fuser pro port $PORT..."
        fuser -k $PORT/tcp 2>/dev/null || true
        sleep 1
    fi
    
    # Method 2: lsof + kill
    if command -v lsof &> /dev/null; then
        print_status "Používám lsof + kill pro port $PORT..."
        PIDS=$(lsof -t -i :$PORT 2>/dev/null || true)
        if [ -n "$PIDS" ]; then
            for PID in $PIDS; do
                kill -9 $PID 2>/dev/null || true
                print_status "Zabil jsem proces PID: $PID"
            done
        fi
        sleep 1
    fi
    
    # Method 3: pkill by name
    print_status "Používám pkill pro python procesy..."
    pkill -9 -f ":$PORT" 2>/dev/null || true
    pkill -9 -f "port=$PORT" 2>/dev/null || true
    pkill -9 -f "app.py" 2>/dev/null || true
    pkill -9 -f "search_ui.py" 2>/dev/null || true
    sleep 1
    
    # Method 4: ss + kill (alternative to lsof)
    if command -v ss &> /dev/null; then
        PIDS=$(ss -tlnp | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' || true)
        if [ -n "$PIDS" ]; then
            for PID in $PIDS; do
                kill -9 $PID 2>/dev/null || true
                print_status "Zabil jsem proces PID: $PID (přes ss)"
            done
        fi
        sleep 1
    fi
    
    # Method 5: netstat + kill
    if command -v netstat &> /dev/null; then
        PIDS=$(netstat -tlnp | grep ":$PORT " | grep -oP '[0-9]+/[a-zA-Z]+' | cut -d'/' -f1 || true)
        if [ -n "$PIDS" ]; then
            for PID in $PIDS; do
                kill -9 $PID 2>/dev/null || true
                print_status "Zabil jsem proces PID: $PID (přes netstat)"
            done
        fi
        sleep 1
    fi
    
    # Verify port is free
    if command -v lsof &> /dev/null; then
        if lsof -i :$PORT > /dev/null 2>&1; then
            print_error "Port $PORT je stále obsazený!"
            print_info "Zkuste ručně: sudo fuser -k $PORT/tcp"
            return 1
        fi
    fi
    
    print_success "Port $PORT je volný"
    return 0
}

# Forcefully kill processes on ports 8070 and 8095
print_header "Ukončování existujících procesů"
echo ""

kill_port 8070
kill_port 8095

echo ""
print_header "Spouštění služeb"
echo ""

# Navigate to script directory
cd "$(dirname "$0")"

# Start admin console (port 8070)
print_status "Spouštím správcovskou konzoli na portu 8070..."
python3 app.py > /tmp/mini_search_app.log 2>&1 &
APP_PID=$!
print_success "Správcovská konzole spuštěna (PID: $APP_PID)"
print_info "Log: /tmp/mini_search_app.log"

# Start search UI (port 8095)
print_status "Spouštím vyhledávací rozhraní na portu 8095..."
python3 search_ui.py > /tmp/mini_search_ui.log 2>&1 &
SEARCH_PID=$!
print_success "Vyhledávací rozhraní spuštěno (PID: $SEARCH_PID)"
print_info "Log: /tmp/mini_search_ui.log"

echo ""
print_header "Systém spuštěn"
echo ""

# Show URLs
print_info "🔧 Správcovská konzole: http://localhost:8070"
print_info "🔍 Vyhledávání:       http://localhost:8095"}, {
echo ""

# Show process info
print_status "Aktivní procesy:"
echo "------------------------------------------"
if command -v ps &> /dev/null; then
    ps aux | grep -E "app.py|search_ui.py" | grep -v grep | grep -v ".sh" | while read line; do
        if [ -n "$line" ]; then
            print_info "$line"
        fi
    done
else
    print_info "ps není dostupný"
fi
echo "------------------------------------------"
echo ""

print_status "Pro zastavení systémů:"
print_info "  ./stop_all.sh"
print_info "nebo"
print_info "  kill -9 $APP_PID $SEARCH_PID"
echo ""

# Check if ports are accessible
echo "Kontrola dostupnosti portů..."
sleep 3

for PORT in 8070 8095; do
    if curl -s -o /dev/null -I "http://localhost:$PORT" > /dev/null 2>&1; then
        print_success "✅ Port $PORT je dostupný"
    else
        print_warning "⚠️  Port $PORT není ještě dostupný (čekání na spuštění)..."
    fi
done

echo ""
print_success "✅ Spouštění dokončeno"
echo ""
