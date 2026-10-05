# Mini Search – Android APK

Samostatná aplikace pro Android, která **nevyžaduje Termux**. Server
(`app_combined.py`) i jeho Python závislosti jsou zabalené přímo v APK pomocí
[Chaquopy](https://chaquo.com/chaquopy/); UI je WebView, které načítá
`http://127.0.0.1:8070/`.

- **Vyhledávač** – veřejná stránka `/`, dostupná i v prohlížeči na telefonu.
- **Správa** – panel `/admin`; když server běží jen na telefonu, je dostupný
  pouze z tohoto zařízení (`MINISEARCH_ADMIN_LOCAL_ONLY=1`).
- Server drží na pozadí foreground služba (`ServerService`), takže indexace
  neusne. Data (SQLite + logy) jsou v privátním úložišti aplikace.

## Vektory

`hnswlib` pro Android neexistuje, ale `numpy` ano. `vector_search()` proto bez
`hnswlib` použije přesné brute-force hledání nejbližších sousedů přes numpy
(`_bruteforce_neighbours`). Vektory se řídí režimem `auto`: vypnou se jen při
nízké RAM nebo baterii, jinak běží. Reálný model `sentence-transformers` v APK
není, takže se použije `_HashingEmbedding` (bez stahování modelu).

## Build

Potřebujete JDK 17, Android SDK (platform 34, build-tools, NDK) a Gradle 8.9.
Cestu k SDK nastavte do `android/local.properties` (`sdk.dir=...`).

```bash
cd android
gradle :app:assembleDebug
# výsledek: app/build/outputs/apk/debug/app-debug.apk
```

Chaquopy při prvním buildu stahuje Python balíčky z `repo.chaquo.com`; v tomto
prostředí má repozitář neplatný TLS certifikát (`CN=chaquo.com`), takže build
potřebuje `-k` v pipu, případně `JAVA_TOOL_OPTIONS` s vypnutou kontrolou.

## Konfigurace

`android/requirements-android.txt` je seznam Python balíčků v APK. `extruct`
záměrně chybí (potřebuje `rdflib`/`jstyleson`, které nemají Android kola);
`_extract_jsonld()` se bez něj obejde vlastním parserem.
