#!/bin/bash

set -e

echo "=========================================="
echo "Mini Search - Instalační skript"
echo "=========================================="

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+\.\d+' | head -1)
echo "Zjišťuji verzi Pythonu: $PYTHON_VERSION"

if python3 -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)"; then
    echo "✅ Python 3.10+ nalezne"
else
    echo "❌ Vyžaduje se Python 3.10 nebo novější!"
    echo "Nainstalujte Python 3.10+ a spusťte instalaci znovu."
    exit 1
fi

# Check if already installed
if [ -f ".install_done" ]; then
    echo "✅ Instalace již byla provedena. Pokračuji..."
else
    echo ""
    echo "=========================================="
    echo "Instalace závislostí..."
    echo "=========================================="
    
    # Install pip dependencies
    echo "Instalace Python balíčků..."
    pip install -r requirements.txt --no-cache-dir
    
    echo ""
    echo "=========================================="
    echo "Stahování modelu sentence-transformers..."
    echo "=========================================="
    
    # Download sentence-transformers model
    python3 -c "
from sentence_transformers import SentenceTransformer
import os

model_path = 'models/paraphrase-multilingual-MiniLM-L12-v2'
os.makedirs('models', exist_ok=True)

print('Stahuji model paraphrase-multilingual-MiniLM-L12-v2...')
model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2', cache_folder='models')
print('✅ Model úspěšně stažen a uložen do: models/')
"
    
    echo ""
    echo "=========================================="
    echo "Inicializace databáze..."
    echo "=========================================="
    
    # Initialize SQLite database
    python3 -c "
import sqlite3
import os

os.makedirs('data', exist_ok=True)

conn = sqlite3.connect('console.db')
cursor = conn.cursor()

# Create tables
cursor.execute('''
    CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_url TEXT UNIQUE,
        aliases TEXT DEFAULT '[]',
        status TEXT DEFAULT 'active',
        error_count INTEGER DEFAULT 0,
        last_crawled INTEGER DEFAULT 0,
        max_pages INTEGER DEFAULT 500
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS crawl_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,
        url TEXT,
        status TEXT DEFAULT 'pending',
        locked_by TEXT DEFAULT '',
        error_reason TEXT DEFAULT '',
        retry_count INTEGER DEFAULT 0,
        FOREIGN KEY (site_id) REFERENCES sites(id)
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS sitemaps_feeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,
        url TEXT,
        type TEXT DEFAULT 'sitemap',
        last_checked INTEGER DEFAULT 0,
        FOREIGN KEY (site_id) REFERENCES sites(id)
    )
''')

conn.commit()
conn.close()
print('✅ Databáze console.db úspěšně inicializována')
"
    
    # Initialize ChromaDB directory
    echo ""
    echo "=========================================="
    echo "Inicializace ChromaDB..."
    echo "=========================================="
    
    python3 -c "
import os
os.makedirs('chroma_db', exist_ok=True)
print('✅ Adresář chroma_db vytvořen')
"
    
    echo ""
    echo "=========================================="
    echo "Instalace dokončena!"
    echo "=========================================="
    
    # Mark as done
    touch .install_done
    echo "✅ Vytvořen soubor .install_done - instalace je idempotentní"
fi

echo ""
echo "Pro spuštění systému použijte: ./start_all.sh"
echo ""
