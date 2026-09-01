#!/bin/bash

# Mini Search - Stop skript v4.0
# Graceful ukončení všech služeb

# Barvy
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
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

# Úvod
echo ""
print_header "Mini Search - Ukončování systému v4.0"
echo ""

# Ukonči procesy na portech 8070 a 8095
PORTS=(8070 8095)

for port in "${PORTS[@]}"; do
    print_status "Ukončování procesů na portu $port..."
    
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
    
    print_success "Procesy na portu $port ukončeny"
done

echo ""

# Počkej na ukončení
print_status "Čekání na ukončení všech procesů..."
sleep 2

# Zkontroluj, zda jsou procesy opravdu ukončeny
if pgrep -f "python3 (app|search_ui|crawler_engine)" &> /dev/null; then
    print_warning "Některé procesy stále běží, zkouším znovu..."
    pkill -9 -f python3 2>/dev/null || true
    sleep 1
fi

echo ""
print_header "Systém ukončen"
echo ""
print_success "✅ Všechny služby byly úspěšně ukončeny"
echo ""
