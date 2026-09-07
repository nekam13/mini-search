# Mini Search

Lehký osobní vyhledávač pro indexování vybraných webů. Aplikace crawluje stránky, ukládá jejich metadata a text do SQLite a používá vektorové vyhledávání pro sémanticky podobné výsledky.

> Aktuální vývojová větev: `beta-optimized` (v6.1).

## Funkce

- Indexace vybraných webů ze sitemap, RSS/Atom feedů nebo odkazů z úvodní stránky
- Respektování pravidel `robots.txt` a případného `Crawl-delay`
- Vektorové vyhledávání pomocí SentenceTransformers a hnswlib
- Extrakce titulků, Open Graph metadat, textu, obrázků, audio odkazů a JSON-LD schema.org dat
- Filtry pro články, podcasty, audio a stránky s cenami
- Autocomplete nad názvy indexovaných stránek
- Správcovské rozhraní pro přidání webu, recrawl, smazání a kontrolu statistik
- Pravidelné kontroly RSS feedů, sitemap a opakované procházení aktivních webů
- Ošetření HTTP 429 (`Retry-After`) a HTTP 403 (označení webu jako blokovaného)
- Deduplikace velmi podobných výsledků

## Technologie

- Python 3
- Flask
- SQLite (WAL režim)
- hnswlib
- SentenceTransformers
- model `paraphrase-multilingual-MiniLM-L12-v2`
- Beautiful Soup, lxml, extruct a feedparser
- APScheduler

## Spuštění v Ubuntu přes Termux

Pro náročnější knihovny, jako jsou PyTorch, NumPy nebo hnswlib, je doporučeno spouštět projekt v Ubuntu přes `proot-distro`, ne přímo v čistém Termuxu.

### 1. Spusť Ubuntu

V Termuxu:

```bash
proot-distro login ubuntu
apt update && apt upgrade -y
```

### 2. Nainstaluj systémové závislosti

```bash
apt install -y \
  python3 python3-pip python3-venv git \
  build-essential cmake pkg-config \
  libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev
```

### 3. Naklonuj repozitář

```bash
cd ~
git clone https://github.com/nekam13/mini-search.git
cd mini-search
git checkout beta-optimized
```

Pokud už repozitář máš naklonovaný:

```bash
cd ~/mini-search
git fetch origin
git checkout beta-optimized
git pull origin beta-optimized
```

### 4. Vytvoř virtuální prostředí

```bash
python3 -m venv venv
source venv/bin/activate
```

Po aktivaci se na začátku příkazového řádku zobrazí `(venv)`.

### 5. Nainstaluj Python závislosti

```bash
python -m pip install --upgrade pip setuptools wheel

pip install --no-cache-dir \
  flask apscheduler requests beautifulsoup4 lxml \
  feedparser extruct numpy hnswlib

pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

pip install --no-cache-dir sentence-transformers==2.2.2
```

### 6. Stáhni jazykový model

Tento krok je potřeba pouze při prvním spuštění. Model se uloží do cache uživatele v Ubuntu.

```bash
python3 - <<'EOF'
from sentence_transformers import SentenceTransformer

print("Stahuji model...")
SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
print("Model byl uspesne stazen.")
EOF
```

### 7. Spusť aplikaci

```bash
python3 app_combined.py
```

Aplikace poběží na adrese:

- Vyhledávání: `http://127.0.0.1:8070/`
- Správa indexu: `http://127.0.0.1:8070/admin`

## Běžné spuštění později

Po otevření Termuxu spusť:

```bash
proot-distro login ubuntu
cd ~/mini-search
source venv/bin/activate
python3 app_combined.py
```

## Použití

1. Otevři `http://127.0.0.1:8070/admin`.
2. Do formuláře vlož URL webu a nastav maximální počet stránek.
3. Aplikace najde sitemapu nebo feed; pokud je nenajde, pokusí se získat odkazy z domovské stránky.
4. Workery postupně stahují povolené stránky a ukládají je do lokálního indexu.
5. Vyhledávej na `http://127.0.0.1:8070/` přirozeným jazykem, česky i dalšími jazyky podporovanými použitým modelem.

## Filtry ve vyhledávání

| Filtr | Zobrazí |
|---|---|
| Vše | Všechny relevantní indexované stránky |
| Články | `Article`, `BlogPosting` a `NewsArticle` z JSON-LD |
| Podcasty | `PodcastEpisode` z JSON-LD |
| Audio | Stránky s nalezeným audio souborem nebo HTML `<audio>` |
| Ceny | Stránky, jejichž schema.org data obsahují cenu |

## Data a úložiště

Aplikace vytváří lokální SQLite databázi v adresáři `data/`. Obsahuje zejména:

- seznam sledovaných webů,
- frontu URL pro crawling,
- uložené stránky a metadata,
- textový obsah,
- embeddingy pro vektorové vyhledávání,
- objevené sitemapy a feedy.

HNSW index je při běhu v paměti. Při restartu se znovu sestaví z embeddingů uložených v SQLite.

## Řešení problémů

### `Package ... has no installation candidate`

Ověř, že jsi uvnitř Ubuntu (`proot-distro login ubuntu`) a že máš aktualizované seznamy balíčků:

```bash
apt update
apt install -y libxml2-dev libxslt1-dev python3-venv python3-pip
```

### PyTorch se nevejde do paměti

Používej instalaci bez cache:

```bash
pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
```

Před instalací také zavři jiné náročné aplikace a ověř dostupné místo:

```bash
df -h
```

### `hnswlib` nejde nainstalovat

Doinstaluj překladové nástroje a zkus instalaci znovu:

```bash
apt install -y build-essential cmake python3-dev
pip install --no-cache-dir hnswlib
```

### Aplikace se nespustí kvůli portu

Zkontroluj, zda už neběží jiná instance aplikace. Případně ji ukonči pomocí `Ctrl+C` v původním terminálu.

## Poznámky k provozu

- Crawluj pouze weby, u kterých je takové použití přiměřené a povolené.
- Aplikace kontroluje `robots.txt`, ale provozovatel webu může přístup omezit i jinak.
- U větších webů začni s nízkou hodnotou `max_pages`, například 50 až 200.
- Model i PyTorch mohou na telefonu zabrat výrazné množství úložiště a RAM.

## Licence

Projekt zatím nemá definovanou licenci. Před veřejným sdílením nebo použitím cizími lidmi doplň soubor `LICENSE`.