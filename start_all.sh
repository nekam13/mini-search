#!/bin/bash

# Mini Search - Startup Script v2.0
# Optimized for Ubuntu 26.04 ARM64 + Termux environment

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
print_header "Mini Search - Spouštění systému v2.0"
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

# Function to check and kill process on port
kill_port() {
    local PORT=$1
    print_status "Kontrola portu $PORT..."
    
    # Check if port is in use
    if command -v lsof &> /dev/null; then
        if lsof -i :$PORT > /dev/null 2>&1; then
            print_warning "Port $PORT je obsazený"
            
            # Try fuser
            if command -v fuser &> /dev/null; then
                print_status "Ukončuji procesy na portu $PORT (fuser)..."
                fuser -k $PORT/tcp 2>/dev/null || true
            else
                # Try pkill
                print_status "Ukončuji procesy na portu $PORT (pkill)..."
                pkill -f ":$PORT" 2>/dev/null || true
                pkill -f "port=$PORT" 2>/dev/null || true
            fi
            
            sleep 2
            
            # Verify port is free
            if lsof -i :$PORT > /dev/null 2>&1; then
                print_error "Nepodařilo se ukončit procesy na portu $PORT"
                print_info "Zkuste ručně: sudo lsof -i :$PORT"
                return 1
            fi
        fi
    elif command -v netstat &> /dev/null; then
        if netstat -tuln | grep ":$PORT " > /dev/null 2>&1; then
            print_warning "Port $PORT je obsazený"
            pkill -f ":$PORT" 2>/dev/null || true
            sleep 2
        fi
    fi
    
    print_success "Port $PORT je volný"
    return 0
}

# Kill processes on ports 5000 and 8095
kill_port 5000
kill_port 8095

echo ""
print_header "Spouštění služeb"
echo ""

# Start admin console (port 5000)
print_status "Spouštím správcovskou konzoli na portu 5000..."
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
print_info "🔧 Správcovská konzole: http://localhost:5000"
print_info "🔍 Vyhledávání:       http://localhost:8095"
echo ""

# Show process info
print_status "Aktivní procesy:"
echo "------------------------------------------"
if command -v ps &> /dev/null; then
    ps aux | grep -E "app.py|search_ui.py" | grep -v grep | while read line; do
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
print_info "  kill $APP_PID $SEARCH_PID"
echo ""

# Check if ports are accessible
echo "Kontrola dostupnosti portů..."
sleep 3

for PORT in 5000 8095; do
    if curl -s -o /dev/null -I "http://localhost:$PORT" > /dev/null 2>&1; then
        print_success "✅ Port $PORT je dostupný"
    else
        print_warning "⚠️  Port $PORT není ještě dostupný (čekání na spuštění)..."
    fi
done

echo ""
print_success "✅ Spouštění dokončeno"
echo ""
