# Mini Search – Android APK

Samostatná aplikace pro Android, která **nevyžaduje Termux**. Server
(`app_combined.py`) i jeho Python závislosti jsou zabalené přímo v APK pomocí
[Chaquopy](https://chaquo.com/chaquopy/). Aplikace má **nativní české UI**
(obrazovky Domů / Hledat / Zdroje / Správa / Nastavení) a jen ve dvou záložkách
(Hledat, Správa) zobrazuje lokální server v uzamčeném WebView. Přidávání
a správa zdrojů je **součástí aplikace** (nativní obrazovka Zdroje), ne webová
stránka — veřejná stránka `/` žádné přidávání zdrojů nenabízí.

- **Domů** – nativní ovládání serveru: stav (`STOPPED`/`STARTING`/`RUNNING`/
  `STOPPING`/`ERROR`), tlačítka Start / Zastavit / Znovu spustit, přepínač
  „Sdílet v síti" (LAN), zobrazení LAN adresy pro PC a import wiki dumpu.
- **Hledat** – veřejná stránka `/` v WebView (jen vyhledávání a novinky).
- **Zdroje** – nativní obrazovka: přidání zdroje (typ se rozpozná automaticky
  z adresy), seznam zdrojů s re-crawlem a smazáním. Volá Python přes
  `android_entry.sources_json()` / `add_source_json()` / `delete_source_json()`
  / `recrawl_source_json()`, takže platí jediný zdroj pravdy (`site_sources`)
  a indexace se spouští na pozadí.
- **Správa** – panel `/admin` v WebView; server poslouchá jen na `127.0.0.1`,
  takže administrace není dostupná z LAN (`MINISEARCH_ADMIN_LOCAL_ONLY=1`).
  Mutující akce (recrawl, pauza, obnovení, smazání) jsou **POST-only**, aby je
  nešlo spustit obyčejným cross-site odkazem; požadavky s cizím
  `Origin`/`Referer` se odmítají (403). `confirm()`/`alert()` z admin formulářů
  vykresluje `WebChromeClient` jako nativní dialog (ověřeno instrumentačním
  testem).
- **Nastavení** – nativní obrazovka, která vykresluje `SETTINGS_SPEC` přímo
  z backendu (v Kotlinu není druhá konfigurace). Uložení, obnovení výchozích
  hodnot a restart při změně, která to vyžaduje.
- Server drží na pozadí foreground služba (`ServerService`, typ `specialUse`),
  takže indexace neusne. Data (SQLite + logy) jsou v privátním úložišti
  aplikace (`filesDir`).

## Reálné embeddingy (ONNX, offline)

Aplikace **obsahuje skutečný vícejazyčný model**
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (revision
`e8f8c211226b894fcb81acc59f3b34ba3efd5f42`) ve formátu ONNX (qint8, arm64).
Inference běží v Kotlinu přes ONNX Runtime for Android, tokenizer je přesný
přepis XLM-R sentencepiece tokenizeru (`SpmTokenizer.kt`, shoda s referencí).

- Model i tokenizer se **stahují při buildu** a kontrolují se proti SHA-256;
  nikdy se nestahují za běhu a nic se neposílá ven.
- Python se na model napojuje přes `EmbeddingBridge` (statická metoda `get()`);
  vektorový prostor je označen `minilm-l12-v2:<revision>`, takže se staré
  hashovací vektory nikdy neporovnávají s vektory reálného modelu.
- Bez modelu (`-PskipEmbeddingModel`) se použije `_HashingEmbedding`, který je v
  UI i logu **výslovně označený jako náhradní a bez sémantiky**. FTS5 funguje
  vždy.
- `hnswlib` pro Android neexistuje; `vector_search()` proto použije přesné
  brute-force hledání přes numpy s omezenou pamětí (top-k halda).

## Import Wikipedie z dumpu

V aplikaci je akce **Import dumpu**: přes systémový výběr souborů vyberete
stažený `cswiki-*.xml.bz2`. `content://` URI **není** cesta k souboru, takže
`DumpImporter` nejdřív zkontroluje volné místo a zkopíruje proud do privátního
úložiště (`filesDir/wiki_dumps`); teprve pak `android_entry.import_wiki_dump()`
zaregistruje zdroj a spustí import na pozadí (idempotentně, s možností
pokračování). API importér funguje bez dumpu.

## Životní cyklus serveru

`ServerController` drží stavy `STOPPED`, `STARTING`, `RUNNING`, `STOPPING`,
`ERROR`. Do stavu `RUNNING` se přejde až po úspěšné HTTP kontrole `/health`
s timeoutem; samotné spuštění Python vlákna se za běžící server nepovažuje.
Start je idempotentní (rotace, návrat do aplikace ani opakované spuštění služby
nevytvoří druhý server, scheduler ani workery — `inFlight`/`state` hlídač).
Chaquopy se inicializuje **právě jednou za proces** (`PythonRuntime.ensureStarted`,
na pozadí a před použitím `Python.getInstance()`). `stop_server()` zastaví
příjem další práce, ukončí workery, scheduler i HTTP server (řiditelný server
s `shutdown`), udělá SQLite checkpoint a zavře spojení; nový start ve stejném
procesu funguje.

## Build

Potřebujete JDK 17, Android SDK (platform 34, build-tools, NDK). Gradle wrapper
je součástí repa (Gradle 8.7 s kontrolním součtem distribuce).

```bash
cd android
./gradlew :app:assembleDebug
# výsledek: app/build/outputs/apk/debug/app-debug.apk
```

### Dvě varianty APK

| Varianta | Model | Velikost | Poznámka |
|---|---|---|---|
| plná | reálný MiniLM (118 MB) | ~136,6 MiB | sémantické hledání, **nad limitem GitHubu (100 MB)** |
| hashovací | žádný (`-PskipEmbeddingModel`) | ~62,5 MiB | menší, jen FTS5 + hashovací fallback; vhodná ke commitu |

Obě varianty jsou v repozitáři **přes Git LFS** (`mini-search-7.9-debug.apk`,
`mini-search-7.9-nomodel-debug.apk`), protože i malá má přes 50 MB. GitHub má
tvrdý limit 100 MB na soubor v běžném gitu. Klon bez `git-lfs` dostane jen
pointer; obě varianty lze kdykoli sestavit jedním příkazem výše (model se při
buildu stáhne a ověří proti SHA-256).

Při prvním buildu se stáhnou dva artefakty z Hugging Face (model 118 MB a
`tokenizer.json`) a ověří se proti SHA-256. Pro offline build je lze dodat
předem:

```bash
./gradlew :app:assembleDebug \
    -PembeddingModel=/cesta/model_qint8_arm64.onnx \
    -PtokenizerJson=/cesta/tokenizer.json
# malé APK bez modelu (hashovací fallback):
./gradlew :app:assembleDebug -PskipEmbeddingModel
```

Primární cílová architektura je `arm64-v8a` (skutečné telefony). Pro běh na
emulátoru x86_64 se build dělá odděleně a transparentně:

```bash
./gradlew :app:assembleDebug -PskipEmbeddingModel -Pabi=x86_64
```

Chaquopy při prvním buildu stahuje Python balíčky z `repo.chaquo.com`. Cestu k
SDK nastavte do `android/local.properties` (`sdk.dir=...`).

### Testy

```bash
./gradlew :app:testDebugUnitTest            # JVM: tokenizer + ONNX vs reference
./gradlew :app:connectedDebugAndroidTest    # na zařízení: start serveru + nativní UI
```

Instrumentační sada má dvě části: `ServerSmokeTest` (spouští reálný Chaquopy
backend a kontroluje `/health`, `/`, `/admin`, stop/restart, LAN sdílení) a
`MainActivityUiTest` (čtyři nativní záložky a dialogy `confirm()`/`alert()`).

## Konfigurace

`android/requirements-android.txt` je seznam Python balíčků v APK. `extruct`
záměrně chybí (potřebuje `rdflib`/`jstyleson`, které nemají Android kola);
`_extract_jsonld()` se bez něj obejde vlastním parserem. `MINISEARCH_*`
proměnné se nastavují v `android_entry.py`; runtime nastavení zůstává v
`SETTINGS_SPEC` v `app_combined.py` (v Kotlinu není druhá konfigurace).

## Aktualizace a soubory v úložišti

Aplikace nikdy sama sebe neaktualizuje: `BUNDLED_RUNTIME=1` vypne kontrolu
aktualizací z Gitu i její ovládání v dashboardu a `ServerController`/Python
nikdy nespouští `git pull` ani `pip install`. Data, logy a pomocné soubory
(`.commit_sha`, `update_log`) leží v privátním úložišti vedle `console.db`.

## Aktualizace aplikace

`applicationId` je `cz.minisearch.app`. `versionCode` se zvýšil z 4 na 5
(`versionName` 7.8 → 7.9). Předchozí APK (7.8) byl podepsaný **jiným** debug
klíčem, než jaký je v tomto prostředí (7.9 je podepsaný klíčem s otiskem
`adf89e2efd7af8862ba9ee1889aad9dc1c88063de2ce64a98d3dfee529df4800`), takže
běžná aktualizace přes původní instalaci **nemusí být možná** (Android odmítne
APK s jiným podpisem). Odinstalace staré verze by smazala data a **není**
náhradou migrace; databázi nikdy nemažte. Release se podepisuje jen dodatečným
klíčem přes bezpečnou konfiguraci; klíče a hesla nejsou součástí repozitáře.

## Doklad o buildu a testech

Podrobný doklad (odevzdané APK, SHA-256, přesný postup z čistého checkoutu,
výsledky testů a seznam neověřených scénářů) je v `DODANI-ANDROID.md` v kořeni
repozitáře.

