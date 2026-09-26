# Mini Search

Lehký osobní vyhledávač pro indexování vybraných webů. Aplikace crawluje stránky, ukládá jejich metadata a text do SQLite a používá vektorové vyhledávání pro sémanticky podobné výsledky.

> **Aktuální vývojová větev: `beta-optimized` (v7.5)**

## Funkce

- **Hybridní vyhledávání**: 60% hnswlib vektor + 35% FTS5 full-text + 5% SEO scoring
- Indexace vybraných webů ze sitemap, RSS/Atom feedů nebo odkazů z úvodní stránky
- **Gzip sitemap podpora** s omezením rekurze a maximálním počtem URL
- **Respektování robots.txt** a Crawl-delay pro každou doménu
- **Indexace lokálních webů**: automatická detekce privátních adres, ignorování robots.txt a 3× vyšší priorita ve vyhledávání
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
| Karty výsledků | Strukturované „Rich Results" karty podle typu obsahu (viz níže) |
| Obrázky | Samostatná sekce s náhledy obrázků z nalezených stránek (viz níže) |
| Rich Results | Wikipedie, produkt, recept, organizace a článek – každý s vlastními informacemi |
| Stránkování | 25 výsledků na stránku, parametr `page` (nad 500 výsledků se nepokračuje) |
| Prázdné stavy | Vysvětlení a přímý odkaz do správy zdrojů, když se nic nenajde |

Vzhled stojí na „clay" principech: měkký vnější stín (`--clay-drop`) doplněný
vnitřním odleskem (`--clay-lift`) u vyvýšených ploch a vnitřním stínem
(`--clay-inset`) u vstupů a stisknutých tlačítek. Díky tomu prvky působí jako
vytvarované z jednoho kusu materiálu, ne jako ploché obdélníky. Interaktivní
prvky se při najetí nadzvednou a při stisku „zapadnou".

Šablona dostává už připravená data (`prepare_results()`), takže v HTML nezůstává
žádná logika. Zvýrazňování hledaných výrazů escapuje text ještě před vložením
značek `<mark>`, takže uložené HTML v titulech se nevykreslí.

### Rich Results karty

Každý výsledek dostane v `prepare_results()` klíč `card_type`, podle kterého se
vybere vizuální karta:

| `card_type` | Kdy | Co se zobrazí |
|---|---|---|
| `wiki` | cíl je `*.wikipedia.org` / Wikimedia | badge „Wikipedie", jazyk, kategorie z breadcrumbs, zdroj Wikimedia, náhledový obrázek |
| `product` | Schema.org `Product` (nebo jakákoli stránka s cenou) | náhled, cena (`price-tag`), dostupnost (`Skladem`/`Vyprodáno`), hodnocení hvězdičkami, značka, kód |
| `recipe` | Schema.org `Recipe` | obrázek jídla, doba přípravy, kalorie, hodnocení, porce, kuchyně |
| `organization` | Schema.org `Organization`/`LocalBusiness` | logo, adresa, telefon, otevírací doba |
| `article` | výchozí (cokoli jiného) | titulek, breadcrumb, zvýrazněný snippet, autor a datum |

Typ se určuje z hostitele URL a ze strukturovaných dat (`schema_type` +
`schema_details`, kde je i `_meta` s breadcrumbs a autorem). Veškeré hodnoty se
předpočítávají do hotových řetězců (cena s `Kč`, doba jako `1 h 20 min`), takže
šablona jen vykresluje. Obrázky se přijímají jen jako `http(s)` nebo kořenově
relativní URL – `javascript:` i `data:` se zahazují, aby se do `<img src>`
nedostal skript. Vše ostatní prochází autoescapingem.

Styly karet jsou v `static/css/clay.css` (třídy `.card-rich`, `.card-wiki`,
`.card-product`, `.card-recipe`, `.card-organization`, `.badge-wiki`,
`.price-tag`, `.rating-stars`, `.rich-metadata`, `.card-thumbnail`,
`.card-media-layout`) a používají výhradně existující tokeny design systému.
Na malých obrazovkách se náhled přesune nad text a zmenší se.

### Sekce obrázků

Pod seznamem výsledků je samostatná sekce „Obrázky" (`.image-results`), která
zobrazí náhledy z už nalezených stránek. Bere `og:image` a obrázky z JSON-LD
(`images`), takže se **neukládají ani neindexují jako samostatné stránky** – do
`pages`, FTS ani crawl fronty se obrázky nikdy nedostanou. Tím se šetří místo
i RAM na mobilu a zároveň zůstávají obrázky dostupné u výsledků.

- `MINISEARCH_IMAGE_RESULTS` (výchozí 12, `0` sekci vypne) určuje, kolik
  obrázků se má nejvýše zobrazit.
- Filtr **Obrázky** (`/?q=…&filter=images`) zobrazí obrázky jako hlavní
  výsledek – prohledá širší okno indexu a vysbírá náhledy z nalezených stránek.
  Kolik jich nejvýše vrátí, určuje `MINISEARCH_IMAGE_SEARCH_LIMIT` (výchozí 60).
- URL obrázků projdou kontrolou `_safe_image_url()` – povoleny jsou jen
  `http(s)` adresy, `javascript:` a `data:` se zahazují (XSS ochrana).
- Náhledy se načítají líně (`loading="lazy"`) a bez referreru.
- Styly jsou v `static/css/search.css` (`.image-grid`, `.image-card`), tokeny
  zůstávají pouze v `clay.css`.
- Import z Wikipedie ukládá k článku i obrázky (viz níže), takže sekce
  „Obrázky" má co zobrazit i bez crawlování webu.

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
| Nastavení | `/admin/settings` | Runtime nastavení (váhy, crawler, wiki, údržba) |

### Nastavení v adminu (`/admin/settings`)

Většina voleb byla dříve jen v proměnných prostředí. Nyní je lze pohodlně měnit
v adminu; hodnota se uloží do tabulky `app_settings` a **má přednost před
prostředím**. Většina změn platí okamžitě, položky označené `restart` se
projeví až po restartu aplikace (např. počet crawlovacích vláken nebo plán
noční údržby).

- Skupiny: **Vyhledávání**, **Crawler**, **Automatická obnova**, **Wikipedie**,
  **Vektorové hledání**, **Noční údržba**.
- U každé položky je vidět, kterou proměnnou prostředí se seeduje
  (`MINISEARCH_*`), takže je jasné, odkud hodnota pochází.
- Tlačítko **Obnovit výchozí** smaže všechny uložené přepisy a vrátí hodnoty
  z prostředí.
- JSON varianta pro skripty: `GET/POST /admin/api/settings`
  (`{"settings": {"search.vector_weight": 55}}`, resp. `{"action": "reset"}`).

Port aplikace se nastavuje přes `MINISEARCH_PORT` (výchozí `8070`).

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

### Automatická indexace po přidání zdroje

`POST /admin/api/sources` zdroj nejen uloží, ale rovnou spustí jeho indexaci
(`index_source()`), takže není potřeba ručně otevírat detail a mačkat recrawl:

| Typ zdroje | Co se zařadí do fronty |
|---|---|
| `url` | Daná URL s prioritou zdroje |
| `sitemap` | Všechny URL ze sitemapy (priorita 3) |
| `feed` / `rss` / `atom` | Všechny položky feedu (priorita 1) |
| `domain` | Odkazy nalezené na úvodní stránce (priorita 5) |

Indexace běží na pozadí (`index_source_async()`), takže odpověď API nečeká na
síť. Zdroj ve stavu `paused` se neindexuje. Sloupec `last_checked` se po
zpracování aktualizuje.

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
python3 tests/test_source_indexing.py        # auto-indexace nových zdrojů a data dashboardu
python3 tests/test_local_sites.py            # lokální sítě: detekce, robots.txt, boost
python3 tests/test_local_indexing_smoke.py   # živý lokální HTTP server: crawl a indexace
python3 tests/test_wiki_import.py            # wiki importér: dump, API, idempotence, resume
python3 tests/test_crawl_metadata.py         # Schema.org/OG, charset, retry/backoff, lowmem
python3 tests/test_rich_cards.py             # Rich Results karty, XSS, auto-migrace a úklid
python3 tests/test_maintenance.py            # noční údržba: dedup, vektory, wiki, mrtvé odkazy
python3 tests/test_settings_images.py        # admin nastavení, filtr Obrázky, wiki obrázky
```

Všechny testy jsou deterministické a běží bez živého internetu – síťová část
používá lokální HTTP server a wiki import malý lokální dump.

## Lokální weby (místní síť)

Mini Search umí indexovat weby běžící v místní síti – `localhost`, `127.0.0.1`,
`192.168.x.x`, `10.x.x.x`, `172.16–31.x.x`, `*.local`, `*.lan` a podobně.

- **Automatická detekce**: adresa se rozpozná jako lokální podle hostitele
  (privátní rozsahy, CGNAT, single-label jména, vyhrazené suffixy).
- **Bez robots.txt**: u lokálních webů se `robots.txt` nevyžaduje ani
  nevyhodnocuje – dev servery často vrací `Disallow: /`, což by blokovalo indexaci.
- **Vyšší priorita (3×)**: lokální výsledky se ve vyhledávání násobí koeficientem
  `3.0` (veřejné `1.0`) a v přehledech se řadí nahoru. Koeficient lze upravit
  u každého webu v adminu (1–10).
- **Fronta**: lokální URL se zařazují s nejurgentnější prioritou (`1`), tedy se
  zpracují před ostatními.

Lokální web přidáte v adminu přes **Přidat zdroj** (zaškrtněte „Lokální web“
nebo nechte prázdné pro automatickou detekci), případně v **Editovat web**
u existující domény. Nové zdroje (URL, sitemap, feed) přidané pod lokální
doménu automaticky doménu označí jako lokální.

## Filtry ve vyhledávání

| Filtr | Zobrazí |
|---|---|
| Vše | Všechny relevantní indexované stránky |
| Články | `Article`, `BlogPosting` a `NewsArticle` z JSON-LD |
| Podcasty | `PodcastEpisode` z JSON-LD |
| Audio | Stránky s nalezeným audio souborem nebo HTML `<audio>` |
| Ceny | Stránky, jejichž schema.org data obsahují cenu |

## Hybridní vyhledávání

Výsledky jsou řazeny kombinací (váhy jdou nastavit přes prostředí):
- **50% vektorové podobnosti** (hnswlib + SentenceTransformers, případně lokální hashovací fallback)
- **40% full-text vyhledávání** (FTS5 s unicode61 tokenizerem pro češtinu)
- **10% SEO skóre** (přítomnost titulku, popisu, schema markup, atd.)

| Proměnná | Výchozí | Význam |
|---|---|---|
| `MINISEARCH_VECTOR_WEIGHT` | 50 | váha vektorové podobnosti |
| `MINISEARCH_FTS_WEIGHT` | 40 | váha plnotextového hledání |
| `MINISEARCH_SEO_WEIGHT` | 10 | váha SEO skóre |

Váhy by měly dát dohromady 100. Všechny tři sečtou dílčí skóre, které se pak
ještě vynásobí prioritou webu (místní sítě 3×).

## Česká Wikipedie

Wikipedie se indexuje **nativním importérem**, ne obecným crawlerem. Jsou dvě
varianty a obě streamují data po jednom článku, takže se nikdy nenačte celý
dump do paměti:

| Importér | Co dělá | Kdy se hodí |
|---|---|---|
| `api` (výchozí) | Prochází MediaWiki API po dávkách (`allpages` + `extracts`). Neukládá nic na disk. | Rychlý start, telefon, kdy není místo na dump. |
| `dump` | Streamuje lokálně stažený `cswiki-*-pages-articles-multistream.xml.bz2` přes `xml.etree` v pull režimu. | Offline, hromadný import bez tisíců HTTP dotazů a bez rate limitů. |

### Přidání v adminu

V adminu na **Přidat zdroj** zvolte typ **Wikipedie** a jako URL zadejte:

- `cs` nebo `wiki:cs` – jazykový kód (výchozí je `cs`),
- `cs.wikipedia.org` – doména,
- `file:///cesta/cswiki-latest-pages-articles-multistream.xml.bz2` – lokální dump.

Importér (`api`/`dump`) se u `file://` předvyplní na `dump` automaticky.
Do pole **Max stránek** u domény zadejte, kolik článků se má indexovat.

### Import z příkazové řádky (Termux)

Pro hromadný import doporučujeme jednorázový příkaz místo běžící aplikace:

```bash
# Stažení českého dumpu (jednorázově, ~1 GB; uložte mimo repozitář)
mkdir -p ~/wiki-dumps
cd ~/wiki-dumps
curl -LO https://dumps.wikimedia.org/cswiki/latest/cswiki-latest-pages-articles-multistream.xml.bz2

# Import prvních 5000 článků
python3 app_combined.py --import-wiki "file://$HOME/wiki-dumps/cswiki-latest-pages-articles-multistream.xml.bz2" --max-pages 5000

# Pokračování v přerušeném importu (stav se ukládá u zdroje)
python3 app_combined.py --source-id 1 --max-pages 5000

# Import celého dumpu (opakuje dávky, dokud nepřestanou přibývat články)
python3 app_combined.py --import-wiki "file://$HOME/wiki-dumps/....xml.bz2" --all

# Bez lokálního úložiště, rovnou z API
python3 app_combined.py --import-wiki cs --max-pages 2000
```

- **Idempotence**: opakovaný import stejný článek nezdvojí (`url_hash` dedup).
  Dump se čte od titulu podle uloženého `next_title`, takže navázání pokračuje
  tam, kde se přestalo.
- **Pozastavení**: zdroj se stavem `paused` importer přeskočí.
- **Filtrování**: přesměrování se neukládají jako stránky, ale jako aliasy webu
  (hledání na přesměrovaný název tak najde cílový článek). Diskusní, uživatelské,
  kategoriální, šablonové a další neencyklopedické jmenné prostory se přeskakují.
- **Diakritika**: text se ukládá v originále a zobrazuje se s diakritikou, ale FTS
  dotazy se skládají z „odháčkované“ podoby. Dotaz `cesky` tedy najde `český`
  a naopak; zvýraznění `<mark>` se aplikuje až po escapování (`_highlight_snippet`).
- **Obrázky**: k článku se ukládá i náhled pro samostatnou sekci „Obrázky".
  API importér si vyžádá `pageimages` (jen URL, pár bajtů), dump importér sestaví
  adresu z `[[Soubor:…]]` přes `Special:FilePath`. Na článek se ukládá nejvýše
  `MINISEARCH_WIKI_IMAGES` (výchozí 3) obrázků, aby galerie nenafoukla úložiště.
  Obrázky se **neindexují jako stránky**, jsou jen metadata u článku.
- **Nastavení**: jazyk, velikost dávky a počet pokusů lze měnit i v adminu na
  `/admin/settings` (skupina **Wikipedie**).

### Úložiště a RAM

Dump se **nikdy necommituje** (viz `.gitignore`) a drží se mimo repozitář. Během
importu je v paměti vždy jeden článek. Vektorové embeddingy se v
nízkopaměťovém režimu úplně vynechávají a hledání se opírá o FTS5.

## Nízkopaměťový režim (Termux/Android)

`MINISEARCH_PROFILE=lowmem` (nebo `MINISEARCH_LOWMEM=1`) přepne všechny drahé
výchozí hodnoty najednou. Konfigurace se čte z prostředí:

| Proměnná | Výchozí | Význam |
|---|---|---|
| `MINISEARCH_PROFILE` | – | `lowmem` = úsporný režim pro mobil |
| `MINISEARCH_WORKERS` | 1 (lowmem) / 2 | Počet crawlovacích vláken |
| `MINISEARCH_EMBEDDINGS` | `auto` (lowmem) / `1` | `auto` = vektory jen při dostatku RAM a baterie; `1`/`0` = natvrdo |
| `MINISEARCH_MIN_FREE_MB` | 300 | Pod touto volnou RAM (MB) se `auto` vektory vypne |
| `MINISEARCH_MIN_BATTERY_PCT` | 15 | Pod tímto % baterie (bez nabíjení) se `auto` vektory vypne |
| `MINISEARCH_REQUEST_TIMEOUT` | 10 (lowmem) / 30 | HTTP timeout (s) |
| `MINISEARCH_MAX_RETRIES` | 3 | Počet pokusů u síťových chyb |
| `MINISEARCH_RETRY_MAX_DELAY` | 5 | Strop exponenciálního backoffu (s) |
| `MINISEARCH_MAX_BODY_CHARS` | 3500 | Max délka těla stránky |
| `MINISEARCH_MAX_LINKS_PER_PAGE` | 100 | Max nových odkazů z jedné stránky (0 = vypnuto) |
| `MINISEARCH_IMAGE_RESULTS` | 12 | Kolik obrázků zobrazit v sekci výsledků (0 = sekce vypnutá) |
| `MINISEARCH_VECTOR_WEIGHT` | 50 | Váha vektorového hledání (%) |
| `MINISEARCH_FTS_WEIGHT` | 40 | Váha plnotextového hledání (%) |
| `MINISEARCH_SEO_WEIGHT` | 10 | Váha SEO skóre (%) |
| `MINISEARCH_RECRAWL_FEED_HOURS` | 1 | Interval obnovy feedů (0 = vypnuto) |
| `MINISEARCH_RECRAWL_SITEMAP_HOURS` | 24 | Interval obnovy sitemap (0 = vypnuto) |
| `MINISEARCH_RECRAWL_SITE_HOURS` | 12 | Interval obnovy webů (0 = vypnuto) |
| `MINISEARCH_RECRAWL_STALE_HOURS` | 72 | Jak stará stránka se smí obnovit |
| `MINISEARCH_SQLITE_CACHE_KB` | 2048 (lowmem) / 16384 | Cache SQLite (KiB) |
| `MINISEARCH_SQLITE_TEMP_STORE` | 1 (lowmem) / 2 | Dočasné tabulky: 0=default, 1=soubor, 2=RAM |
| `MINISEARCH_HNSW_MAX_ELEMENTS` | 20000 (lowmem) / 100000 | Strop vektorů pro hnswlib index |
| `MINISEARCH_WIKI_LANG` | `cs` | Výchozí jazyk Wikipedie |
| `MINISEARCH_WIKI_BATCH` | 50 | Dávka pro wiki API |
| `MINISEARCH_WIKI_API_BASE` | – | Mirror MediaWiki API (např. pro testy) |

```bash
export MINISEARCH_PROFILE=lowmem
export MINISEARCH_WORKERS=1
./start.sh
```

Po přerušení (kill/crash) se při startu řádky fronty, které zůstaly v `locked`,
vrátí zpět do `pending`, takže se zpracování samo obnoví.

### Vektorové vyhledávání na mobilu (`auto`)

V lowmem režimu je výchozí `MINISEARCH_EMBEDDINGS=auto`: vektorové hledání se
zapne, jen když je na zařízení dost prostředků. Aplikace zjišťuje volnou RAM
(`/proc/meminfo`, jinak `os.sysconf`) a stav baterie (`/sys/class/power_supply`);
když je volné paměti méně než `MINISEARCH_MIN_FREE_MB`, nebo je baterie pod
`MINISEARCH_MIN_BATTERY_PCT` a telefon se nenabíjí, vektory se přeskočí a hledání
plynule přejde na FTS5. Na zařízeních, kde se stav nedá přečíst, se nic neblokuje.
Chování lze vynutit pomocí `MINISEARCH_EMBEDDINGS=1` nebo `0`.

Když `sentence-transformers` není nainstalovaný (typicky na Termuxu), použije se
místo něj **lokální hashovací embedding** (`_HashingEmbedding`) – hash
foldovaných tokenů normalizovaný do jednotkové délky. Nezabírá místo, nestahuje
model a česká diakritika se v něm skládá (`Praha` == `praha`). Kvalita je nižší
než u skutečného modelu, takže ten má vždy přednost; hashování je jen rozumný
fallback, aby vektorové hledání nevrátilo hlouposti jako dřívější nulové vektory.

Vektory se navíc neindexují do hnswlib, pokud jich je více než
`MINISEARCH_HNSW_MAX_ELEMENTS` nebo pokud lowmem režim není v dobré kondici;
hledání se pak plynule vrací na FTS5. Crawling, sitemapy a sledování odkazů
nikdy nezávisí na vektorech – lowmem vypíná jen AI část, ne zpracování stránek.

## Noční údržba („dreaming mode")

Jedním během se databáze zkonsoliduje, doplní chybějící vektory, pomalu
pokračuje v importu Wikipedie a (volitelně) zkontroluje mrtvé odkazy. Režim je
šetrný k paměti: každá etapa je dávkovaná, respektuje `SHUTDOWN_FLAG` (lze ji
přerušit) a průběh se loguje do `logs/maintenance.log`.

```bash
# Jednorázově (ideálně přes termux-job-scheduler / cron v noci)
python3 app_combined.py --maintenance

# Jen úklid DB a dopočet vektorů, bez sítě
python3 app_combined.py --nightly --no-wiki --no-links

# Omezit wiki import a zapnout kontrolu odkazů s promazáním mrtvých
MINISEARCH_MAINT_LINK_CHECK=200 MINISEARCH_MAINT_PURGE_DEAD=1 \
  python3 app_combined.py --maintenance --max-pages 500
```

Co který krok dělá:

1. **Dedup a úklid** – sloučí stránky se stejným `url_hash` (přesná shoda URL)
   i stránky s identickým obsahem (podpis z nejčastějších tokenů; krátká těla se
   nechávají být, aby se nesléval boilerplate). Uvolněné místo se vrátí pomocí
   `PRAGMA optimize`; `VACUUM` se spustí jen když je na disku dost místa
   (`MINISEARCH_MAINT_VACUUM_MIN_MB`, jinak se přeskočí a nic se nerozbije).
2. **Dopočet vektorů** – stránkám z lowmem režimu (`embedding IS NULL`)
   dopočítá vektory, ale jen když jsou vektory vůbec povolené (respektuje
   `MINISEARCH_EMBEDDINGS=auto`). Běží v dávkách a při zhoršení prostředků se
   zastaví; zbytek dobere další noc.
3. **Pomalý import Wikipedie** – naváže na uložený `next_title` u aktivních wiki
   zdrojů (`--max-pages`, výchozí `MINISEARCH_MAINT_WIKI_PAGES=200`). Pauznuté
   zdroje přeskočí, opakovaný běh je idempotentní.
4. **Kontrola odkazů** – ověří nejstarší stránky (HEAD, při 405 fallback na GET)
   a stránky s 404/410 označí `link_status='dead'`; s
   `MINISEARCH_MAINT_PURGE_DEAD=1` je i smaže. Ověřování se ve výchozím stavu
   **nespouští** (`MINISEARCH_MAINT_LINK_CHECK=0`), protože sahá na síť.

Na pozadí lze údržbu zapnout přes `MINISEARCH_NIGHTLY=1`
(hodina `MINISEARCH_NIGHTLY_HOUR`, výchozí 3:00).

| Proměnná | Výchozí | Význam |
|---|---|---|
| `MINISEARCH_MAINTENANCE_LOG` | `logs/maintenance.log` | Kam se loguje průběh |
| `MINISEARCH_MAINT_WIKI_PAGES` | 200 | Max článků na jednu noc (0 vypne) |
| `MINISEARCH_MAINT_EMBED_BATCH` | 200 | Max stránek s dopočtem vektorů na běh |
| `MINISEARCH_MAINT_LINK_CHECK` | 0 | Kolik nejstarších stránek ověřit (0 vypne) |
| `MINISEARCH_MAINT_LINK_CHECK_DAYS` | 30 | Po kolika dnech stránku přeověřit |
| `MINISEARCH_MAINT_PURGE_DEAD` | 0 | `1` = mazat stránky s 404/410 |
| `MINISEARCH_MAINT_VACUUM_MIN_MB` | 50 | Kolik MB volného místa je potřeba pro `VACUUM` |
| `MINISEARCH_NIGHTLY` | 0 | `1` = registrovat noční úlohu na pozadí |
| `MINISEARCH_NIGHTLY_HOUR` | 3 | Hodina noční úlohy (0–23) |

## Zpracování stránek a crawler

- **Canonical URL**: přednostně se použije `<link rel="canonical">`, tracking
  parametry se odstraňují, duplicitní stránky se sloučí pod stejný `url_hash`.
- **Kódování**: respektuje se `charset` z HTTP hlavičky, z meta tagu
  (`<meta charset>` i `<meta http-equiv>`) a nakonec se zkouší UTF-8 a
  `windows-1250` pro staré české weby. Weby posílané jako `text/html` **bez**
  uvedeného charsetu se dekódují jako UTF-8 (ať se nerozbijí háčky – dřív
  vznikalo „KouzelnÃ©").
- **Metadata**: OpenGraph, Twitter Card a Schema.org JSON-LD (vnořené `@graph`,
  pole `@type`, poškozené bloky se přeskočí, ne zahodí) – autor, datum,
  breadcrumbs, obrázky, audio a typ článku.
- **robots.txt a crawl-delay**: načítají se přes společné HTTP vrstvy s timeoutem
  a User-Agent; u lokálních webů a Wikipedie se robots.txt obchází.
- **Retry/backoff**: přechodné stavy (429, 500, 502, 503, 504, 408, 425) se
  opakují s exponenciálním zpožděním; `429` respektuje `Retry-After`.
- **Sledování odkazů**: každá zaindexovaná stránka přidá do fronty odkazy, které
  na ní najde (`<a href>`), takže se web proleze i když sitemap chybí nebo je
  neúplný. Přeskakují se odkazy mimo doménu, `mailto:`/`javascript:`, obrázky a
  další přílohy; počet nových odkazů na stránku hlídá
  `MINISEARCH_MAX_LINKS_PER_PAGE`. Celkový počet stránek drží `max_pages` webu.
- **Sitemapy**: `robots.txt` (`Sitemap:`), indexové sitemapy se rekurzivně
  rozbalí, gzip se pozná podle magic bytes (i při špatném `Content-Type`),
  duplicitní a prázdné `<loc>` se vyčistí a poškozené XML se přeskočí. Čtou se
  jen přímé potomky `<loc>`, takže se do fronty nedostanou obrázky z rozšíření
  `<image:loc>`/`<video:loc>`.
- **Obrázky a přílohy**: URL končící na obrázek/audio/video (`.jpg`, `.png`,
  `.mp3`, …) se nikdy nezařadí do fronty ani do indexu – zobrazují se pouze
  v samostatné sekci výsledků.
- **Odolnost**: jeden velký nebo poškozený článek nezastaví ostatní; těla stránek
  se ořezávají na `MINISEARCH_MAX_BODY_CHARS`.

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

### Automatická migrace a úklid

Migrace probíhá sama při každém startu (`get_db()`), takže se nic nemusí spouštět
ručně a stará databáze se upgraduje na místě. Aplikace nejdřív ověří, že výsledná
tabulka má všechny sloupce a že nový typ `wiki` projde CHECK constraintem; teprve
potom smaže případnou zálohu z přerušené migrace (`site_sources_old`). Pokud by
nová tabulka obsahovala méně řádků než záloha, záloha se **zachová** k ruční
kontrole. Díky tomu je aktualizace bezpečná i při výpadku uprostřed přestavby.

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
