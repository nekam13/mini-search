#!/usr/bin/env bash
#
# Build the Mini Search desktop app for macOS.
#
# Produces, in packaging/macos/dist/:
#   * mini-search.app     – double-clickable app bundle
#   * MiniSearch-<version>-macos-<arch>.dmg – disk image (when hdiutil exists)
#
# Must run on macOS: PyInstaller cannot cross-compile a macOS binary from Linux.
# The GitHub Actions workflow runs it on a macos-latest runner.
#
# Usage:  bash packaging/macos/build_macos.sh [--version 7.9]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD="$HERE/build"
DIST="$HERE/dist"
VERSION="7.9"
while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        *) echo "Neznámý přepínač: $1" >&2; exit 2 ;;
    esac
done
ARCH="$(uname -m)"
mkdir -p "$BUILD" "$DIST"

echo "==> Python závislosti"
python3 -m pip install --upgrade pip
# Same light profile as Linux: no torch / sentence-transformers. The desktop
# build uses FTS5 + the hashing embedder unless the user supplies an ONNX model.
python3 -m pip install -r "$ROOT/requirements-mobile.txt"
python3 -m pip install "pyinstaller>=6.0"

echo "==> PyInstaller"
python3 -m PyInstaller --clean --noconfirm \
    --distpath "$BUILD/pyinstaller-dist" \
    --workpath "$BUILD/pyinstaller-work" \
    --windowed \
    "$ROOT/packaging/minisearch.spec"

APP="$BUILD/pyinstaller-dist/mini-search.app"
[ -d "$APP" ] || { echo "App bundle nenalezen: $APP"; exit 1; }

# Add the usage description strings and the .app icon.
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
if [ -f "$HERE/mini-search.icns" ]; then
    cp "$HERE/mini-search.icns" "$APP/Contents/Resources/icon-windowed.icns"
fi
codesign --force --deep --sign - "$APP" 2>/dev/null || \
    echo "(ad-hoc podpis přeskočen – není codesign)"

echo "==> DMG"
if command -v hdiutil >/dev/null; then
    DMG="$DIST/MiniSearch-$VERSION-macos-$ARCH.dmg"
    rm -f "$DMG"
    hdiutil create -volname "Mini Search" -srcfolder "$APP" -ov -format UDZO "$DMG"
fi

echo
echo "==> Hotovo"
ls -la "$DIST"
