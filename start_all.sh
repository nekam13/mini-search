#!/bin/bash

# Mini Search - Startup skript v4.0
# Spouští všechny služby

# Barvy
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
    echo -e "${CYAN}[*]${NC} $1"
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

# Úvod
clear
echo ""
print_header "Mini Search - Spouštění systému v4.0"
echo ""
print_warning "Prostředí: Ubuntu 26.04 ARM64 + Termux"
echo ""

# Aktivace virtuálního prostředí
if [ -d "venv" ]; then
    print_status "Aktivace virtuálního prostředí..."
    source venv/bin/activate
    print_success "Virtuální prostředí aktivováno"
else
    print_warning "Virtuální prostředí nenalezeno, používám systémový Python"
fi

# Ukončení existujících procesů
print_header "Ukončování existujících procesů"
echo ""

kill_processes_on_port() {
    local port=$1
    print_status "Násilné ukončení procesů na portu $port..."
    
    # Zkus fuser
    if command -v fuser &> /dev/null; then
        fuser -k $port/tcp 2>/dev/null || true
    fi
    
    # Zkus lsof
    if command -v lsof &> /dev/null; then
        lsof -ti:$port | xargs kill -9 2>/dev/null || true
    fi
    
    # Zkus pkill
    pkill -f "python3.*port=$port" 2>/dev/null || true
    pkill -f "python3 app.py" 2>/dev/null || true
    pkill -f "python3 search_ui.py" 2>/dev/null || true
    pkill -f "python3 crawler_engine.py" 2>/dev/null || true
    
    # Počkej
    sleep 1
    
    # Kontrola
    if ss -tlnp 2>/dev/null | grep -q ":$port "; then
        print_warning "Port $port je stále obsazen, zkouším znovu..."
        sleep 2
        pkill -9 -f python3 2>/dev/null || true
        sleep 1
    fi
    
    print_success "Port $port je volný"
}

# Ukonči procesy na portech 8070 a 8095
kill_processes_on_port 8070
kill_processes_on_port 8095

echo ""

# Spouštění služeb
print_header "Spouštění služeb"
echo ""

# Spusť správcovskou konzoli
print_status "Spouštím správcovskou konzoli na portu 8070..."
python3 app.py > /tmp/mini_search_app.log 2>&1 &
APP_PID=$!
print_success "Správcovská konzole spuštěna (PID: $APP_PID)"
print_status "Log: /tmp/mini_search_app.log"

# Počkej chvíli
sleep 2

# Spusť vyhledávací rozhraní
print_status "Spouštím vyhledávací rozhraní na portu 8095..."
python3 search_ui.py > /tmp/mini_search_ui.log 2>&1 &
UI_PID=$!
print_success "Vyhledávací rozhraní spuštěno (PID: $UI_PID)"
print_status "Log: /tmp/mini_search_ui.log"

# Počkej chvíli
sleep 2

# Spusť crawler engine (pouze pokud není v app.py)
print_status "Spouštím crawler engine..."
python3 crawler_engine.py > /tmp/mini_search_crawler.log 2>&1 &
CRAWLER_PID=$!
print_success "Crawler engine spuštěn (PID: $CRAWLER_PID)"
print_status "Log: /tmp/mini_search_crawler.log"

echo ""
print_header "Systém spuštěn"
echo ""

print_success "🔧 Správcovská konzole: http://localhost:8070"
print_success "🔍 Vyhledávání:       http://localhost:8095"
echo ""

# Kontrola dostupnosti portů
print_status "Kontrola dostupnosti portů..."

if curl -s -o /dev/null -I http://localhost:8070; then
    print_success "✅ Port 8070 je dostupný"
else
    print_warning "⚠️  Port 8070 není dostupný, zkontrolujte log: /tmp/mini_search_app.log"
fi

if curl -s -o /dev/null -I http://localhost:8095; then
    print_success "✅ Port 8095 je dostupný"
else
    print_warning "⚠️  Port 8095 není dostupný, zkontrolujte log: /tmp/mini_search_ui.log"
fi

echo ""
print_success "✅ Spouštění dokončeno"
echo ""
print_status "Aktivní procesy:"
echo "------------------------------------------"
ps aux | grep -E "python3 (app|search_ui|crawler_engine)" | grep -v grep
echo "------------------------------------------"
echo ""
print_status "Pro zastavení systémů:"
print_status "  ./stop_all.sh"
echo ""
print_status "nebo"
echo ""
print_status "  kill -9 $APP_PID $UI_PID $CRAWLER_PID"
echo ""
