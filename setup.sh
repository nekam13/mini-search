#!/bin/bash

# Mini Search - Setup skript v7.0
# Jednorazova inicializace pro novou instalaci

set -e

echo "=========================================="
echo "Mini Search - Setup v7.0"
echo "=========================================="
echo ""

# Kontrola Pythonu
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
if [ -z "$PYTHON_VERSION" ]; then
    echo "Python 3 nenalezen!"
    exit 1
fi

MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ $MAJOR -lt 3 ] || [ $MINOR -lt 10 ]; then
    echo "Vyuzaduje se Python 3.10 nebo novesi! (nalezne: $PYTHON_VERSION)"
    exit 1
fi

echo "Python $PYTHON_VERSION"

# Instalace systemovych zavislosti
if command -v apt-get &> /dev/null; then
    echo "[*] Instalace systemovych zavislosti..."
    sudo apt-get install -y python3-pip python3-venv lsof curl git build-essential cmake pkg-config libxml2-dev libxslt1-dev > /dev/null 2>&1
    echo "Systemove zavislosti nainstalovany"
fi

echo ""

# Vytvoreni virtualniho prostredi
if [ ! -d "venv" ]; then
    echo "[*] Vytvarim virtualni prostredi..."
    python3 -m venv venv
fi

# Aktivace virtualniho prostredi
source venv/bin/activate

echo ""

# Instalace Python balicku
echo "[*] Instalace Python balicku..."
pip install --quiet --upgrade pip setuptools wheel
pip install --quiet -r requirements.txt

echo "Zakladni balicky nainstalovany"

echo ""
echo "[*] Instalace PyTorch pro CPU..."
pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu

echo "PyTorch nainstalovan"

echo ""
echo "[*] Instalace sentence-transformers..."
pip install --quiet sentence-transformers

echo "Sentence-transformers nainstalovan"

echo ""
echo "[*] Stahovani modelu..."
python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')" 2>&1 | grep -v "Downloading" | grep -v "Extracting" | grep -v "Installing" || true

echo "Model stazen"

echo ""
echo "[*] Inicializace databaze..."
python3 -c "import app_combined; app_combined.init_db()"

echo "Databaze inicializovana"

echo ""
echo "[*] Vytvareni adresaru..."
mkdir -p logs
mkdir -p data

echo "Adresare vytvoreny"

# Vytvor marker
touch .setup_done

echo ""
echo "=========================================="
echo "Setup dokoncen!"
echo "=========================================="
echo ""
echo "Pro spusteni: ./start.sh"
echo "Pro zastaveni: ./stop.sh"
echo "Pro aktualizaci: ./update.sh"
echo ""
echo "Pristup: http://localhost:8070"
echo ""
echo "Dulezite: Pro bezpecnou aktualizaci pouzivejte ./update.sh"
echo "         Nebo spravujte aktualizace pres admin rozhrani."
