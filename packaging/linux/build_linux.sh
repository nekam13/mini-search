#!/usr/bin/env bash
#
# Build the Linux desktop packages for Mini Search from a clean checkout.
#
# Produces (in packaging/linux/dist/):
#   * mini-search-<version>-x86_64.AppImage   – single-file, no install
#   * mini-search_<version>_amd64.deb         – Debian/Ubuntu package
#
# Requirements: python3 (with pip), and network access the first time (PyInstaller
# + appimagetool). Everything else is fetched into packaging/linux/.cache and is
# NOT committed.
#
# Usage:  bash packaging/linux/build_linux.sh [--version 7.9]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CACHE="$HERE/.cache"
DIST="$HERE/dist"
BUILD="$HERE/build"
VERSION="7.9"

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        *) echo "Neznámý přepínač: $1" >&2; exit 2 ;;
    esac
done

APP_NAME="mini-search"
ARCH="$(uname -m)"            # x86_64 or aarch64
# Reverse-DNS id shared by the .desktop file, the icons and the AppStream
# metadata. appstreamcli (run by appimagetool) requires all three to agree, and
# the id must be reverse-DNS — hence not the plain "mini-search" binary name.
APP_ID="cz.minisearch.MiniSearch"
APPDIR="$BUILD/${APP_NAME}.AppDir"
mkdir -p "$CACHE" "$DIST" "$BUILD"

echo "==> Kontroluji nástroje"
# A poisoned LD_LIBRARY_PATH (e.g. left over from Android emulator work) breaks
# native Python: ctypes/numpy segfault. Clear it for the build.
unset LD_LIBRARY_PATH || true
command -v python3 >/dev/null || { echo "Chybí python3"; exit 1; }

# ---------------------------------------------------------------------------
# 0. Python dependencies (the light profile: no torch / sentence-transformers,
#    which the desktop build never needs — it falls back to FTS5 + hashing).
# ---------------------------------------------------------------------------
echo "==> Python závislosti"
python3 -m pip install --user -q -r "$ROOT/requirements-mobile.txt"

# ---------------------------------------------------------------------------
# 1. Build the self-contained Python bundle with PyInstaller.
# ---------------------------------------------------------------------------
echo "==> PyInstaller"
if ! python3 -c "import PyInstaller" 2>/dev/null; then
    python3 -m pip install --user -q "pyinstaller>=6.0"
fi
python3 -m PyInstaller --clean --noconfirm \
    --distpath "$BUILD/pyinstaller-dist" \
    --workpath "$BUILD/pyinstaller-work" \
    "$ROOT/packaging/minisearch.spec"

BUNDLE="$BUILD/pyinstaller-dist/$APP_NAME"
[ -x "$BUNDLE/$APP_NAME" ] || { echo "PyInstaller bundle nenalezen: $BUNDLE"; exit 1; }

# ---------------------------------------------------------------------------
# 2. Assemble the AppDir.
# ---------------------------------------------------------------------------
echo "==> AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps" \
         "$APPDIR/usr/share/metainfo"
cp -a "$BUNDLE/." "$APPDIR/usr/bin/"
cp "$HERE/$APP_ID.desktop" "$APPDIR/usr/share/applications/"
cp "$HERE/$APP_ID.desktop" "$APPDIR/$APP_ID.desktop"
cp "$HERE/$APP_ID.appdata.xml" "$APPDIR/usr/share/metainfo/"
cp "$HERE/$APP_ID.appdata.xml" "$APPDIR/$APP_ID.appdata.xml"
cp "$HERE/$APP_ID.svg" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_ID.svg"
cp "$HERE/$APP_ID.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_ID.png"
cp "$HERE/$APP_ID.svg" "$APPDIR/$APP_ID.svg"
cp "$HERE/$APP_ID.png" "$APPDIR/$APP_ID.png"
cp "$HERE/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
# AppImages are run by other users, so every file must be world-readable.
chmod -R a+rX "$APPDIR"

# ---------------------------------------------------------------------------
# 3. AppImage.
# ---------------------------------------------------------------------------
echo "==> AppImage"
TOOL="$CACHE/appimagetool-$ARCH.AppImage"
if [ ! -x "$TOOL" ]; then
    curl -fL -o "$TOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
    chmod +x "$TOOL"
fi
# appimagetool is itself an AppImage. Inside containers/CI there is usually no
# FUSE device, so extract it once and run the real binary directly.
TOOLRUN="$CACHE/appimagetool-$ARCH-squashfs-root/AppRun"
if [ ! -x "$TOOLRUN" ]; then
    ( cd "$CACHE" && "$TOOL" --appimage-extract >/dev/null 2>&1 && \
      mv squashfs-root "appimagetool-$ARCH-squashfs-root" )
fi
APPIMAGE="$DIST/${APP_NAME}-${VERSION}-${ARCH}.AppImage"
ARCH="$ARCH" "$TOOLRUN" "$APPDIR" "$APPIMAGE"
chmod +x "$APPIMAGE"

# ---------------------------------------------------------------------------
# 4. Debian package.
# ---------------------------------------------------------------------------
echo "==> .deb"
DEBROOT="$BUILD/deb"
rm -rf "$DEBROOT"
mkdir -p "$DEBROOT/DEBIAN" "$DEBROOT/opt/$APP_NAME" \
         "$DEBROOT/usr/bin" "$DEBROOT/usr/share/applications" \
         "$DEBROOT/usr/share/metainfo" \
         "$DEBROOT/usr/share/icons/hicolor/256x256/apps"
# A setgid parent (common in shared/CI workspaces) leaves the control dir at
# 2755, which dpkg-deb rejects. The 5-digit form clears the setgid bit; a plain
# "0755" would silently preserve it.
chmod 00755 "$DEBROOT/DEBIAN"
cp -a "$BUNDLE/." "$DEBROOT/opt/$APP_NAME/"
cp "$HERE/$APP_ID.desktop" "$DEBROOT/usr/share/applications/"
cp "$HERE/$APP_ID.appdata.xml" "$DEBROOT/usr/share/metainfo/"
cp "$HERE/$APP_ID.svg" "$DEBROOT/usr/share/icons/hicolor/256x256/apps/$APP_ID.svg"
cp "$HERE/$APP_ID.png" "$DEBROOT/usr/share/icons/hicolor/256x256/apps/$APP_ID.png"
ln -sf "/opt/$APP_NAME/$APP_NAME" "$DEBROOT/usr/bin/$APP_NAME"
chmod -R a+rX "$DEBROOT/opt"

SIZE_KB="$(du -sk "$DEBROOT/opt" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/amd64/" -e "s/@SIZE@/$SIZE_KB/" \
    "$HERE/deb-control.in" > "$DEBROOT/DEBIAN/control"
DEB="$DIST/${APP_NAME}_${VERSION}_amd64.deb"
dpkg-deb --build --root-owner-group "$DEBROOT" "$DEB"

echo
echo "==> Hotovo"
ls -la "$DIST"
