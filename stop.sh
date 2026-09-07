#!/bin/bash

# Mini Search - Stop skript v6.0

echo "=========================================="
echo "Mini Search - Ukončování"
echo "=========================================="
echo ""

# Ukonči podle PID
if [ -f "app.pid" ]; then
    echo "[*] Ukončování procesu $(cat app.pid)..."
    kill $(cat app.pid) 2>/dev/null || true
    rm -f app.pid
fi

# Ukonči podle portu
fuser -k 8070/tcp 2>/dev/null || true

sleep 1

if pgrep -f "python3 app_combined.py" &> /dev/null; then
    pkill -9 -f "python3 app_combined.py" 2>/dev/null || true
fi

echo ""
echo "✅ Mini Search zastaven. Index zachován."
