#!/bin/bash

echo "🧹 Ukončuji případné staré procesy..."
pkill -f meilisearch
pkill -f app.py
pkill -f search_ui.py
sleep 1

echo "🚀 Spouštím Meilisearch..."
./meilisearch &
sleep 2

echo "🌐 Spouštím správcovskou konzoli (port 5000)..."
python3 app.py &

echo "🔍 Spouštím vyhledávací rozhraní (port 8095)..."
python3 search_ui.py &

echo "✨ Vše je úspěšně spuštěno!"
echo "-> Konzole: http://localhost:5000"
echo "-> Vyhledávač: http://localhost:8095"
