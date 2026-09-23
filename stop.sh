#!/bin/bash

# Mini Search - Stop skript v7.0

echo "=========================================="
echo "Mini Search - Ukonceni v7.0"
echo "=========================================="
echo ""

# Ukonceni podle PID
if [ -f "app.pid" ]; then
    echo "[*] Ukoncuji proces $(cat app.pid)..."
    kill $(cat app.pid) 2>/dev/null || true
    rm -f app.pid
fi

# Ukonceni podle portu
fuser -k 8070/tcp 2>/dev/null || true

sleep 1

if pgrep -f "python3 app_combined.py" &> /dev/null; then
    pkill -9 -f "python3 app_combined.py" 2>/dev/null || true
fi

echo ""
echo "Mini Search zastaven. Index zachovan."
