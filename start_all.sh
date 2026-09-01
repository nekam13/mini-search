#!/bin/bash

set -e

echo "=========================================="
echo "Mini Search - Spuštění systému"
echo "=========================================="

# Check if ports are busy and kill them if needed
for PORT in 5000 8095; do
    echo "Kontroluji port $PORT..."
    if lsof -i :$PORT > /dev/null 2>&1; then
        echo "Port $PORT je obsazený, ukončuji procesy..."
        fuser -k $PORT/tcp 2>/dev/null || pkill -f ":$PORT" 2>/dev/null || true
        sleep 2
    fi
done

echo ""
echo "=========================================="
echo "Spouštím Mini Search..."
echo "=========================================="

# Start admin console (port 5000)
echo "Spouštím správcovskou konzoli na portu 5000..."
python3 app.py > /tmp/app_5000.log 2>&1 &
APP_PID=$!
echo "✅ Správcovská konzole spuštěna (PID: $APP_PID)"

# Start search UI (port 8095)
echo "Spouštím vyhledávací rozhraní na portu 8095..."
python3 search_ui.py > /tmp/search_ui_8095.log 2>&1 &
SEARCH_PID=$!
echo "✅ Vyhledávací rozhraní spuštěno (PID: $SEARCH_PID)"

echo ""
echo "=========================================="
echo "Vše je úspěšně spuštěno!"
echo "=========================================="
echo ""
echo "🔧 Správcovská konzole: http://localhost:5000"
echo "🔍 Vyhledávání:       http://localhost:8095"
echo ""
echo "Pro zastavení systémů:"
echo "  kill $APP_PID"
echo "  kill $SEARCH_PID"
echo ""
echo "nebo spusťte: ./start_all.sh (automaticky ukončí staré procesy)"
echo ""

# Show process info
echo "Aktivní procesy:"
ps aux | grep -E "app.py|search_ui.py" | grep -v grep
