#!/usr/bin/env bash
#
# Build the Mini Search app for iOS / iPadOS with BeeWare Briefcase.
#
# Briefcase bundles the Python runtime and all dependencies into a real Xcode
# project, so the device needs neither Termux nor a server. This script first
# copies the shared backend (app_combined.py, templates/, static/) into the
# package, so there is exactly one implementation of the search logic.
#
# Must run on macOS with Xcode: Briefcase cannot build from Linux. The GitHub
# Actions workflow runs it on a macos-latest runner.
#
# What this produces (see the note on signing below):
#   dist/Mini Search.app                  – the built (simulator) app bundle
#   dist/MiniSearch-<version>-ios.ipa     – an UNSIGNED .ipa wrapping that bundle
#   dist/MiniSearch-<version>-ios.zip     – the same .app, zipped
#
# Usage:  bash packaging/ios/build_ios.sh [--version 7.9]
#
# --- Why the .ipa is unsigned ---------------------------------------------
# Briefcase (0.3.x and 0.4.x) deliberately refuses to emit an iOS distribution
# artefact: `distribution_path()` always raises NoDistributionArtefact
# ("iOS apps must be published through Xcode"), and `briefcase build` has no
# signing options. Signing requires an Apple Developer identity and a
# provisioning profile, which a CI runner does not have and which must never be
# invented. So we build the app (which needs no signing) and wrap the resulting
# .app in a Payload/ .ipa ourselves. The result installs on a simulator, and on
# a device only after it is re-signed with a real identity (e.g. `codesign` +
# `xcrun devicectl`, or re-packaging in Xcode).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VERSION="7.9"
while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        *) echo "Neznámý přepínač: $1" >&2; exit 2 ;;
    esac
done

PKG="$HERE/src/minisearch"
DIST="$HERE/dist"

echo "==> Kopíruji sdílený backend do iOS balíčku"
cp "$ROOT/app_combined.py" "$PKG/app_combined.py"
rm -rf "$PKG/templates" "$PKG/static"
cp -a "$ROOT/templates" "$PKG/templates"
cp -a "$ROOT/static" "$PKG/static"

echo "==> Briefcase"
python3 -m pip install --upgrade "briefcase>=0.3.20,<0.5"
cd "$HERE"
python3 -m briefcase create iOS
python3 -m briefcase update iOS --update-requirements
python3 -m briefcase build iOS

# Briefcase builds into build/<app>/ios/xcode/build/Debug-iphonesimulator/.
APP="$(find "$HERE/build" -maxdepth 6 -type d -name "*.app" -path "*iphonesimulator*" | head -1)"
if [ -z "$APP" ]; then
    echo "CHYBA: nenalezen .app balíček; build Briefcase selhal." >&2
    exit 1
fi
echo "==> Nalezen .app: $APP"

rm -rf "$DIST"
mkdir -p "$DIST"
cp -a "$APP" "$DIST/Mini Search.app"

echo "==> Balím nepodepsaný .ipa (Payload/)"
IPA_STAGE="$(mktemp -d)"
mkdir -p "$IPA_STAGE/Payload"
cp -a "$APP" "$IPA_STAGE/Payload/"
( cd "$IPA_STAGE" && zip -qry "$DIST/MiniSearch-${VERSION}-ios.ipa" Payload )
( cd "$DIST" && zip -qry "MiniSearch-${VERSION}-ios.zip" "Mini Search.app" )
rm -rf "$IPA_STAGE"

echo
echo "==> Hotovo – výstup najdete v packaging/ios/dist/"
ls -la "$DIST"
echo
echo "POZNÁMKA: .ipa je nepodepsané. Instalace na zařízení vyžaduje podepsání"
echo "          platným Apple Developer identem (CI žádný nemá)."

