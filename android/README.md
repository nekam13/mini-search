# Mini Search – Android APK

Samostatná aplikace pro Android, která **nevyžaduje Termux**. Server
(`app_combined.py`) i jeho Python závislosti jsou zabalené přímo v APK pomocí
[Chaquopy](https://chaquo.com/chaquopy/); UI je WebView, které načítá
`http://127.0.0.1:8070/`.

- **Vyhledávač** – veřejná stránka `/`.
- **Správa** – panel `/admin`; server poslouchá jen na `127.0.0.1`, takže
  administrace není dostupná z LAN (`MINISEARCH_ADMIN_LOCAL_ONLY=1`).
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

## Životní cyklus serveru

`ServerController` drží stavy `STOPPED`, `STARTING`, `RUNNING`, `STOPPING`,
`ERROR`. Do stavu `RUNNING` se přejde až po úspěšné HTTP kontrole `/health`
s timeoutem; samotné spuštění Python vlákna se za běžící server nepovažuje.
Start je idempotentní (rotace ani opakované spuštění služby nevytvoří druhý
server). `stop_server()` korektně ukončí workery, scheduler i HTTP server,
udělá SQLite checkpoint a zavře spojení; nový start ve stejném procesu funguje.

## Build

Potřebujete JDK 17, Android SDK (platform 34, build-tools, NDK). Gradle wrapper
je součástí repa (Gradle 8.7 s kontrolním součtem distribuce).

```bash
cd android
./gradlew :app:assembleDebug
# výsledek: app/build/outputs/apk/debug/app-debug.apk
```

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

Chaquopy při prvním buildu stahuje Python balíčky z `repo.chaquo.com`. Cestu k
SDK nastavte do `android/local.properties` (`sdk.dir=...`).

### Testy

```bash
./gradlew :app:testDebugUnitTest            # JVM: tokenizer + ONNX vs reference
./gradlew :app:connectedDebugAndroidTest    # na zařízení/emulátoru: reálný start serveru
```

## Konfigurace

`android/requirements-android.txt` je seznam Python balíčků v APK. `extruct`
záměrně chybí (potřebuje `rdflib`/`jstyleson`, které nemají Android kola);
`_extract_jsonld()` se bez něj obejde vlastním parserem. `MINISEARCH_*`
proměnné se nastavují v `android_entry.py`; runtime nastavení zůstává v
`SETTINGS_SPEC` v `app_combined.py` (v Kotlinu není druhá konfigurace).

## Aktualizace aplikace

`applicationId` je `cz.minisearch.app`. `versionCode` se zvýšil z 1 na 2
(`versionName` 7.5 → 7.6). Baseline `mini-search-7.5-debug.apk` byl podepsaný
**jiným** debug klíčem, než jaký je v tomto prostředí, takže běžná aktualizace
přes původní instalaci nemusí být možná (odinstalace by smazala data). Release
se podepisuje jen dodatečným klíčem přes bezpečnou konfiguraci; klíče a hesla
nejsou součástí repozitáře.
