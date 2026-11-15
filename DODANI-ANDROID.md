# Mini Search pro Android – dokončení, build a ověření

Tento dokument shrnuje, co bylo v rámci úkolu hotovo, jak přesně se APK sestaví
z čistého checkoutu a co je ověřené a co ne. Všechny údaje odpovídají skutečně
provedeným krokům; nic není odhad ani ukázka.

- Repozitář: `https://github.com/nekam13/mini-search`
- Pracovní větev: `android-app-completion`
- Zdrojový commit obou APK: **`36735a1`** (`Refuse mutating admin actions over GET`)
- Commit s dokumentací: `7200ac0` (přidává tento dokument a přebudované APK;
  zdrojový kód backendu ani Androidu se v něm nemění)
- Výchozí větev `beta-optimized` byla ověřena; větev `android-app-completion`
  vychází z referenčního commitu zadání a obsahuje dokončení Android aplikace.

## 1. Odevzdané soubory

| Soubor | Varianta | Velikost | SHA-256 |
|---|---|---|---|
| `mini-search-7.6-nomodel-debug.apk` | bez modelu (hashovací fallback), **commitnutá** | 64 388 880 B (~61,4 MiB) | `16edf63edbc22cb35ea91080f3700734bbd256066975ab08c3287192e88b4534` |
| `mini-search-7.6-debug.apk` | plná s modelem MiniLM (142 MB), **negitovaná** | 142 024 109 B (~135,4 MiB) | `09a2198a7566ab06b27f65e986e79b8abdfe773931f3dc94811ae0bf0cabb076` |

Metadata obou variant (shodná): `applicationId = cz.minisearch.app`,
`versionCode = 2`, `versionName = 7.6`, nativní ABI `arm64-v8a`.

Proč je plná varianta negitovaná: GitHub má tvrdý limit 100 MB na soubor a
Git LFS v tomto prostředí není k dispozici. Plná varianta proto **není**
nahrána do gitu; sestaví se jedním příkazem (viz § 4) a model se při buildu
stáhne a ověří proti SHA-256. Obě varianty jsou skutečně sestavené z commitu
`36735a1`, nikoli převzaté z dřívějška.

## 2. Co bylo dokončeno

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

### Backendové testy (všech 16 skriptů z AGENTS.md)

Spuštěno `python3 tests/<skript>.py` pro každý skript; všechny prošly (exit 0):

| Skript | PASS |
|---|---|
| `test_db.py` | 9 |
| `test_admin.py` | 8 |
| `test_admin_panel.py` | 65 |
| `test_discovery_integration.py` | 16 |
| `test_search_page.py` | 52 |
| `test_source_indexing.py` | 31 |
| `test_local_sites.py` | 49 |
| `test_local_indexing_smoke.py` | 1 (běží reálný HTTP server) |
| `test_wiki_import.py` | 42 |
| `test_crawl_metadata.py` | 37 |
| `test_rich_cards.py` | 50 |
| `test_maintenance.py` | 46 |
| `test_crawler_discovery.py` | 27 |
| `test_search_quality.py` | 44 |
| `test_settings_images.py` | 45 |
| `test_lifecycle.py` | 37 |

Testy životního cyklu (`test_lifecycle.py`) pokrývají přesně to, co úkol žádá:
start → `RUNNING` až po `/health`, opakovaný start jako no-op bez druhého
serveru/scheduleru/workerů, stop a restart ve stejném procesu, zachování dat,
oddělení vektorových prostorů a obnovu přerušené fronty.

### Android JVM testy

`./gradlew :app:testDebugUnitTest` – `SpmTokenizerTest` (6 testů) a
`EmbeddingBridgeTest` (1 test), 0 selhání, 0 přeskočeno. Ověřují shodu
tokenizeru s referencí a embedding výstupu (mean pooling, L2 normalizace)
proti referenčním vektorům `sentence-transformers` na českých textech.

### Android instrumentační testy

`android/app/src/androidTest/.../ServerSmokeTest.kt` byl rozšířen na pět testů:
spuštění a připravenost serveru, `/health`, idempotentní start bez duplicitních
workerů, načtení hledání i administrace přes HTTP, stop a restart ve stejném
procesu a umístění databáze v privátním úložišti. Testy se **kompilují**
(`:app:compileDebugAndroidTestKotlin` prošlo), ale **nebyly spuštěny na
zařízení** – viz § 6.

## 4. Postup buildu z čistého checkoutu

Potřebné: **JDK 17**, **Android SDK** (platform 34, build-tools 34.0.0, NDK
26.1.10909125). Gradle wrapper je součástí repa (Gradle 8.7 s kontrolním
součtem distribuce `544c35d6…961d`).

```bash
git clone https://github.com/nekam13/mini-search.git
cd mini-search
git checkout android-app-completion

cd android
echo "sdk.dir=/cesta/k/android-sdk" > local.properties

# malá varianta bez modelu (hashovací fallback) – tato je v repu:
./gradlew :app:assembleDebug -PskipEmbeddingModel
# -> app/build-nomodel/outputs/apk/debug/app-debug.apk

# plná varianta s reálným modelem (stáhne a ověří model i tokenizer):
./gradlew :app:assembleDebug
# -> app/build/outputs/apk/debug/app-debug.apk

# testy:
./gradlew :app:testDebugUnitTest
```

Při prvním plném buildu se stáhnou dva artefakty z Hugging Face (model
`model_qint8_arm64.onnx` a `tokenizer.json`) a ověří se proti SHA-256. Offline
build je možný přes `-PembeddingModel=/cesta/model.onnx
-PtokenizerJson=/cesta/tokenizer.json`. Verze závislostí jsou připnuté
(AGP 8.5.2, Kotlin 1.9.24, Chaquopy 17.0.0, Python 3.13).

Obě varianty byly takto sestaveny z **čistého klonu** commitu `36735a1` bez
ručních oprav a bez absolutních cest v repu.

## 5. Ověřená funkčnost a co ověřeno nebylo

**Ověřeno:**
- Build obou variant z čistého checkoutu projde (`BUILD SUCCESSFUL`).
- V plné variantě jsou v APK `assets/embedding/model_qint8_arm64.onnx`,
  `assets/ml/tokenizer_spm.bin`, `lib/arm64-v8a/libonnxruntime.so`,
  `lib/arm64-v8a/libonnxruntime4j_jni.so` a `assets/chaquopy/app.imy`.
- V malé variantě model **není**, tokenizer ano.
- `templates/` a `static/` jsou zabalené v `app.imy` a v zabalené podobě
  obsahují opravené POST formuláře (GET odkazy zmizely).
- Android JVM testy procházejí.
- Všech 16 backendových sad prochází (viz § 3).
- Instrumentační testy se kompilují.

**Neověřeno (přesné důvody):**
- **Instalace a běh na zařízení/emulátoru.** V tomto prostředí nelze spustit
  emulátor: hostitel je `x86_64` **bez `/dev/kvm` a bez virtualizace**
  (`vmx`/`svm` v `/proc/cpuinfo` chybí), takže hardwarově akcelerovaný emulátor
  neběží; emulátor Google navíc nepodporuje arm64 hosty na x86_64 hostiteli a
  APK je postavené jen pro `arm64-v8a`. Proto **nebylo provedeno** ověření
  „nainstaluj nové APK a spusť hledání" ani `connectedDebugAndroidTest`.
  Skutečné nasazení na fyzické arm64 zařízení zůstává neověřené.
- **Shoda embeddingů na zařízení** – `EmbeddingBridgeTest` je JVM test, který
  ověřuje matematiku ONNX Runtime na desktopu; na zařízení přes Chaquopy běžel
  jen `ServerSmokeTest` (neověřeno, viz výše).
- **Podpisová kompatibilita s předchozí instalací** – viz § 7.

## 6. Bezpečnostní opatření (souhrn)

- HTTP server poslouchá na `127.0.0.1`; LAN indexování je jiná věc než
  zpřístupnění aplikace do LAN.
- `/admin` a `/admin/api/**` jsou dostupné jen z loopbacku; navíc se odmítají
  mutující požadavky s cizím `Origin`/`Referer` (403).
- Všech pět dříve GET-mutujících akcí (recrawl, recrawl-all, pause-all,
  resume-all, delete) je nyní POST-only; GET vrací 405.
- Z APK jsou vypnuté git aktualizace i jejich mutační endpointy
  (`BUNDLED_RUNTIME=1`); aplikace nikdy nespouští `git pull` ani `pip install`.
- WebView: JS ano (Clay UI), ale `allowFileAccess=false`,
  `allowContentAccess=false`, žádný JavaScript most, externí odkazy se otevírají
  v systémovém prohlížeči, `MIXED_CONTENT_NEVER_ALLOW`.
- TLS se nikde nevypíná (žádné `verify=False`, trust-all ani obcházení).
- Secret key se generuje za běhu (`os.urandom`), v repu není pevný klíč.

## 7. Aktualizace a podpis

`applicationId` zůstal `cz.minisearch.app` a `versionCode` se zvýšil z 1 na 2
(`versionName` 7.5 → 7.6). Debug APK je podepsané automatickým debug klíčem
(otisk certifikátu SHA-256: `bb20decd8562c42dfaf0e621a4e0a65cc3b68f7f819fde8dba73930487623104`).
**Původní podpisový klíč aplikace `mini-search-7.5-debug.apk` není v tomto
prostředí k dispozici**, proto běžná aktualizace přes původní instalaci nemusí
být možná; odinstalace staré verze by smazala data a **není** náhradou migrace.
Release se podepisuje jen dodatečným klíčem přes bezpečnou konfiguraci; klíče a
hesla nejsou součástí repozitáře a žádný produktivně podepsaný release není
tvzen.

## 8. Reprodukovatelnost

Build byl ověřen dvakrát z čistého klonu (commit `36735a1`) a výsledek je
**funkčně shodný**:

- V plné variantě má zabalený model přesně SHA-256
  `783fea82d71a58179b830a4dbd2d58447e640609e98eedf9ffa12622d375a672` (shodný
  s připnutou hodnotou v `build.gradle`), tokenizer i nativní knihovny
  `libonnxruntime*.so` jsou přítomné.
- V malé variantě jsou `templates/` (13 souborů) i `static/` (8) v `app.imy`
  a v zabalené podobě obsahují opravené POST formuláře.
- Těla přeložených modulů (`app_combined.pyc`, `android_entry.pyc`) jsou
  **bajtově shodná**; liší se jen 4bajtová hlavička s časem překladu.

Bajtová shoda celého APK není zaručena: ZIP záznamy nesou časové značky,
Python `.pyc` obsahuje mtime a `classes*.dex` / `requirements-common.imy` nesou
metadata nástrojů. Ověřená je proto **obsahová** reprodukovatelnost, ne
bit-identický soubor.

## 9. Omezení prostředí během práce

Prostředí bylo v průběhu práce restartováno a přišlo o `/opt/jdk17`,
`/opt/android-sdk` i Python závislosti. Vše bylo znovu nainstalováno (JDK 17
Temurin, Android SDK cmdline-tools + platform 34 + build-tools 34.0.0 + NDK
26.1.10909125, `pip install -r requirements.txt`) a buildy i testy byly po
obnově zopakovány, aby výsledky odpovídaly skutečnému stavu. Databáze
`console.db` se nikdy needitovala ani nemazala.
