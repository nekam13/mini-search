# Mini Search pro desktop a mobil – balíčky, build a ověření

Tento dokument popisuje, jak se z téhož backendu (`app_combined.py`) sestaví
instalovatelné balíčky pro **Linux, Windows, macOS a iOS/iPadOS**, a co z toho
bylo v tomto prostředí skutečně sestaveno a otestováno. Nic není odhad: co
nešlo v Linux kontejneru ověřit, je výslovně označeno jako neověřené.

- Repozitář: `https://github.com/nekam13/mini-search`
- Zdrojový commit: `6451f0a` (větev `android-app-completion`) — tento commit
  přidává desktop/mobilní balíčky a aktualizuje obě Android APK
- Verze: **7.9** (shodná s Android APK i s `versionName`)

Android APK má vlastní dokument `DODANI-ANDROID.md`; v tomto commitu se u něj
aktualizovaly jen údaje o novém buildu (hashe, commit, výsledky testů).

## 1. Princip: jeden backend, žádná druhá logika

Všechny platformy spouští **stejný** `app_combined.py` přes `start_server()` /
`stop_server()`. Nepřidává se žádná druhá konfigurace ani druhá implementace
vyhledávání:

- **Linux / Windows / macOS** – `desktop/launcher.py` nastaví prostředí
  (privátní datový adresář, loopback, vypnutá autoupdate), spustí server a
  otevře UI (nativní okno `pywebview`, jinak výchozí prohlížeč).
- **iOS / iPadOS** – `packaging/ios/src/minisearch/app.py` (BeeWare Toga)
  spustí stejný server na `127.0.0.1` a zobrazí ho ve WebView.
- **Android** – `android_entry.py` (beze změny).

`SETTINGS_SPEC` v `app_combined.py` zůstává jediným zdrojem pravdy pro
nastavení; desktop ani iOS si nevedou vlastní konfiguraci.

## 2. Odevzdané balíčky

### Sestaveno a otestováno v tomto prostředí (Linux x86_64)

| Soubor | Velikost | SHA-256 |
|---|---|---|
| `packaging/linux/dist/mini-search-7.9-x86_64.AppImage` | 36 121 080 B (~34,4 MiB) | `647cbe3cf17bb88cb790de1fbb97f52e4f52007308a34c05241e706c56560ddd` |
| `packaging/linux/dist/mini-search_7.9_amd64.deb` | 27 955 560 B (~26,7 MiB) | `dfe272ecb103eb9eaa60558a40113434174988c8349efc06a2d0ca951c0c125b` |

Metadata: `applicationId` (Android) se desktop netýká; balíčky nesou
`Name=Mini Search`, `versionName`/`Version` **7.9**. Databáze, logy a modely se
ukládají do privátního adresáře uživatele (`~/.local/share/mini-search`,
respektive `%LOCALAPPDATA%\MiniSearch` a `~/Library/Application Support/MiniSearch`),
nikdy do instalačního stromu ani do aktuálního pracovního adresáře.

### Připraveno ke sestavení, ale **v tomto prostředí nesestaveno**

Tyto balíčky nelze z Linuxu zkompilovat (PyInstaller ani Xcode neumí křížový
build) — sestaví je přiložený GitHub Actions workflow na nativních runnerech:

| Platforma | Výstup | Skript |
|---|---|---|
| Windows | `MiniSearch-7.9-windows-x64.zip`, volitelně `…-setup.exe` | `packaging/windows/build_windows.ps1`, `installer.iss` |
| macOS | `mini-search.app`, `MiniSearch-7.9-macos-<arch>.dmg` | `packaging/macos/build_macos.sh` |
| iOS/iPadOS | `.app`/`.ipa` | `packaging/ios/build_ios.sh` (BeeWare Briefcase) |

Workflow: `.github/workflows/build-release.yml` – na tag `v*` sestaví všechny
platformy a přiloží je k GitHub release.

## 3. Ověřené výsledky (skutečně spuštěné)

### Launcher a životní cyklus

`python3 tests/test_desktop_launcher.py` → **19/19 PASS** proti skutečnému
backendu na loopbacku (žádné mocky):

- datový adresář respektuje `MINISEARCH_DATA_DIR`, je per-user a mimo bundle;
- prostředí: `console.db` v datovém adresáři, `BUNDLED_RUNTIME=1` (vypnutá
  autoupdate), `UPDATE_CHECK=0`, profil `lowmem`, loopback, LAN vypnutá;
- start → `RUNNING` až po HTTP `/health` (skutečný dotaz), `GET /health` = 200,
  `console.db` vznikne v datovém adresáři, veřejná stránka `/` se načte;
- stop → `STOPPED`, HTTP už neodpovídá; **restart ve stejném procesu** funguje
  a **nevznikají duplicitní workeři**.

### Balíčky (smoke test `packaging/smoke_test.sh`)

Pro každý artefakt se spustí server, čeká se na `/health` = 200, načte se `/`
i `/admin` a po `SIGTERM` se ověří, že se port uvolnil. Prošly:

- `mini-search-7.9-x86_64.AppImage` (přes `--appimage-extract-and-run`, FUSE
  v kontejneru není) – **4/4 PASS**;
- `mini-search_7.9_amd64.deb` – nainstalováno `dpkg -i`, spuštěn
  `/opt/mini-search/mini-search` i vyextrahovaná binárka – **4/4 PASS**;
- AppDir `packaging/linux/build/mini-search.AppDir/AppRun` – **4/4 PASS**.

Ověřeno navíc: `dpkg -r mini-search` balíček čistě odstraní (nezůstane
`/opt/mini-search` ani `/usr/bin/mini-search`).

### Backendové testy (18 sad)

Spuštěny všechny skripty z `AGENTS.md` (`python3 tests/<skript>.py`), všechny
prošly (exit 0), včetně nového `test_desktop_launcher.py`. Tato práce žádný
backendový soubor nemění, jde tedy o regresní kontrolu.

## 4. Postup buildu z čistého checkoutu

### Linux (AppImage + .deb)

Potřebné: `python3` (s `pip`), `curl`, `dpkg-deb`, `file`, `libfuse2` a síť při
prvním běhu (PyInstaller + appimagetool se stáhnou do `packaging/linux/.cache`,
který se necommituje).

```bash
git clone https://github.com/nekam13/mini-search.git
cd mini-search
git checkout android-app-completion
bash packaging/linux/build_linux.sh --version 7.9
# -> packaging/linux/dist/mini-search-7.9-x86_64.AppImage
# -> packaging/linux/dist/mini-search_7.9_amd64.deb
bash packaging/smoke_test.sh packaging/linux/dist/mini-search-7.9-x86_64.AppImage
```

Skript sám instaluje lehké závislosti (`requirements-mobile.txt`, bez torch) a
PyInstaller, pokud chybí.

### Windows / macOS / iOS

Nativní runner (viz `.github/workflows/build-release.yml`), nebo ručně:

```powershell
# Windows
pwsh -File packaging/windows/build_windows.ps1 -Version 7.9
```
```bash
# macOS
bash packaging/macos/build_macos.sh --version 7.9
# iOS/iPadOS (vyžaduje Xcode)
bash packaging/ios/build_ios.sh --version 7.9 --adhoc
```

## 5. Co je neověřené (a proč)

- **Windows, macOS a iOS balíčky nejsou sestavené.** Toto prostředí je Linux
  x86_64 bez Xcode; PyInstaller neumí křížový build a iOS vyžaduje macOS +
  Xcode. Připravené skripty a CI workflow jsou, ale výsledné soubory pro tyto
  platformy **v tomto odevzdání nejsou** – nevydávám je za hotové.
- **Reálný ONNX model v desktop balíčcích.** Desktop build model nebalí a
  spadne na FTS5 + hashovací embedding (bez sémantiky). To je záměr: 118MB
  model nechceme v každém balíčku; kdo ho chce, nastaví `MINISEARCH_MODEL`.
  Reálné sémantické hledání na zařízení je ověřené jen v Android APK
  (`DODANI-ANDROID.md`).
- **Podpis.** Linux balíčky jsou nepodepsané (AppImage/deb podpis se zde
  neřeší); Windows/macOS/iOS by v CI potřebovaly vlastní certifikáty. Žádný
  produktivně podepsaný release se netvrdí.

## 6. Omezení prostředí

- `LD_LIBRARY_PATH` v tomto kontejneru obsahuje cestu k aarch64 knihovnám,
  která rozbíjí `ctypes`/`numpy` (segfault). Build i testy proto běží s
  `unset LD_LIBRARY_PATH`; `build_linux.sh` to dělá sám.
- V kontejneru není FUSE, takže `appimagetool` se rozbalí a spouští přímo;
  hotová AppImage se testuje přes `--appimage-extract-and-run`.
- V kontejneru není `/dev/kvm`, takže Android emulátor je pomalý (viz
  `DODANI-ANDROID.md`).
