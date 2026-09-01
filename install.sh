#!/bin/bash

# Mini Search - Instalační skript v4.0
# Kompletne zjednodušený pro Ubuntu 26.04 ARM64 + Termux
# Vše v jednom kroku, bez těžkých kompilací

set -e

# Barvy
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

print_header() {
    echo -e "${BLUE}==========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}==========================================${NC}"
}

print_status() {
    echo -e "${CYAN}[*]${NC} $1"
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

# Úvod
clear
echo ""
print_header "Mini Search - Instalace v4.0"
echo ""
print_warning "Prostředí: Ubuntu 26.04 ARM64 + Termux"
print_warning "Zjednodušená instalace - vše v jednom kroku"
echo ""

# Kontrola Pythonu
print_status "Kontrola Pythonu..."
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 není nainstalován!"
    print_error "Nainstalujte: pkg install python (v Termux) nebo apt install python3 (v Ubuntu)"
    exit 1
fi

PYTHON_VER=$(python3 --version 2>&1)
print_success "Nalezen: $PYTHON_VER"

# Kontrola pip
print_status "Kontrola pip..."
if ! python3 -m pip --version &> /dev/null; then
    print_status "Instalace pip..."
    if command -v apt &> /dev/null; then
        sudo apt update && sudo apt install -y python3-pip
    elif command -v pkg &> /dev/null; then
        pkg install python-pip
    else
        curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
        python3 get-pip.py
        rm get-pip.py
    fi
fi
print_success "pip je dostupný"

# Vytvoření virtuálního prostředí
print_status "Vytvářím virtuální prostředí..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    print_success "Virtuální prostředí vytvořeno"
else
    print_warning "Virtuální prostředí již existuje"
fi

# Aktivace
print_status "Aktivace virtuálního prostředí..."
source venv/bin/activate

# Aktualizace pip
print_status "Aktualizace pip..."
pip install --upgrade pip setuptools wheel

# Instalace základních balíčků
print_header "Instalace Python balíčků"
echo ""

print_status "Instalace lehkých závislostí..."
pip install flask==3.0.3 apscheduler==3.10.4 beautifulsoup4==4.12.2 lxml==5.2.2 feedparser==6.0.10 
print_success "Základní balíčky nainstalovány"

# Instalace PyTorch pro ARM64
print_status "Instalace PyTorch pro ARM64..."
print_warning "Toto může trvat několik minut..."
pip install torch --index-url https://download.pytorch.org/whl/cpu
print_success "PyTorch nainstalován"

# Instalace sentence-transformers
print_status "Instalace sentence-transformers..."
pip install sentence-transformers==2.2.2
print_success "Sentence-transformers nainstalován"

# Pokus o ChromaDB
print_status "Instalace ChromaDB..."
if pip install "chromadb>=0.5.0" 2>/dev/null; then
    print_success "ChromaDB nainstalován"
else
    print_warning "ChromaDB se nepodařilo nainstalovat, použiji SQLite fallback"
fi

# Inicializace databáze
print_header "Inicializace databáze"
echo ""

python3 -c "
import os
os.makedirs('data', exist_ok=True)

# Import a inicializace
import crawler_engine
crawler_engine.init_db()
print('✅ Databáze console.db úspěšně inicializována')
print('✅ Indexy vytvořeny pro optimalizaci dotazů')
"

# Inicializace ChromaDB
print_status "Inicializace ChromaDB..."
python3 -c "
import os
os.makedirs('chroma_db', exist_ok=True)
print('✅ Adresář chroma_db vytvořen')
"

# Vytvoření .gitignore
print_status "Vytvářím .gitignore..."
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

# Virtual environment
venv/
ENV/
env/

# IDE
.idea/
.vscode/

# Database
console.db
console.db-wal
console.db-shm
chroma_db/

# Logs
*.log
/tmp/*.log

# Models
models/

# OS files
.DS_Store
Thumbs.db

# Installation marker
.install_done
EOF
print_success ".gitignore vytvořen"

# Nastavení práv pro skripty
print_status "Nastavuji práva pro skripty..."
chmod +x start_all.sh stop_all.sh
print_success "Skripty jsou spustitelné"

# Označení instalace jako dokončené
touch .install_done

# Závěr
print_header "Instalace dokončena!"
echo ""
print_success "Vše je připraveno k použití"
echo ""
echo "=========================================="
echo "Návod k použití:"
echo "=========================================="
echo ""
print_status "Pro spuštění systému:"
echo "  source venv/bin/activate"
echo "  ./start_all.sh"
echo ""
print_status "Pro zastavení systému:"
echo "  ./stop_all.sh"
echo ""
print_status "Přímé spuštění:"
echo "  python3 app.py           # Správcovská konzole (port 8070)"
echo "  python3 search_ui.py     # Vyhledávání (port 8095)"
echo "  python3 crawler_engine.py # Crawler engine"
echo ""
print_status "Důležité URL:"
echo "  http://localhost:8070   # Správcovská konzole"
echo "  http://localhost:8095   # Vyhledávání"
echo ""
echo "=========================================="
