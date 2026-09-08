#!/bin/bash

# Mini Search - Update skript v7.0
# Bezpecna aktualizace existujici instalace

set -e

echo "=========================================="
echo "Mini Search - Aktualizace v7.0"
echo "=========================================="
echo ""

# Kontrola, ze jsme ve spravnem adresari
if [ ! -f "app_combined.py" ]; then
    echo "Chyba: Neni v adresari Mini Search!"
    exit 1
fi

# Ulozit aktualni PID pro mozne obnoveni
if [ -f "app.pid" ]; then
    echo "[*] Uklidam stary proces..."
    ./stop.sh
    sleep 2
fi

echo ""

# Zalohovat soucasny stav
BACKUP_DIR="backup_$(date +%Y%m%d_%H%M%S)"
echo "[*] Vytvarim zalohu do $BACKUP_DIR..."
mkdir -p "$BACKUP_DIR"
cp -r console.db logs data "$BACKUP_DIR/" 2>/dev/null || true
echo "Zaloha vytvorena"

echo ""

# Aktualizovat z GitHub
GIT_URL="https://github.com/nekam13/mini-search.git"
BRANCH="beta-optimized"

echo "[*] Aktualizuji z $GIT_URL ($BRANCH)..."
git fetch origin $BRANCH 2>/dev/null || true
git checkout $BRANCH 2>/dev/null || true
git pull origin $BRANCH

echo "Kod aktualizovan"

echo ""

# Instalovat nove zavislosti
if [ -d "venv" ]; then
    echo "[*] Instaluji nove zavislosti..."
    source venv/bin/activate
    pip install --quiet --upgrade -r requirements.txt 2>/dev/null || true
    echo "Zavislosti aktualizovany"
fi

echo ""

# Obnovit databazi (migrace)
echo "[*] Provadim migraci databaze..."
python3 -c "import app_combined; app_combined.get_db()" 2>&1 | grep -v "Downloading" || true
echo "Migrace dokoncena"

echo ""

# Ulozit commit SHA
echo "[*] Ukladam commit SHA..."
GIT_SHA=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
echo "$GIT_SHA" > .commit_sha
echo "0" > .update_available

echo ""
echo "=========================================="
echo "Aktualizace dokoncena!"
echo "=========================================="
echo ""
echo "Aktualni commit: $GIT_SHA"
echo ""
echo "Pro spusteni aktualizovane verze: ./start.sh"
echo ""
echo "Dulezite: Pred spustenim zkontrolujte, ze vse probehlo v poradku."
echo "         Pokud nastal problem, obnoveni: cp -r $BACKUP_DIR/* ."
