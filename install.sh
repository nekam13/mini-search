#!/bin/bash

# Mini Search - Installation Script
# Optimized for Ubuntu 26.04 ARM64 + Termux environment
# Version: 2.0

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

# Function to print colored messages
print_header() {
    echo -e "${MAGENTA}==========================================${NC}"
    echo -e "${MAGENTA}$1${NC}"
    echo -e "${MAGENTA}==========================================${NC}"
}

print_status() {
    echo -e "${BLUE}[*]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[+]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

print_error() {
    echo -e "${RED}[-]${NC} $1"
}

print_info() {
    echo -e "${CYAN}[i]${NC} $1"
}

# Main installation
echo ""
print_header "Mini Search - Instalační skript v2.0"
echo ""
print_info "Prostředí: Ubuntu 26.04 ARM64 + Termux"
echo ""

# Check if running as root
if [ "$(id -u)" -eq 0 ]; then
    print_warning "Spouštíte jako root - doporučeno pro systémovou instalaci"
else
    print_info "Spouštíte jako běžný uživatel"
fi

# Check Python version
print_status "Kontrola verze Pythonu..."
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+\.\d+' | head -1)

if python3 -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)"; then
    print_success "Python $PYTHON_VERSION nalezne"
else
    print_error "Vyžaduje se Python 3.10 nebo novější!"
    print_error "Nainstalujte: sudo apt update && sudo apt install -y python3.10 python3.10-venv python3.10-dev"
    exit 1
fi

# Check for pip
if ! command -v pip3 &> /dev/null; then
    print_status "Instalace pip..."
    sudo apt update && sudo apt install -y python3-pip
    print_success "pip nainstalován"
fi

# Check if already installed
if [ -f ".install_done" ]; then
    print_success "Instalace již byla provedena"
    print_status "Kontrola závislostí..."
    
    # Verify all packages are installed
    MISSING=0
    while IFS= read -r line; do
        # Skip comments and empty lines
        [[ "$line" =~ ^#.*$ ]] && continue
        [[ -z "$line" ]] && continue
        
        # Extract package name
        PKG=$(echo "$line" | cut -d'=' -f1 | tr -d '[:space:]')
        [[ -z "$PKG" ]] && continue
        
        if ! python3 -c "import $PKG" 2>/dev/null; then
            print_warning "Chybí balíček: $PKG"
            MISSING=1
        fi
    done < requirements.txt
    
    if [ $MISSING -eq 0 ]; then
        print_success "Všechny závislosti jsou nainstalovány"
        echo ""
        print_header "Instalace dokončena"
        print_info "Pro spuštění použijte: ./start_all.sh"
        print_info "Pro zastavení použijte: ./stop_all.sh"
        exit 0
    else
        print_warning "Některé závislosti chybí, pokračuji s instalací..."
    fi
fi

echo ""
print_header "Instalace systémových závislostí"
echo ""

# Install system dependencies
print_status "Aktualizace balíčků..."
sudo apt update

print_status "Instalace build nástrojů a knihoven..."
sudo apt install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    pkg-config \
    git \
    curl \
    wget

print_success "Systémové závislosti nainstalovány"

echo ""
print_header "Vytváření virtuálního prostředí"
echo ""

# Create virtual environment
if [ ! -d "venv" ]; then
    print_status "Vytvářím virtuální prostředí..."
    python3 -m venv venv
    print_success "Virtuální prostředí vytvořeno"
else
    print_info "Virtuální prostředí již existuje"
fi

# Activate virtual environment
print_status "Aktivace virtuálního prostředí..."
source venv/bin/activate

# Upgrade pip
print_status "Aktualizace pip..."
pip install --upgrade pip setuptools wheel

print_success "pip aktualizován"

echo ""
print_header "Instalace Python balíčků"
echo ""

# Install requirements
print_status "Instalace závislostí z requirements.txt..."
pip install --no-cache-dir -r requirements.txt

print_success "Python balíčky nainstalovány"

echo ""
print_header "Stahování modelu sentence-transformers"
echo ""

print_status "Stahuji model paraphrase-multilingual-MiniLM-L12-v2..."
print_info "Toto může trvat několik minut podle rychlosti připojení..."

python3 -c "
import os
import sys
from sentence_transformers import SentenceTransformer

model_path = 'models/paraphrase-multilingual-MiniLM-L12-v2'
os.makedirs('models', exist_ok=True)

try:
    model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2', cache_folder='models')
    print('✅ Model úspěšně stažen a uložen do: models/')
    
    # Test model
    test_embedding = model.encode(['Test sentence'])
    print(f'✅ Model testován: {test_embedding.shape}')
except Exception as e:
    print(f'❌ Chyba při stahování modelu: {e}')
    sys.exit(1)
"

echo ""
print_header "Inicializace databáze"
echo ""

# Initialize SQLite database
python3 -c "
import sqlite3
import os

# Create data directory
os.makedirs('data', exist_ok=True)

conn = sqlite3.connect('console.db')
cursor = conn.cursor()

# Create tables with proper indexes
cursor.execute('''
    CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_url TEXT UNIQUE,
        aliases TEXT DEFAULT '[]',
        status TEXT DEFAULT 'active',
        error_count INTEGER DEFAULT 0,
        last_crawled INTEGER DEFAULT 0,
        max_pages INTEGER DEFAULT 500,
        created_at INTEGER DEFAULT strftime('%s', 'now')
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
        created_at INTEGER DEFAULT strftime('%s', 'now'),
        FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS sitemaps_feeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,
        url TEXT,
        type TEXT DEFAULT 'sitemap',
        last_checked INTEGER DEFAULT 0,
        FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
    )
''')

# Create indexes for better performance
cursor.execute('CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_sites_last_crawled ON sites(last_crawled)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_sites_created_at ON sites(created_at)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_site_id ON crawl_queue(site_id)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_created_at ON crawl_queue(created_at)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_feeds_site_id ON sitemaps_feeds(site_id)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_feeds_type ON sitemaps_feeds(type)')

conn.commit()
conn.close()
print('✅ Databáze console.db úspěšně inicializována')
print('✅ Indexy vytvořeny pro optimalizaci dotazů')
"

print_success "Databáze inicializována"

echo ""
print_header "Inicializace ChromaDB"
echo ""

# Initialize ChromaDB directory
python3 -c "
import os
os.makedirs('chroma_db', exist_ok=True)
print('✅ Adresář chroma_db vytvořen')
"

# Test ChromaDB
print_status "Testování ChromaDB..."
python3 -c "
import chromadb
from chromadb.config import Settings
import os

os.makedirs('chroma_db', exist_ok=True)

try:
    client = chromadb.Client(
        Settings(
            chroma_db_impl='duckdb+parquet',
            persist_directory='chroma_db'
        )
    )
    collection = client.get_or_create_collection(name='pages')
    print('✅ ChromaDB úspěšně inicializována')
except Exception as e:
    print(f'❌ Chyba ChromaDB: {e}')
"

print_success "ChromaDB připravena"

echo ""
print_header "Vytváření .gitignore"
echo ""

# Create .gitignore if it doesn't exist
if [ ! -f ".gitignore" ]; then
    cat > .gitignore << 'EOF'
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual environment
venv/
ENV/
env/

# IDE
.idea/
.vscode/
*.swp
*.swo
*~

# Database and ChromaDB
console.db
console.db-wal
console.db-shm
chroma_db/
models/

# Logs
*.log
/tmp/*.log

# OS files
.DS_Store
Thumbs.db

# Installation marker
.install_done

# Python cache
.pytest_cache/
.coverage
htmlcov/

# Environment files
.env
.env.local
.env.*.local
EOF
    print_success ".gitignore vytvořen"
else
    print_info ".gitignore již existuje"
fi

echo ""
print_header "Vytváření start/stop skriptů"
echo ""

# Make scripts executable
chmod +x start_all.sh stop_all.sh
print_success "Skripty jsou spustitelné"

echo ""
print_header "Instalace dokončena!"
echo ""

# Mark as done
touch .install_done
print_success "Vytvořen soubor .install_done"

echo ""
echo "=========================================="
print_header "Návod k použití"
echo "=========================================="
echo ""

print_info "Pro spuštění systému:"
print_status "  source venv/bin/activate  # Aktivace virtuálního prostředí"
print_status "  ./start_all.sh           # Spuštění všech služeb"
echo ""

print_info "Pro zastavení systémů:"
print_status "  ./stop_all.sh            # Ukončení všech služeb"
echo ""

print_info "Příkazy pro ruční spuštění:"
print_status "  python3 app.py           # Správcovská konzole (port 5000)"
print_status "  python3 search_ui.py     # Vyhledávání (port 8095)"
print_status "  python3 crawler_engine.py # Crawler engine"
echo ""

print_info "Důležité URL:"
print_status "  http://localhost:5000   # Správcovská konzole"
print_status "  http://localhost:8095   # Vyhledávání"
echo ""

print_info "Log soubory:"
print_status "  /tmp/mini_search_app.log   # Admin konzole"
print_status "  /tmp/mini_search_ui.log   # Vyhledávání"
echo ""

# Show system info
print_header "Systémové informace"
echo ""
print_info "Python: $(python3 --version 2>&1)"
print_info "Pip: $(pip --version 2>&1)"
print_info "Systém: $(uname -a)"
echo ""

# Deactivate virtual environment
# deactivate
