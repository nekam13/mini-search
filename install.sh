#!/bin/bash

# Mini Search - Instalační skript v5.0
# Kompletne zjednoduseny - vse v jednom kroku

set -e

echo "=========================================="
echo "Mini Search - Instalace v5.0"
echo "=========================================="
echo ""

# Instalace pip balicku
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install flask==3.0.3 beautifulsoup4==4.12.2 lxml==5.2.2 feedparser==6.0.10

echo "✅ Zakladni balicky nainstalovany"
echo ""

# Inicializace databaze
python3 -c "
import os
os.makedirs('data', exist_ok=True)

import sqlite3
conn = sqlite3.connect('console.db')
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_url TEXT UNIQUE,
        aliases TEXT DEFAULT '[]',
        status TEXT DEFAULT 'active',
        error_count INTEGER DEFAULT 0,
        last_crawled INTEGER DEFAULT 0,
        max_pages INTEGER DEFAULT 500,
        created_at INTEGER DEFAULT 0
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
        created_at INTEGER DEFAULT 0,
        FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,
        url TEXT,
        title TEXT DEFAULT '',
        content TEXT DEFAULT '',
        metadata TEXT DEFAULT '{}',
        indexed_at INTEGER DEFAULT 0,
        FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
    )
''')

cursor.execute('CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_site ON crawl_queue(site_id)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_pages_content ON pages(content)')

conn.commit()
conn.close()
print('✅ Databaze inicializovana')
"

echo ""
echo "=========================================="
echo "Instalace dokoncena!"
echo "=========================================="
echo ""
echo "Pro spusteni: ./start_all.sh"
echo "Pro zastaveni: ./stop_all.sh"
echo ""
echo "Pristup: http://localhost:8070"
