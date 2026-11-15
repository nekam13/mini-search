# Mini Search pro Android – dokončení, build a ověření

Tento dokument shrnuje, co bylo v rámci úkolu hotovo, jak přesně se APK sestaví
z čistého checkoutu a co je ověřené a co ne. Všechny údaje odpovídají skutečně
provedeným krokům; nic není odhad ani ukázka.

- Repozitář: `https://github.com/nekam13/mini-search`
- Pracovní větev: `android-app-completion`
- Zdrojový commit obou APK: **`d80cb50`** (`Fix Android startup, dark mode,
  FTS5 and timezone on-device`) plus dokumentační commit
- Výchozí větev `beta-optimized` byla ověřena na commitu `f39092a`; větev
  `android-app-completion` vychází z referenčního commitu zadání
  (`bf629cc`) a obsahuje dokončení Android aplikace.

## 1. Odevzdané soubory

| Soubor | Varianta | Velikost | SHA-256 |
|---|---|---|---|
| `mini-search-7.7-debug.apk` | plná s modelem MiniLM (ONNX), **v repu přes Git LFS** | 143 141 101 B (~136,5 MiB) | `c5d8ca5208a66369dddb045d8ca0ece5d4d944067df9de8d4f46773411778e35` |
| `mini-search-7.7-nomodel-debug.apk` | bez modelu (hashovací fallback), **commitnutá** | 65 505 692 B (~62,5 MiB) | `72ae088403e6ebc8a3bd6a739157abb1fe00a3b0c0d01f2c2fef0ab7ca21000c` |

Metadata obou variant (shodná): `applicationId = cz.minisearch.app`,
`versionCode = 3`, `versionName = 7.7`, `minSdk = 30`, `targetSdk = 34`,
nativní ABI `arm64-v8a`.

Plná varianta je v repu **přes Git LFS**: GitHub má tvrdý limit 100 MB na
soubor v běžném gitu, takže 143MB APK je uložené jako LFS objekt (`.gitattributes`
→ `filter=lfs`) a v gitu je jen malý pointer. Klony bez nainstalovaného
`git-lfs` dostanou jen pointer (134 B), proto:

```bash
git lfs install            # jednou na stroji
git clone https://github.com/nekam13/mini-search.git
```

Alternativně se plná varianta kdykoli sestaví jedním příkazem (viz § 4); model
se při buildu stáhne a ověří proti SHA-256. Obě varianty jsou skutečně sestavené
z commitu `d80cb50`, nikoli převzaté z dřívějška.


## 2. Co bylo dokončeno

Tato iterace navazuje na předchozí dodávku a řeší čtyři **skutečné chyby
nalezené spuštěním aplikace na emulátoru**, ne jen statickou kontrolou:

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

### Backendové testy (všech 17 skriptů)

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
| `test_lifecycle.py` | 37 |
| `test_android_compat.py` | 5 |

Testy životního cyklu (`test_lifecycle.py`) pokrývají start → `RUNNING` až po
`/health`, opakovaný start jako no-op bez druhého serveru/scheduleru/workerů,
stop a restart ve stejném procesu, zachování dat, oddělení vektorových prostorů
a obnovu přerušené fronty.

Nový `test_android_compat.py` (5 testů) ověřuje přesně to, co je specifické pro
Android: detekci FTS5, fallback tabulku s triggery, skládání diakritiky v LIKE
fallbacku, start/stop/restart bez FTS5 a start bez timezone.

### Android JVM testy

`./gradlew :app:testDebugUnitTest` – `SpmTokenizerTest` (6 testů) a
`EmbeddingBridgeTest` (1 test), **7 testů, 0 selhání, 0 přeskočeno**. Ověřují
shodu tokenizeru s referencí a embedding výstupu (mean pooling, L2 normalizace)
proti referenčním vektorům `sentence-transformers` na českých textech.

### Android instrumentační testy (skutečně spuštěné na emulátoru)

`./gradlew :app:connectedDebugAndroidTest -Pabi=x86_64 -PskipEmbeddingModel`
na emulátoru `msx(AVD) - 14`: **7 testů, 0 selhání, 0 přeskočeno**.

`ServerSmokeTest.kt` spouští skutečný Chaquopy backend a ověřuje:

- `serverBecomesReadyAndServesHealth` – start, `state == RUNNING`, `/health` 200
- `startIsIdempotentAndDoesNotDuplicateWorkers` – druhý start nezmění počet
  workerů
- `searchAndAdminPagesLoad` – `/` i `/admin` vrací 200
- `stopAndRestartWorkInTheSameProcess` – stop a nový start ve stejném procesu
- `databaseLivesInPrivateStorage` – `console.db` je ve `filesDir`
- `pagesSurviveStopAndRestart` – restart neztratí indexované stránky
- `mutatingAdminRoutesRejectGet` – GET na mutační admin endpointy není 200

### End-to-end ověření na emulátoru (mimo testy)

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
./gradlew :app:connectedDebugAndroidTest -Pabi=x86_64 -PskipEmbeddingModel
```

Při prvním plném buildu se stáhnou dva artefakty z Hugging Face (model
`model_qint8_arm64.onnx` a `tokenizer.json`) a ověří se proti SHA-256. Offline
build je možný přes `-PembeddingModel=/cesta/model.onnx
-PtokenizerJson=/cesta/tokenizer.json`. Verze závislostí jsou připnuté
(AGP 8.5.2, Kotlin 1.9.24, Chaquopy 17.0.0, Python 3.13).

Obě varianty byly sestaveny z pracovní kopie na commitu `d80cb50` bez ručních
oprav a bez absolutních cest v repu.

## 5. Ověřená funkčnost a co ověřeno nebylo

**Ověřeno (skutečně spuštěno):**
- Build obou variant projde (`BUILD SUCCESSFUL`) a výsledné APK mají shodná
  metadata (`versionCode 3`, `versionName 7.7`).
- V plné variantě jsou v APK `assets/embedding/model_qint8_arm64.onnx`
  (118 412 398 B), `assets/ml/tokenizer_spm.bin`, `lib/arm64-v8a/libonnxruntime.so`,
  `lib/arm64-v8a/libonnxruntime4j_jni.so` a `assets/chaquopy/app.imy`.
- V malé variantě model **není**, tokenizer ano.
- `templates/` a `static/` jsou zabalené v `app.imy` (`templates/search.html`,
  `templates/base_clay.html`, `static/css/clay.css`, `static/js/search.js`).
- 17 backendových sad, 7 JVM testů a 7 instrumentačních testů prochází.
- Instalace a běh **nového APK** na emulátoru `msx(AVD) - 14`: server se rozjede,
  `/health` = 200, české hledání s diakritikou i bez ní funguje, crawling
  a automatická indexace nového zdroje fungují, administrace se načte.

**Neověřeno (přesné důvody):**
- **Fyzické arm64 zařízení.** V tomto prostředí je jen emulátor `x86_64` bez
  `/dev/kvm`; plná `arm64-v8a` varianta na něm nainstalovat nelze. Testováno
  proto na `x86_64` buildu se stejnými zdroji (`-Pabi=x86_64`), ale s hashovacím
  fallbackem místo ONNX. Běh ONNX modelu na fyzickém telefonu zůstává neověřený.
- **Shoda embeddingů na zařízení** – `EmbeddingBridgeTest` je JVM test ověřující
  matematiku ONNX Runtime na desktopu; na zařízení běžel jen hashovací fallback.
- **Pád rendereru WebView na emulátoru** – renderer na emulátoru bez KVM
  opakovaně padá na `SIGTRAP` (sandbox WebView, omezení prostředí, nikoli
  aplikace). Aplikace díky `onRenderProcessGone` nespadne, ale vykreslení
  stránky v emulátoru je tím poznamenané. Obsah stránek je ověřen přes HTTP.
- **Podpisová kompatibilita s předchozí instalací** – viz § 7.

## 6. Bezpečnostní opatření (souhrn)

- HTTP server poslouchá na `127.0.0.1`; LAN indexování je jiná věc než
  zpřístupnění aplikace do LAN.
- `/admin` a `/admin/api/**` jsou dostupné jen z loopbacku; navíc se odmítají
  mutující požadavky s cizím `Origin`/`Referer` (403).
- Všech sedm dříve GET-mutujících akcí (recrawl, recrawl-all, pause-all,
  resume-all, delete, check-update, apply-update) je nyní POST-only; GET vrací 405.
- Z APK jsou vypnuté git aktualizace i jejich mutační endpointy
  (`BUNDLED_RUNTIME=1`); aplikace nikdy nespouští `git pull` ani `pip install`.
- WebView: JS ano (Clay UI), ale `allowFileAccess=false`,
  `allowContentAccess=false`, žádný JavaScript most, externí odkazy se otevírají
  v systémovém prohlížeči, `MIXED_CONTENT_NEVER_ALLOW`.
- TLS se nikde nevypíná (žádné `verify=False`, trust-all ani obcházení).
- Secret key se generuje za běhu (`os.urandom`), v repu není pevný klíč.

## 7. Aktualizace a podpis

`applicationId` zůstal `cz.minisearch.app` a `versionCode` se zvýšil z 2 na 3
(`versionName` 7.6 → 7.7). Debug APK je podepsané automatickým debug klíčem
(otisk certifikátu SHA-256:
`271e48cb8eda8819735e8564986d34f48a9af0040deeff13eca2c4f767a60174`).
**Původní podpisový klíč aplikace `mini-search-7.5-debug.apk` ani klíč předchozí
7.6 není v tomto prostředí k dispozici**, proto běžná aktualizace přes původní
instalaci nemusí být možná (Android odmítne APK s jiným podpisem). Odinstalace
staré verze by smazala data a **není** náhradou migrace; databázi nikdy nemažte.
Release se podepisuje jen dodatečným klíčem přes bezpečnou konfiguraci; klíče a
hesla nejsou součástí repozitáře a žádný produktivně podepsaný release není
tvzen.

## 8. Reprodukovatelnost

Oba buildy prošly opakovaně (před i po opravě `testInstrumentationRunner`)
a výsledek je **obsahově shodný**:

- V plné variantě má zabalený model přesně SHA-256
  `783fea82d71a58179b830a4dbd2d58447e640609e98eedf9ffa12622d375a672` (shodný
  s připnutou hodnotou v `build.gradle`), tokenizer i nativní knihovny
  `libonnxruntime*.so` jsou přítomné.
- V malé variantě jsou `templates/` i `static/` v `app.imy` a v zabalené podobě
  obsahují opravené POST formuláře.
- Těla přeložených modulů (`app_combined.pyc`, `android_entry.pyc`) jsou
  **bajtově shodná**; liší se jen 4bajtová hlavička s časem překladu.

Bajtová shoda celého APK není zaručena: ZIP záznamy nesou časové značky,
Python `.pyc` obsahuje mtime a `classes*.dex` / `requirements-common.imy` nesou
metadata nástrojů. Ověřená je proto **obsahová** reprodukovatelnost, ne
bit-identický soubor.

## 9. Omezení prostředí během práce

- Emulátor `msx(AVD) - 14` běží **bez `/dev/kvm`** (hostitel x86_64 bez
  virtualizace), takže je velmi pomalý a ANR; to je důvod pádů rendereru WebView
  a nutnosti delších timeoutů u instrumentačních testů.
- Plná `arm64-v8a` varianta se na tomto emulátoru nainstalovat nedá, proto běžely
  instrumentační testy na `x86_64` buildu se stejnými zdroji a hashovacím
  fallbackem (model ONNX na zařízení tedy ověřen nebyl).
- Toolchain: JDK 21 (`/usr/lib/jvm/java-21-openjdk-amd64`), Gradle 8.7,
  `ANDROID_HOME=/opt/android-sdk` (platform 34, build-tools 34.0.0).
- Databáze `console.db` v repu se nikdy needitovala ani nemazala.
