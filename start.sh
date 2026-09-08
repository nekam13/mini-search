#!/bin/bash

# Mini Search - Start skript v7.0

set -e

echo "=========================================="
echo "Mini Search - Spousteni v7.0"
echo "=========================================="
echo ""

# Kontrola setupu
if [ ! -f ".setup_done" ]; then
    echo "[!] Setup nebyl proveden, spoustim setup.sh..."
    ./setup.sh
    exit 0
fi

echo ""

# Ukonceni starych procesu
if [ -f "app.pid" ]; then
    echo "[*] Ukoncuji stary proces..."
    kill $(cat app.pid) 2>/dev/null || true
    rm -f app.pid
fi

fuser -k 8070/tcp 2>/dev/null || true
sleep 1

# Vytvoreni adresaru pro logy
mkdir -p logs

# Aktivace virtualniho prostredi a spusteni
if [ -d "venv" ]; then
    echo "[*] Aktivuji virtualni prostredi..."
    source venv/bin/activate
fi

echo "[*] Spoustim Mini Search..."
nohup python3 app_combined.py > logs/app.log 2>&1 &
echo $! > app.pid

echo ""
echo "Mini Search spusten na http://localhost:8070"
echo "   PID: $(cat app.pid)"
echo "   Logy: tail -f logs/app.log"
echo ""
echo "Pro zastaveni: ./stop.sh"
echo "Pro aktualizaci: ./update.sh"
