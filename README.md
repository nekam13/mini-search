# Mini Search

Lehký osobní vyhledávač pro indexování vybraných webů. Aplikace crawluje stránky, ukládá jejich metadata a text do SQLite a používá vektorové vyhledávání pro sémanticky podobné výsledky.

> **Aktuální vývojová větev: `beta-optimized` (v7.2)**

## Funkce

- **Hybridní vyhledávání**: 60% hnswlib vektor + 35% FTS5 full-text + 5% SEO scoring
- Indexace vybraných webů ze sitemap, RSS/Atom feedů nebo odkazů z úvodní stránky
- **Gzip sitemap podpora** s omezením rekurze a maximálním počtem URL
- **Respektování robots.txt** a Crawl-delay pro každou doménu
- Vektorové vyhledávání pomocí SentenceTransformers a hnswlib
- **FTS5 full-text vyhledávání** s podporou češtiny (unicode61 tokenizer)
- Extrakce titulků, Open Graph metadat, textu, obrázků, audio odkazů a JSON-LD schema.org dat
- Filtry pro články, podcasty, audio a stránky s cenami
- **Autocomplete** nad názvy indexovaných stránek
- **Moderní vyhledávací stránka (Clay design)** s našeptávačem, filtry a řazením výsledků
- **Moderní admin panel (Clay design)**: dashboard, správa webů a zdrojů, vyhledávání v indexu, hromadné akce
- **Hierarchická správa zdrojů**: domény, konkrétní URL, sitemapy, RSS/Atom feedy
- **REST API** pro zdroje, weby a statistiky
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
2. Na dashboardu klikněte na **Přidat zdroj**.
3. Vyberte existující doménu, nebo ponechte „— nová doména —" a zadejte plnou URL. Typ zdroje se předvyplní automaticky podle adresy.
4. Workery postupně stahují povolené stránky a ukládají je do lokálního indexu.
5. Vyhledávejte na `http://127.0.0.1:8070/` přirozeným jazykem, česky i dalšími jazyky podporovanými použitým modelem.

## Vyhledávací stránka (Clay design)

Veřejné vyhledávání na `/` používá stejný design systém jako admin panel. Sdílené
tokeny (barvy, stíny, rádiusy, tlačítka, formuláře) jsou v `static/css/clay.css`,
stránkově specifické styly v `static/css/search.css` a logika v `static/js/search.js`.

Prvky rozhraní:

| Prvek | Popis |
|---|---|
| Našeptávač | Napovídá titulky z indexu, ovládá se šipkami, Enter potvrdí, Esc zavře |
| Filtry | Vše, Články, Podcasty, Audio, Ceny (tlačítka ve stylu chipů) |
| Řazení | Podle relevance, názvu nebo data (klientsky, bez přenačtení) |
| Karty výsledků | Doménový breadcrumb, zvýrazněné shody, indikátor relevance, audio přehrávač |
| Stránkování | 25 výsledků na stránku, parametr `page` (nad 500 výsledků se nepokračuje) |
| Prázdné stavy | Vysvětlení a přímý odkaz do správy zdrojů, když se nic nenajde |

Šablona dostává už připravená data (`prepare_results()`), takže v HTML nezůstává
žádná logika. Zvýrazňování hledaných výrazů escapuje text ještě před vložením
značek `<mark>`, takže uložené HTML v titulech se nevykreslí.

## Admin panel (Clay design)

Admin rozhraní je postavené na Flask šablonách (`templates/admin/`) a statických souborech
(`static/css/admin.css`, `static/js/admin.js`). Nahrazuje původní inline `ADMIN_HTML`.

| Stránka | URL | Popis |
|---|---|---|
| Přehled | `/admin` | Globální statistiky, rychlé akce, poslední weby |
| Weby a zdroje | `/admin/sites` | Filtrování, stránkování, správa webů |
| Detail webu | `/admin/sites/<id>` | Statistiky, seznam zdrojů, naposledy indexované stránky, chyby |
| Editace webu | `/admin/sites/<id>/edit` | `max_pages`, status, aliasy |
| Přidat zdroj | `/admin/sources/new` | Doména, URL, sitemap, RSS/Atom feed |
| Editace zdroje | `/admin/sources/<id>/edit` | URL, typ, priorita, poznámka, `max_pages` |
| Hledat v indexu | `/admin/search` | Fulltext/hybridní vyhledávání v indexovaných stránkách |

### Hierarchie zdrojů

Každý web (doména) může mít pod sebou libovolný počet zdrojů. Typy zdrojů:

| Typ | Popis |
|---|---|
| `domain` | Kanonická doména, zakládá záznam v `sites` |
| `url` | Konkrétní stránka, která se má indexovat přednostně |
| `sitemap` | Sitemap ke zpracování v rámci discovery |
| `feed`, `rss`, `atom` | RSS/Atom feed pro průběžné kontroly novinek |

Každý zdroj má `priority` (1–10), volitelnou `notes` a `status`. Manuálně přidané
sitemapy a feedy zpracovává jak `phase_1_discovery()`, tak plánovač
(`check_feeds`, `check_sitemaps`).

### Hromadné akce

- `/admin/recrawl-all` – zahájí recrawl všech aktivních webů
- `/admin/pause-all` – pozastaví všechny aktivní weby
- `/admin/resume-all` – obnoví všechny pozastavené weby

### Ověřování aktualizací

- `/admin/update-status` – JSON stav kontroly aktualizací
- `/admin/stats` – JSON statistiky indexu (kompatibilní s v6.1)

## REST API

Všechny endpointy vrací JSON a používají parametrizované dotazy (ochrana proti SQL injection).
Šablony escapují výstup (ochrana proti XSS).

| Metoda | Endpoint | Popis |
|---|---|---|
| `GET` | `/admin/api/sources` | Seznam zdrojů (volitelně `?site_id=`, `?source_type=`) |
| `POST` | `/admin/api/sources` | Přidat zdroj (JSON nebo form) |
| `GET` | `/admin/api/sources/<id>` | Detail zdroje |
| `PUT` | `/admin/api/sources/<id>` | Editace zdroje (`url`, `source_type`, `priority`, `notes`, `status`, `max_pages`) |
| `DELETE` | `/admin/api/sources/<id>` | Smazat zdroj |
| `GET` | `/admin/api/sources/<id>/stats` | Statistiky zdroje |
| `GET` | `/admin/api/sites` | Seznam webů (filtry `q`, `status`, `source_type`) |
| `GET` | `/admin/api/sites/<id>` | Detail webu včetně zdrojů a statistik |
| `PUT` | `/admin/api/sites/<id>` | Editace webu (`max_pages`, `status`, `aliases`) |
| `DELETE` | `/admin/api/sites/<id>` | Smazat web (kaskádově i zdroje) |
| `GET` | `/admin/api/stats` | Globální statistiky |

Příklad:

```bash
curl -X POST http://127.0.0.1:8070/admin/api/sources \
  -H "Content-Type: application/json" \
  -d '{"site_id": 1, "url": "https://example.com/sitemap.xml", "source_type": "sitemap", "priority": 7}'
```

## Validace a bezpečnost

- URL musí být platná (přijímá se doména i plná adresa včetně schématu).
- `max_pages` musí být celé číslo v rozsahu 1–10000.
- Priorita musí být celé číslo v rozsahu 1–10.
- URL musí být v rámci domény unikátní; duplicity jsou odmítnuty.
- Neplatný typ zdroje je odmítnut na úrovni aplikace i databázového `CHECK` constraintu.
- Chyby se logují do souboru (viz `ERROR_LOG_PATH`).

## Testy

```bash
python3 tests/test_db.py                     # schéma, indexy, FTS5, site_sources
python3 tests/test_admin.py                  # admin stránky a JSON API
python3 tests/test_admin_panel.py            # kompletní end-to-end testy admin panelu
python3 tests/test_discovery_integration.py  # napojení zdrojů na discovery a plánovač
python3 tests/test_search_page.py            # vyhledávací stránka, filtry, XSS, našeptávač
```

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

## Migrace z v6.1 / v7.0

Automatická migrace probíhá při prvním spuštění:
1. Přidají se nové sloupce do existujících tabulek (crawl_delay, scheduled_at, seo_score, recursion_depth)
2. Vytvoří se nová tabulka `pages_fts` pro full-text vyhledávání
3. Vytvoří se nová tabulka `update_status` pro sledování aktualizací
4. Vytvoří se nová tabulka `site_sources` pro hierarchickou správu zdrojů
5. Vytvoří se triggery pro automatickou synchronizaci FTS5
6. Provede se backfill FTS5 tabulky z existujících dat
7. Stávající weby se převedou na zdroj typu `domain` a existující `sitemaps_feeds`
   se převedou do `site_sources` (idempotentně, tedy bez duplicit při opakovaném spuštění)

**Žádná data nebudou smazána!**

## Licence

Projekt zatím nemá definovanou licenci. Před veřejným sdílením nebo použitím cizími lidmi doplňte soubor `LICENSE`.
