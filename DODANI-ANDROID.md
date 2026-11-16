# Mini Search pro Android – dokončení, build a ověření

Tento dokument shrnuje, co bylo v rámci úkolu hotovo, jak přesně se APK sestaví
z čistého checkoutu a co je ověřené a co ne. Všechny údaje odpovídají skutečně
provedeným krokům; nic není odhad ani ukázka.

- Repozitář: `https://github.com/nekam13/mini-search`
- Pracovní větev: `android-app-completion`
- Zdrojový commit obou APK: **`7996c3e`** (`Android 7.9: nativní Clay UI,
  souběžné čtení a nové APK`)
- Výchozí větev `beta-optimized` byla ověřena na commitu `f39092a`; větev
  `android-app-completion` vychází z referenčního commitu zadání
  (`bf629cc`) a obsahuje dokončení Android aplikace.

> **Aktuální verze 7.9 (nativní UI).** Od 7.8 přibylo **nativní české UI**
> (obrazovky Domů / Hledat / Správa / Nastavení) místo jediného WebView:
> Domů ovládá server (start/stop/restart, LAN sdílení, import dumpu), Nastavení
> vykresluje `SETTINGS_SPEC` z backendu. Zůstává možnost sdílet **veřejnou
> vyhledávací stránku** v LAN (`network.lan_share`, výchozně vypnuto);
> administrace zůstává pouze na loopbacku. `versionCode` je `5`, `versionName`
> `7.9`. Podrobnosti v § 2 (sekce „Nativní UI") a § 6.

## 1. Odevzdané soubory

| Soubor | Varianta | Velikost | SHA-256 |
|---|---|---|---|
| `mini-search-7.9-debug.apk` | plná s modelem MiniLM (ONNX), **v repu přes Git LFS** | 143 214 586 B (~136,6 MiB) | `f9e3586d27e5a0f3cb6701e1b738bf29b53dd27c93ad4c452d5e71e9c3e3a2ab` |
| `mini-search-7.9-nomodel-debug.apk` | bez modelu (hashovací fallback), **v repu přes Git LFS** | 65 579 557 B (~62,5 MiB) | `7a7ad75e7fe7b35712143c37c8c75b086ac15c50496e2d73f25a9095724ddeb3` |

Metadata obou variant (shodná): `applicationId = cz.minisearch.app`,
`versionCode = 5`, `versionName = 7.9`, `minSdk = 30`, `targetSdk = 34`,
nativní ABI `arm64-v8a`. Debug podpis (certifikát SHA-256)
`adf89e2efd7af8862ba9ee1889aad9dc1c88063de2ce64a98d3dfee529df4800`.

Obě varianty jsou v repu **přes Git LFS** (i malá má přes 50 MB): GitHub má
tvrdý limit 100 MB na soubor v běžném gitu, takže 143MB APK je uložené jako
LFS objekt (`.gitattributes` → `filter=lfs`) a v gitu je jen malý pointer.
Klony bez nainstalovaného `git-lfs` dostanou jen pointer, proto:

```bash
git lfs install            # jednou na stroji
git clone https://github.com/nekam13/mini-search.git
```

Alternativně se obě varianty kdykoli sestaví jedním příkazem (viz § 4); model
se při buildu stáhne a ověří proti SHA-256. Obě varianty jsou skutečně
sestavené z aktuálního zdrojového commitu, nikoli převzaté z dřívějška.


## 2. Co bylo dokončeno

### Nativní UI (nová funkce 7.9)

Místo jediného WebView má aplikace **nativní české UI** se čtyřmi záložkami
(Horní lišta s přepínačem záložek, v duchu společného Clay designu):

- **Domů** (`view_home.xml`) – nativní ovládání serveru: stav
  (`STOPPED`/`STARTING`/`RUNNING`/`STOPPING`/`ERROR`), backend (jaký embedding
  běží), chybová zpráva s diagnostikou, tlačítka Start / Zastavit / Znovu
  spustit, přepínač „Sdílet v síti" (LAN) a zobrazení LAN adresy pro PC,
  tlačítko importu wiki dumpu.
- **Hledat** (`web_container`) – veřejná stránka `/` v uzamčeném WebView;
  WebView se vytváří líně, až když záložku poprvé otevřete (aby `onCreate`
  nezdržel start foreground služby).
- **Správa** – panel `/admin` ve stejném WebView. `confirm()`/`alert()`
  z admin formulářů vykresluje `WebChromeClient` jako nativní dialog; ověřeno
  instrumentačním testem `MainActivityUiTest` (dialog se zobrazí s českou
  zprávou, OK vrátí `confirm()`, Zrušit vrátí `cancel()`).
- **Nastavení** (`view_settings.xml`) – nativní obrazovka, která dostane
  seskupená nastavení z backendu (`settings_json()` → `SETTINGS_SPEC`) a
  vykreslí je; ukládání, obnovení výchozích hodnot a restart, když to změna
  vyžaduje. **V Kotlinu není druhá konfigurace** – jediným zdrojem pravdy
  zůstává `SETTINGS_SPEC` v `app_combined.py`.

Zachováno a doplněno u životního cyklu:

- **Idempotentní start.** `ServerController` má hlídač `inFlight` + `state`,
  takže rotace obrazovky, návrat do aplikace ani opakované spuštění služby
  nevytvoří druhý server, scheduler ani crawlery. `PythonRuntime.ensureStarted`
  spouští `Python.start()` **právě jednou za proces** a na pozadí (nikdy na
  hlavním vlákně).
- **`RUNNING` až po kontrole `/health`** s timeoutem (60 s); samotné vytvoření
  Python vlákna se za běžící server nepovažuje. Při selhání nastoupí `ERROR`
  s českou diagnostikou a tlačítko „Zkusit znovu".
- **Korektní stop a restart** ve stejném procesu: `stop_server()` zastaví
  příjem další práce, ukončí workery a scheduler, zavolá `shutdown()` na
  řiditelném HTTP serveru (Werkzeug `make_server`), udělá SQLite checkpoint a
  zavře spojení.

### Sdílení hledání v LAN (funkce 7.8)

Aplikace umí volitelně zpřístupnit **veřejnou vyhledávací stránku** ostatním
zařízením ve stejné síti (typicky PC), a to na pevném portu 8070, např.
`http://192.168.10.200:8070/`. Administrace zůstává přísně jen na zařízení.

- **Výchozí stav je vypnuto.** Server poslouchá na `127.0.0.1`, dokud uživatel
  sdílení nezapne. Nic se do sítě neotevírá samo.
- **Backend** (`app_combined.py`): nové nastavení `network.lan_share`
  (`MINISEARCH_LAN_SHARE`, `restart=True`) v `SETTINGS_SPEC`; `start_server()`
  při zapnutém sdílení vynutí bind `0.0.0.0`; `_lan_ip_addresses()` zjistí
  adresy stroje a `lan_share_url()` vrátí adresu pro PC (nebo prázdno, když
  žádná není); `server_status()` přidává `lan_share` a `lan_url`.
- **Ochrana administrace**: `_guard_admin_to_localhost()` odmítá jakýkoli
  požadavek na `/admin` (stránku i `/admin/api/**`) z neloopback adresy
  (403) — a to i při zapnutém sdílení. Sdílí se tak jen `/`, `/autocomplete`
  a `/health`, tedy čistě veřejné a nemutující cesty.
- **Android**: v `android_entry.py` funkce `set_lan_share()` / `lan_share_enabled()`
  a `status()` nese `lan_share` / `lan_url`; `ServerController` má `lanUrl`,
  `setLanShare()` a `refreshLanInfo()`; v horní liště je přepínač
  „Sdílet v síti", který po zapnutí **zobrazí adresu k otevření na PC**
  (text je označitelný pro zkopírování) a při nedostupné adrese zobrazí
  srozumitelnou výzvu. Přepnutí proběhne až když server běží.
- **Žádné nové oprávnění**: bind na `0.0.0.0` i zjištění adresy (UDP socket)
  nevyžadují `NEARBY_WIFI_DEVICES` ani polohu, protože se `WifiManager`
  nepoužívá.

Tato funkce je navíc k dosavadnímu dokončení aplikace (7.7), které řešilo
čtyři **skutečné chyby nalezené spuštěním aplikace na emulátoru**:

- **Start neblokuje hlavní vlákno (oprava reálného pádu).** `Python.start()`
  volaný přímo v `Application.onCreate()` blokoval hlavní vlákno ~17 s; služba
  pak nestihla zavolat `startForeground()` do 20 s a systém aplikaci zabil
  (`ForegroundServiceDidNotStartInTimeException`). Chaquopy se nyní inicializuje
  na pozadí (`PythonRuntime.kt`) a všichni konzumenti čekají na latch.
  `ServerService.onStartCommand` volá `startForeground()` jako první věc.
- **Temný režim už nezobrazuje bílé UI na bílém.** Téma bylo
  `Theme.AppCompat.DayNight.NoActionBar` s pevně bílým oknem, ale Clay CSS je
  jen světlé; v temném režimu vznikalo bílé pole (lišta) a ztmavený WebView.
  Téma je nyní jen světlé, okno odpovídá `#F5F5F5` a algoritmické ztmavování
  WebView je vypnuté.
- **FTS5 fallback pro Chaquopy.** Vestavěné SQLite v Chaquopy nemá FTS5
  (`no such module: fts5`). `get_db()` nyní FTS5 testuje a při jeho absenci
  použije tabulku `pages_fts_fallback` se skládáním diakritiky
  (`minisearch_fold` SQL funkce), takže české hledání funguje i bez FTS5.
  Vektorové hledání se tím nevypíná.
- **Timezone fallback.** Scheduler padal na chybějící `tzdata`
  (`ZoneInfoNotFoundError`) a blokoval start serveru; nyní spadne na UTC a start
  nikdy neblokuje.
- **Komprese modelu.** Model v APK je deflate-komprimovaný, takže `openFd()`
  vždy selhal a plná varianta tiše degradovala na hashovací fallback; kopie
  nyní streamuje z assetů s kontrolou velikosti.
- **Pád rendereru WebView už neshodí aplikaci.** `onRenderProcessGone` znovu
  načte lokální stránku místo smrti celého procesu.

Zachované vlastnosti z předchozí dodávky:

- **Životní cyklus serveru** – `ServerController` (Kotlin) drží stavy
  `STOPPED`, `STARTING`, `RUNNING`, `STOPPING`, `ERROR`. Do `RUNNING` se přejde
  až po úspěšné HTTP kontrole `/health` s timeoutem; vytvoření Python vlákna se
  za běžící server nepovažuje. Start je idempotentní, `stop_server()` ukončí
  workery, scheduler i HTTP server, udělá SQLite checkpoint a zavře spojení,
  takže nový start ve stejném procesu funguje.
- **Foreground služba** – `ServerService` typu `specialUse` (nikoli `dataSync`),
  s trvalou notifikací, otevřením aplikace a akcí Zastavit. Po výslovném
  zastavení uživatelem se aplikace sama neobnoví (`UserState.serverEnabled`).
- **Reálné embeddingy** – `paraphrase-multilingual-MiniLM-L12-v2` (pinned
  revision `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`) běží na zařízení přes
  ONNX Runtime for Android (`EmbeddingBridge.kt`), tokenizer je přesný přepis
  XLM-R sentencepiece (`SpmTokenizer.kt`). Vektorový prostor je označen
  `minilm-l12-v2:<revision>`; hashovací vektory (`hash:384`) se s ním nikdy
  neporovnávají.
- **Bezpečnost** – administrace je jen na loopbacku (`MINISEARCH_ADMIN_LOCAL_ONLY=1`),
  mutující administrační akce jsou **POST-only** (GET vrací 405), cross-site
  požadavky s cizím `Origin`/`Referer` se odmítají (403). Z APK je vypnutá
  kontrola aktualizací z gitu i její mutační endpointy. Cleartext je povolen
  jen pro `127.0.0.1`/`localhost` (`network_security_config.xml`). TLS se nikde
  neobchází.

## 3. Výsledky testů (skutečně spuštěné)

### Backendové testy (všech 18 skriptů)

Spuštěno `python3 tests/<skript>.py` pro každý skript; všechny prošly (exit 0):

| Skript | PASS |
|---|---|
| `test_db.py` | 9 |
| `test_admin.py` | 8 |
| `test_admin_panel.py` | 68 |
| `test_discovery_integration.py` | 16 |
| `test_search_page.py` | 52 |
| `test_source_indexing.py` | 31 |
| `test_local_sites.py` | 50 |
| `test_local_indexing_smoke.py` | 1 (běží reálný HTTP server) |
| `test_wiki_import.py` | 42 |
| `test_crawl_metadata.py` | 37 |
| `test_rich_cards.py` | 50 |
| `test_maintenance.py` | 46 |
| `test_crawler_discovery.py` | 27 |
| `test_search_quality.py` | 44 |
| `test_settings_images.py` | 45 |
| `test_lifecycle.py` | 42 |
| `test_android_compat.py` | 5 |
| `test_lan_share.py` | 31 |

Testy životního cyklu (`test_lifecycle.py`) pokrývají start → `RUNNING` až po
`/health`, opakovaný start jako no-op bez druhého serveru/scheduleru/workerů,
stop a restart ve stejném procesu, zachování dat, oddělení vektorových prostorů
a obnovu přerušené fronty.

Nový `test_android_compat.py` (5 testů) ověřuje přesně to, co je specifické pro
Android: detekci FTS5, fallback tabulku s triggery, skládání diakritiky v LIKE
fallbacku, start/stop/restart bez FTS5 a start bez timezone.

Nový `test_lan_share.py` (30 testů) ověřuje sdílení hledání v LAN: výchozí stav
je vypnutý a server zůstává na loopbacku; po zapnutí se bindne na `0.0.0.0`,
`lan_share_url()` vrátí adresu a `server_status()` hlásí `lan_share: true`;
požadavek s **cizím veřejným `Origin`** se odmítne (403), ale požadavek
s LAN `Origin` (`http://10.2.135.153:8070`) projde; administrace zůstává jen na
loopbacku; vypnutí vrátí bind zpět. Test obsahuje i skutečný HTTP požadavek na
LAN adresu.

### Android JVM testy

`./gradlew :app:testDebugUnitTest` – `SpmTokenizerTest` (6 testů) a
`EmbeddingBridgeTest` (1 test), **7 testů, 0 selhání, 0 přeskočeno**. Ověřují
shodu tokenizeru s referencí a embedding výstupu (mean pooling, L2 normalizace)
proti referenčním vektorům `sentence-transformers` na českých textech.

### Android instrumentační testy (skutečně spuštěné na emulátoru)

`./gradlew :app:connectedDebugAndroidTest` (ekvivalentně `adb shell am instrument
-w cz.minisearch.app.test/androidx.test.runner.AndroidJUnitRunner`) na emulátoru
`msx(AVD) - 14`: **12 testů, 0 selhání** (úspěšný běh na odevzdaném `arm64-v8a`
APK). Tvoří je `ServerSmokeTest` (8) a `MainActivityUiTest` (4).

`ServerSmokeTest.kt` spouští skutečný Chaquopy backend a ověřuje:

- `serverBecomesReadyAndServesHealth` – start, `state == RUNNING`, `/health` 200
- `startIsIdempotentAndDoesNotDuplicateWorkers` – druhý start nezmění počet
  workerů
- `searchAndAdminPagesLoad` – `/` i `/admin` vrací 200
- `stopAndRestartWorkInTheSameProcess` – stop a nový start ve stejném procesu
- `databaseLivesInPrivateStorage` – `console.db` je ve `filesDir`
- `pagesSurviveStopAndRestart` – restart neztratí indexované stránky
- `mutatingAdminRoutesRejectGet` – GET na mutační admin endpointy není 200
- `lanSharingRebindsAndExposesTheAddress` – sdílení je výchozně vypnuté; po
  zapnutí `status` hlásí `lan_share: true` a `lan_url`; přes syrový socket se
  ověří, že `/` na adrese zařízení vrací 200 a `/admin` na téže adrese vrací
  403 (tedy že server na LAN skutečně odpovídá a administrace se přesto odmítá).

`MainActivityUiTest.kt` (nové) ověřuje nativní UI a dialogy:

- `nativeTabsArePresentAndSwitchScreens` – čtyři záložky (Domů/Hledat/Správa/
  Nastavení) existují s českými popisky a přepnutí na Správu schová Domů a
  zobrazí webový kontejner.
- `confirmDialogShowsAndReportsConfirm`, `cancelButtonReportsCancel`,
  `alertDialogHasOnlyOkButton` – `MainActivity.buildJsDialog(...)` (přesně to,
  co volá `WebChromeClient.onJsConfirm`/`onJsAlert`) zobrazí dialog s českou
  zprávou; OK vrátí `confirm()`, Zrušit vrátí `cancel()`, alert nabídne jen OK.

> Poznámka k dialogům: testy záměrně **nepřipojují DevTools/CDP klienta**.
> Připojený DevTools klient totiž sám zachytí `window.confirm()`/`alert()`, takže
> `onJsConfirm` se nikdy nezavolá a nativní dialog se neukáže — přes CDP tedy
> dialog ověřit nelze. Test proto volá reálnou metodu bez CDP a kliká na
> skutečné tlačítko `AlertDialog` na hlavním vlákně (kvůli pomalému emulátoru
> bez KVM se místo Espresso čekání použije `waitForIdleSync`).

Poznámka k `searchAndAdminPagesLoad`: první dotaz na `/admin` na emulátoru bez
KVM má studený start ~30 s (druhý už 0,7 s), proto test používá timeout 30 s;
jde o omezení pomalého emulátoru, nikoli o regresi aplikace.

### End-to-end ověření na emulátoru (mimo testy)

> Poznámka k aktuálnosti: kroky 1–5 níže byly provedeny v rámci dřívějšího cyklu
> (7.8) na stejném typu emulátoru. V tomto cyklu (7.9) se přes `adb forward`
> ruční tok **nepodařilo znovu dokončit** — emulátor bez KVM má studený start
> Pythonu tak pomalý, že ho systém opakovaně ukončil (zátěž 15–20). Stejný tok
> ale pokrývají instrumentační testy, které v tomto cyklu **prošly 12/12 na
> odevzdaném `arm64-v8a` APK** a provádějí skutečné HTTP požadavky na běžící
> server (`/health`, `/`, `/admin`, syrový socket na LAN adrese).

Nad spuštěnou aplikací byl přes `adb forward` ověřen skutečný tok:

1. `POST /admin/api/sources` s `{"source_type":"url","url":"http://10.0.2.2:8099/"}`
   → zdroj vytvořen a **automaticky zařazen k indexaci** (201).
2. Crawler českou stránku stáhl a zaindexoval (`?q=praha` → `result-item`
   s „Kouzelná Praha", „Pražský hrad").
3. `?q=cesky` (bez diakritiky) vrátil „Český"/„český" → **skládání diakritiky
   v FTS5 fallbacku funguje na zařízení**.
4. `/health` = 200, `state = RUNNING`, `scheduler_running = true`,
   `embedding_space = hash:384` (nomodel varianta).
5. `console.db` vznikl v privátním `files/`.

## 4. Postup buildu z čistého checkoutu

Potřebné: **JDK 17** (build proběhl i na JDK 21), **Android SDK** (platform 34,
build-tools 34.0.0, NDK 26.1.10909125). Gradle wrapper je součástí repa
(Gradle 8.7 s kontrolním součtem distribuce
`544c35d6bd849ae8a5ed0bcea39ba677dc0f49df7d1835561582da2009b961d`).

```bash
git lfs install            # kvůli plné variantě APK
git clone https://github.com/nekam13/mini-search.git
cd mini-search
git checkout android-app-completion

cd android
echo "sdk.dir=/cesta/k/android-sdk" > local.properties

# plná varianta s reálným modelem (stáhne a ověří model i tokenizer):
./gradlew :app:assembleDebug
# -> app/build/outputs/apk/debug/app-debug.apk

# malá varianta bez modelu (hashovací fallback):
./gradlew :app:assembleDebug -PskipEmbeddingModel
# -> app/build-nomodel/outputs/apk/debug/app-debug.apk

# testy:
./gradlew :app:testDebugUnitTest
./gradlew :app:connectedDebugAndroidTest
```

Při prvním plném buildu se stáhnou dva artefakty z Hugging Face (model
`model_qint8_arm64.onnx` a `tokenizer.json`) a ověří se proti SHA-256. Offline
build je možný přes `-PembeddingModel=/cesta/model.onnx
-PtokenizerJson=/cesta/tokenizer.json`. Verze závislostí jsou připnuté
(AGP 8.5.2, Kotlin 1.9.24, Chaquopy 17.0.0, Python 3.13).

Pro reprodukovatelnější výstup lze přidat `SOURCE_DATE_EPOCH=0` před gradle;
odstraní časové značky v `app.imy` (viz § 8). Obě varianty jsou sestavené
z pracovní kopie na aktuálním commitu větve `android-app-completion` bez ručních
oprav a bez absolutních cest v repu.

## 5. Ověřená funkčnost a co ověřeno nebylo

**Ověřeno (skutečně spuštěno):**
- Build obou variant projde (`BUILD SUCCESSFUL`) a výsledné APK mají shodná
  metadata (`versionCode 5`, `versionName 7.9`).
- V plné variantě jsou v APK `assets/embedding/model_qint8_arm64.onnx`
  (118 412 398 B), `assets/ml/tokenizer_spm.bin`, `lib/arm64-v8a/libonnxruntime.so`,
  `lib/arm64-v8a/libonnxruntime4j_jni.so` a `assets/chaquopy/app.imy`.
- V malé variantě model **není**, tokenizer ano.
- `templates/` a `static/` jsou zabalené v `app.imy` (`templates/search.html`,
  `templates/base_clay.html`, `static/css/clay.css`, `static/js/search.js`).
- 18 backendových sad, 7 JVM testů a 12 instrumentačních testů prochází.
- **Nativní UI na odevzdaném `arm64-v8a` APK.** `MainActivityUiTest` ověřil na
  zařízení, že existují čtyři záložky s českými popisky a že přepnutí na Správu
  schová Domů a zobrazí webový kontejner; dialogy `confirm()`/`alert()`
  (co volá `WebChromeClient`) se zobrazí s českou zprávou a OK/Zrušit vrací
  správnou hodnotu. V logu (`ServerController`) je vidět skutečný přechod do
  `state -> RUNNING` s `workers: 1`, `scheduler_running: true`,
  `embedding_space: "hash:384"` (nomodel varianta) a platným `lan_url`.
- Instalace a běh **nového APK** na emulátoru `msx(AVD) - 14`: server se rozjede,
  `/health` = 200, české hledání s diakritikou i bez ní funguje, crawling
  a automatická indexace nového zdroje fungují, administrace se načte.
- **Plná varianta s reálným ONNX modelem na zařízení.** Emulátor má ARM
  překlad (`libndk_translation.so`, `ro.enable.native.bridge.exec=1`), takže
  i `arm64-v8a` APK se nainstaluje a spustí. Ověřeno:
  - `EmbeddingBridge: embedding model installed (118412398 bytes)` a soubor
    `minilm-l12-v2.onnx` (118 412 398 B) vznikl v privátním `files/` →
    oprava komprimovaného assetu funguje.
  - `/health` hlásí `embedding_space = minilm-l12-v2:e8f8c211226b894fcb81acc59f3b34ba3efd5f42`
    a `embeddings = model (paraphrase-multilingual-MiniLM-L12-v2)`, tedy
    **skutečný model, ne hash**.
  - **Sémantické hledání** bez doslovného překryvu: dotazy „sídlo prezidenta",
    „turistická atrakce", „cestovní ruch" (tato slova se na stránce
    nevyskytují) vrátily stránku o Praze; `?q=hlavní město České republiky`
    vrátilo výsledek, i když stránka text „hlavní město" neobsahuje.
- Světlý i tmavý režim: pozadí `#F5F5F5`, text lišty čitelný (ověřeno
  screenshotem v obou režimech).
- **Sdílení hledání v LAN na zařízení (7.8).** Po zapnutí `network.lan_share`
  přes admin API a restartu aplikace `/health` hlásí `host = 0.0.0.0`,
  `lan_share = true`, `lan_url = http://10.0.2.16:8070/`. UI na obrazovce
  ukazuje přepínač „Sdílet v síti" a text **„Otevřete na PC:
  http://10.0.2.16:8070/"** (ověřeno přes `uiautomator dump`, tj. skutečný
  widget, ne jen logika). Současně `/admin` přes loopback vrací 200 a přes LAN
  adresu 403 (ověřeno instrumentačním testem raw socketem na zařízení).
  Výchozí stav po instalaci je vypnuto (`lan_share = false`, `host =
  127.0.0.1`).

**Neověřeno (přesné důvody):**
- **Stabilita startu na extrémně pomalém emulátoru.** Emulátor bez KVM má
  studený start Python interpretu přes ARM překlad tak pomalý, že občas
  překročí i 120s timeout `PythonRuntime` (v logu pak `ERROR: Python se
  nepodařilo inicializovat do 120 s`). Na tomto emulátoru se to stává
  nepravidelně (jeden běh instrumentačních testů projde 12/12, jiný start
  trvá mnoho minut); jde o omezení emulátoru bez virtualizace, ne o chybu
  aplikace — při úspěšném startu server naběhne správně (`state -> RUNNING`).
  Na reálném telefonu je start řádově rychlejší.
- **Otevření LAN adresy z reálného PC v téže Wi-Fi.** Sdílení je ověřené na
  úrovni serveru (bind `0.0.0.0`, `lan_url`, odpověď na LAN adrese zařízení
  přes raw socket, odmítnutí adminu) a backendový test `test_lan_share.py`
  poslal skutečný HTTP požadavek z adresy `10.2.135.153`. Chybí ale fyzické PC
  ve stejné síti jako telefon; emulátor žije v NATu (`10.0.2.x`), takže
  mezizařízení v jedné Wi-Fi nebylo možné nasimulovat věrně.
- **Fyzické arm64 zařízení.** Ověření proběhlo na emulátoru s ARM překladem
  (`msx(AVD) - 14`), který spouští `arm64-v8a` binárky. Běh na skutečném
  telefonu (jiný SoC, jiná paměť, skutečná baterie) zůstává neověřený.
- **Shoda embeddingů na zařízení proti referenci.** Na zařízení bylo ověřeno,
  že model běží a sémanticky vyhledává; přesná shoda vektorů s referenčním
  `sentence-transformers` je ověřená jen JVM testem (`EmbeddingBridgeTest`) na
  desktopu.
- **Pád rendereru WebView na emulátoru** – renderer na emulátoru bez KVM
  opakovaně padá na `SIGTRAP` (sandbox WebView, omezení prostředí, nikoli
  aplikace). Aplikace díky `onRenderProcessGone` nespadne, ale vykreslení
  stránky v emulátoru je tím poznamenané. Obsah stránek je ověřen přes HTTP.
- **Podpisová kompatibilita s předchozí instalací** – viz § 7.

## 6. Bezpečnostní opatření (souhrn)

- HTTP server poslouchá na `127.0.0.1`; LAN indexování je jiná věc než
  zpřístupnění aplikace do LAN.
- **Volitelné sdílení hledání v LAN** (`network.lan_share`, výchozně vypnuto):
  po zapnutí se bindne `0.0.0.0` a zveřejní se **jen veřejné cesty** (`/`,
  `/autocomplete`, `/health`). Administrace se na LAN adrese odmítá (403) — na
  zařízení ověřeno raw socketem.
- `/admin` a `/admin/api/**` jsou dostupné jen z loopbacku; navíc se odmítají
  mutující požadavky s cizím `Origin`/`Referer` (403). Požadavek s `Origin`
  z vlastní LAN adresy projde (kvůli WebView/stejnému zařízení), cizí veřejný
  `Origin` se odmítá.
- Všech sedm dříve GET-mutujících akcí (recrawl, recrawl-all, pause-all,
  resume-all, delete, check-update, apply-update) je nyní POST-only; GET vrací 405.
- Z APK jsou vypnuté git aktualizace i jejich mutační endpointy
  (`BUNDLED_RUNTIME=1`); aplikace nikdy nespouští `git pull` ani `pip install`.
- WebView: JS ano (Clay UI), ale `allowFileAccess=false`,
  `allowContentAccess=false`, žádný JavaScript most, externí odkazy se otevírají
  v systémovém prohlížeči, `MIXED_CONTENT_NEVER_ALLOW`. Cleartext je povolen
  **jen** pro `127.0.0.1`/`localhost` (`network_security_config.xml`); proto se
  LAN adresa nikdy nenačítá v privilegovaném WebView.
- TLS se nikde nevypíná (žádné `verify=False`, trust-all ani obcházení).
- Secret key se generuje za běhu (`os.urandom`), v repu není pevný klíč.

## 7. Aktualizace a podpis

`applicationId` zůstal `cz.minisearch.app` a `versionCode` se zvýšil z 4 na 5
(`versionName` 7.8 → 7.9). Debug APK je podepsané automatickým debug klíčem
(otisk certifikátu SHA-256:
`adf89e2efd7af8862ba9ee1889aad9dc1c88063de2ce64a98d3dfee529df4800`).
**Předchozí 7.8 APK bylo podepsané jiným debug klíčem**
(`271e48cb…`) než toto prostředí, proto běžná aktualizace přes původní instalaci
nemusí být možná (Android odmítne APK s jiným podpisem). Odinstalace staré verze
by smazala data a **není** náhradou migrace; databázi nikdy nemažte. Release se
podepisuje jen dodatečným klíčem přes bezpečnou konfiguraci; klíče a hesla
nejsou součástí repozitáře a žádný produktivně podepsaný release není tvrzen.

## 8. Reprodukovatelnost

Oba buildy prošly opakovaně; výsledek je **obsahově shodný**:

- V plné variantě má zabalený model přesně SHA-256
  `783fea82d71a58179b830a4dbd2d58447e640609e98eedf9ffa12622d375a672` (shodný
  s připnutou hodnotou v `build.gradle`), tokenizer i nativní knihovny
  `libonnxruntime*.so` jsou přítomné.
- V obou variantách jsou `templates/` i `static/` v `app.imy` a obsahují
  opravené POST formuláře; Python kód nese symboly `lan_share` / `lan_share_url`.
- Těla přeložených modulů (`app_combined.pyc`, `android_entry.pyc`) jsou
  **bajtově shodná** mezi buildy i mezi variantami; liší se jen 4bajtová
  hlavička s časem překladu.

Bajtová shoda celého APK není zaručena: ZIP záznamy nesou časové značky a
`classes*.dex` nese D8 debug checksum tabulku (hash kódů, nikoli kód samotný —
`dexdump` bez hlavičky je mezi buildy identický). Ověřená je proto **obsahová**
reprodukovatelnost, ne bit-identický soubor. Build s `SOURCE_DATE_EPOCH=0`
odstraní časové značky v `app.imy`, ale `classes3.dex` zůstává závislý na
pořadí hashů v D8; to je vlastnost AGP/D8, ne našeho kódu.

## 9. Omezení prostředí během práce

- Emulátor `msx(AVD) - 14` běží **bez `/dev/kvm`** (hostitel x86_64 bez
  virtualizace), takže je velmi pomalý a ANR; to je důvod pádů rendereru WebView,
  občasného překročení 120s timeoutu startu Pythonu a nutnosti delších timeoutů
  u instrumentačních testů. Zátěž guest systému dosahovala 16–20 a systém sám
  hlásil ANR (např. „Process system isn't responding"). Prostředí je proto
  vhodné pro jednorázové ověření, ne pro opakované interaktivní testování.
- Emulátor má zapnutý **ARM překlad** (`libndk_translation.so`,
  `ro.enable.native.bridge.exec=1`), proto na něm běží i `arm64-v8a` APK včetně
  ONNX modelu. Instrumentační testy i ruční ověření běžely přímo na tomto
  `arm64-v8a` buildu (stejné APK jako odevzdané), takže testují skutečný
  produkční artefakt, ne separátní x86_64 variantu. Separátní `x86_64` varianta
  (`-Pabi=x86_64`) sloužila jen jako pomocník při ladění UI a **není** součástí
  odevzdání.
- Toolchain: JDK 21 (`/usr/lib/jvm/java-21-openjdk-amd64`), Gradle 8.7,
  `ANDROID_HOME=/home/openhands/android-sdk` (platform 34, build-tools 34.0.0).
- Databáze `console.db` v repu se nikdy needitovala ani nemazala.
