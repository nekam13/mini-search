#!/bin/bash

# Mini Search - Setup skript v6.0
# Jednorazova inicializace

set -e

echo "=========================================="
echo "Mini Search - Setup v6.0"
echo "=========================================="
echo ""

# Kontrola Pythonu
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
if [ -z "$PYTHON_VERSION" ]; then
    echo "❌ Python 3 není nainstalován!"
    exit 1
fi

MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ $MAJOR -lt 3 ] || [ $MINOR -lt 10 ]; then
    echo "❌ Vyžaduje se Python 3.10 nebo novější! (nalezne: $PYTHON_VERSION)"
    exit 1
fi

echo "✅ Python $PYTHON_VERSION"

# Instalace systemovych zavislosti
if command -v apt-get &> /dev/null; then
    echo "[*] Instalace systémových závislostí..."
    sudo apt-get install -y python3-pip lsof curl > /dev/null 2>&1
    echo "✅ Systémové závislosti nainstalovány"
fi

echo ""

# Instalace Python balicku
echo "[*] Instalace Python balíčků..."
pip install --quiet -r requirements.txt

echo "✅ Základní balíčky nainstalovány"

echo ""
echo "[*] Instalace PyTorch pro CPU..."
pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu

echo "✅ PyTorch nainstalován"

echo ""
echo "[*] Instalace sentence-transformers..."
pip install --quiet sentence-transformers

echo "✅ Sentence-transformers nainstalován"

echo ""
echo "[*] Stahování modelu..."
python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')" 2>&1 | grep -v "Downloading" | grep -v "Extracting" | grep -v "Installing" || true

echo "✅ Model stažen"

echo ""
echo "[*] Inicializace databáze..."
python3 -c "import app_combined; app_combined.init_db()"

echo "✅ Databáze inicializována"

echo ""
echo "[*] Vytváření adresářů..."
mkdir -p logs

echo "✅ Setup dokončen"

# Vytvoř marker
touch .setup_done

echo ""
echo "=========================================="
echo "✅ Setup dokončen!"
echo "=========================================="
echo ""
echo "Pro spuštění: ./start.sh"
echo "Pro zastavení: ./stop.sh"
echo ""
echo "Přístup: http://localhost:8070"
