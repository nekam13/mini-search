#!/bin/bash

# Mini Search - Stop skript v5.0

echo "=========================================="
echo "Mini Search - Ukončování systému"
echo "=========================================="
echo ""

pkill -f "python3 app_combined.py" 2>/dev/null || true
pkill -f "python3 app.py" 2>/dev/null || true
pkill -f "python3 search_ui.py" 2>/dev/null || true
pkill -f "python3 crawler_engine.py" 2>/dev/null || true

sleep 1

if pgrep -f "python3.*app" &> /dev/null; then
    pkill -9 -f python3 2>/dev/null || true
    sleep 1
fi

echo "✅ Systém ukončen"
