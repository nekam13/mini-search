#!/bin/bash

# Mini Search - Start skript v6.0

set -e

echo "=========================================="
echo "Mini Search - Spouštění v6.0"
echo "=========================================="
echo ""

# Kontrola setupu
if [ ! -f ".setup_done" ]; then
    echo "[!] Setup nebyl proveden, spouštím setup.sh..."
    ./setup.sh
fi

echo ""

# Ukonči staré procesy
if [ -f "app.pid" ]; then
    echo "[*] Ukončování starého procesu..."
    kill $(cat app.pid) 2>/dev/null || true
    rm -f app.pid
fi

fuser -k 8070/tcp 2>/dev/null || true
sleep 1

# Vytvoř logs adresář
mkdir -p logs

# Spusť aplikaci
echo "[*] Spouštím Mini Search..."
nohup python3 app_combined.py > logs/app.log 2>&1 &
echo $! > app.pid

echo ""
echo "✅ Mini Search spuštěn na http://localhost:8070"
echo "   PID: $(cat app.pid)"
echo "   Logy: tail -f logs/app.log"
echo ""
echo "Pro zastavení: ./stop.sh"
