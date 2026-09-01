#!/bin/bash

# Mini Search - Startup skript v5.0
# Spouští vše v jednom procesu

echo "=========================================="
echo "Mini Search - Spouštění systému v5.0"
echo "=========================================="
echo ""
echo "[*] Spouštím kombinovanou aplikaci..."
echo ""

# Zastav všechny existující procesy
pkill -f "python3 app_combined.py" 2>/dev/null || true
pkill -f "python3 app.py" 2>/dev/null || true
pkill -f "python3 search_ui.py" 2>/dev/null || true
pkill -f "python3 crawler_engine.py" 2>/dev/null || true

sleep 1

# Spusť kombinovanou aplikaci
python3 app_combined.py > /tmp/mini_search.log 2>&1 &

echo "✅ Mini Search spuštěn!"
echo ""
echo "🔧 Správcovská konzole + Vyhledávání: http://localhost:8070"
echo ""
echo "Log: /tmp/mini_search.log"
echo ""
echo "Pro zastavení: ./stop_all.sh"
