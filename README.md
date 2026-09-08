# Mini Search

Lehký osobní vyhledávač pro indexování vybraných webů. Aplikace crawluje stránky, ukládá jejich metadata a text do SQLite a používá vektorové vyhledávání pro sémanticky podobné výsledky.

> **Aktuální vývojová větev: `beta-optimized` (v7.0)**

## Funkce

- **Hybridní vyhledávání**: 60% hnswlib vektor + 35% FTS5 full-text + 5% SEO scoring
- Indexace vybraných webů ze sitemap, RSS/Atom feedů nebo odkazů z úvodní stránky
- **Gzip sitemap podpora** s omezením rekurze a maximálním počtem URL
- **Respektování robots.txt** a Crawl-delay pro každou doménu
- Vektorové vyhledávání pomocí SentenceTransformers a hnswlib
- **FTS5 full-text vyhledávání** s podporou češtiny (unicode61 tokenizer)
- Extrakce titulků, Open Graph metadat, textu, obrázků, audio odkazů a JSON-LD schema.org dat
- Filtry pro články, podcasty, audio a stránky s cenami
- Autocomplete nad názvy indexovaných stránek
- **Správcovské rozhraní** s podporou aktualizací
- Pravidelné kontroly RSS feedů, sitemap a opakované procházení aktivních webů
- Ošetření HTTP 429 (`Retry-After`) a HTTP 403 (označení webu jako blokovaného)
- **Nedestruktivní recrawl** s respektováním max_pages
- Deduplikace velmi podobných výsledků
- **Automatické aktualizace** z GitHub

## Technologie

- Python 3.10+
- Flask
- SQLite (WAL režim) s FTS5
- hnswlib
- SentenceTransformers
- model `paraphrase-multilingual-MiniLM-L12-v2`
- Beautiful Soup, lxml, extruct a feedparser
- APScheduler

## Spuštění v Ubuntu přes Termux

Pro náročné knihovny, jako jsou PyTorch, NumPy nebo hnswlib, je doporučeno spouštět projekt v Ubuntu přes `proot-distro`, ne přímo v čistém Termuxu.

### 1. Spusťte Ubuntu

V Termuxu:

```bash
proot-distro login ubuntu
apt update && apt upgrade -y
```

### 2. Nainstalujte systémové závislosti

```bash
apt install -y \
  python3 python3-pip python3-venv git \
  build-essential cmake pkg-config \
  libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev
```

### 3. Naklonujte repozitář

```bash
cd ~
git clone https://github.com/nekam13/mini-search.git
cd mini-search
git checkout beta-optimized
git pull origin beta-optimized
```

Pokud už repozitář máte naklonovaný:

```bash
cd ~/mini-search
git fetch origin
git checkout beta-optimized
git pull origin beta-optimized
```

### 4. Spusťte setup

```bash
./setup.sh
```

Setup provede:
- Instalaci systémových závislostí
- Vytvoření virtuálního prostředí
- Instalaci Python balíčků
- Stažení jazykového modelu
- Inicializaci databáze

### 5. Spusťte aplikaci

```bash
./start.sh
```

Aplikace poběží na adrese:
- Vyhledávání: `http://127.0.0.1:8070/`
- Správa indexu: `http://127.0.0.1:8070/admin`

## Běžné spouštění později

Po otevření Termuxu spusťte:

```bash
proot-distro login ubuntu
cd ~/mini-search
./start.sh
```

## Použití

1. Otevřete `http://127.0.0.1:8070/admin`.
2. Do formuláře vložíte URL webu a nastavte maximální počet stránek.
3. Aplikace najde sitemapu nebo feed; pokud je nenajde, pokusí se získat odkazy z domovské stránky.
4. Workery postupně stahují povolené stránky a ukládají je do lokálního indexu.
5. Vyhledávejte na `http://127.0.0.1:8070/` přirozeným jazykem, česky i dalšími jazyky podporovanými použitým modelem.

## Filtry ve vyhledávání

| Filtr | Zobrazí |
|---|---|
| Vše | Všechny relevantní indexované stránky |
| Články | `Article`, `BlogPosting` a `NewsArticle` z JSON-LD |
| Podcasty | `PodcastEpisode` z JSON-LD |
| Audio | Stránky s nalezeným audio souborem nebo HTML `<audio>` |
| Ceny | Stránky, jejichž schema.org data obsahují cenu |

## Hybridní vyhledávání

Výsledky jsou řazeny kombinací:
- **60% vektorové podobnosti** (hnswlib + SentenceTransformers)
- **35% full-text vyhledávání** (FTS5 s unicode61 tokenizerem pro češtinu)
- **5% SEO skóre** (přítomnost titulku, popisu, schema markup, atd.)

## Data a úložiště

Aplikace vytváří lokální SQLite databázi `console.db` v aktuálním adresáři. Obsahuje zejména:

- Seznam sledovaných webů
- Frontu URL pro crawling
- Uložené stránky a metadata
- Textový obsah
- Embeddingy pro vektorové vyhledávání
- FTS5 index pro full-text vyhledávání
- Objevené sitemapy a feedy
- Stav aktualizací

HNSW index je při běhu v paměti. Při restartu se znovu sestaví z embeddingů uložených v SQLite.

## Aktualizace

### Bezpečný postup aktualizace

Pro bezpečnou aktualizaci existující instalace:

1. **Zastavte aplikaci**:
   ```bash
   ./stop.sh
   ```

2. **Zálohujte data** (volitelné, ale doporučené):
   ```bash
   cp console.db console.db.backup
   cp -r logs logs.backup
   ```

3. **Spusťte aktualizaci**:
   ```bash
   ./update.sh
   ```
   
   Nebo přes správcovské rozhraní:
   - Přejděte na `http://127.0.0.1:8070/admin`
   - Klikněte na "Kontrolovat aktualizace"
   - Pokud je aktualizace dostupná, klikněte na "Aktualizovat"

4. **Spusťte aplikaci znovu**:
   ```bash
   ./start.sh
   ```

### Co dělá update.sh:
- Zastaví běžící proces
- Vytvoří zálohu databáze a logů
- Aktualizuje kód z GitHub (větev beta-optimized)
- Nainstaluje nové závislosti
- Provede migraci databáze (nedestruktivní)
- Uloží aktuální commit SHA

### Automatické kontrolování aktualizací

Aplikace automaticky kontroluje aktualizace každých 6 hodin. Stav aktualizace můžete zkontrolovat:
- V souboru `.commit_sha` (aktuální commit)
- V souboru `.update_available` (1 = dostupná, 0 = ne)
- V souboru `update_log` (historie kontrol a aktualizací)
- V databázové tabulce `update_status`
- Přes admin rozhraní

## Řešení problémů

### `Package ... has no installation candidate`

Ověřte, že jste uvnitř Ubuntu (`proot-distro login ubuntu`) a že máte aktualizované seznamy balíčků:

```bash
apt update
apt install -y libxml2-dev libxslt1-dev python3-venv python3-pip
```

### PyTorch se nevejde do paměti

Používejte instalaci bez cache:

```bash
pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
```

Před instalací zkontrolujte dostupné místo:

```bash
df -h
```

### `hnswlib` nejde nainstalovat

Doinstalujte překladové nástroje a zkuste instalaci znovu:

```bash
apt install -y build-essential cmake python3-dev
pip install --no-cache-dir hnswlib
```

### Aplikace se nespustí kvůli portu

Zkontrolujte, zda už neběžela jiná instance aplikace. Případně ji ukončete pomocí `Ctrl+C` v původním terminálu.

### Problémy s FTS5

Ověřte, že máte SQLite s podporou FTS5:

```bash
sqlite3 :memory: "CREATE VIRTUAL TABLE test USING fts5(content); DROP TABLE test;"
```

Pokud to selže, zkontrolujte verzi SQLite:

```bash
sqlite3 --version
```

FTS5 je dostupné od SQLite 3.9.0 (2015).

## Poznámky k provozu

- Crawlujte pouze weby, u kterých je takové použití přiměřené a povolené.
- Aplikace kontroluje `robots.txt`, ale provozovatel webu může přístup omezit i jinak.
- U větších webů začněte s nízkou hodnotou `max_pages`, např. 50 až 200.
- Model i PyTorch mohou na telefonu zabrat významné množství úložiště a RAM.
- **Důležité**: Při aktualizaci se databáze nemazá! Všechny indexované stránky a embeddingy zůstávají zachovány.

## Migrace z v6.1

Při prvním spuštění v7.0 dojde k automatické migraci:
1. Přidají se nové sloupce do existujících tabulek (crawl_delay, scheduled_at, seo_score, recursion_depth)
2. Vytvoří se nová tabulka `pages_fts` pro full-text vyhledávání
3. Vytvoří se nová tabulka `update_status` pro sledování aktualizací
4. Vytvoří se triggery pro automatickou synchronizaci FTS5
5. Provede se backfill FTS5 tabulky z existujících dat

**Žádná data nebudou smazána!**

## Licence

Projekt zatím nemá definovanou licenci. Před veřejným sdílením nebo použitím cizími lidmi doplňte soubor `LICENSE`.
