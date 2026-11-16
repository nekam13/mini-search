#!/usr/bin/env bash
#
# Build the Mini Search app for iOS / iPadOS with BeeWare Briefcase.
#
# Briefcase bundles the Python runtime and all dependencies into a real Xcode
# project, so the device needs neither Termux nor a server. This script first
# copies the shared backend (app_combined.py, templates/, static/) into the
# package, so there is exactly one implementation of the search logic.
#
# Must run on macOS with Xcode: Briefcase cannot build an .ipa/.app from Linux.
# The GitHub Actions workflow runs it on a macos-latest runner.
#
# Usage:  bash packaging/ios/build_ios.sh [--version 7.9] [--adhoc]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VERSION="7.9"
ADHOC=0
while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --adhoc) ADHOC=1; shift ;;
        *) echo "Neznámý přepínač: $1" >&2; exit 2 ;;
    esac
done

PKG="$HERE/src/minisearch"

echo "==> Kopíruji sdílený backend do iOS balíčku"
cp "$ROOT/app_combined.py" "$PKG/app_combined.py"
rm -rf "$PKG/templates" "$PKG/static"
cp -a "$ROOT/templates" "$PKG/templates"
cp -a "$ROOT/static" "$PKG/static"

echo "==> Briefcase"
python3 -m pip install --upgrade "briefcase>=0.3.20"
cd "$HERE"
python3 -m briefcase create iOS
python3 -m briefcase update iOS --update-requirements
if [ "$ADHOC" -eq 1 ]; then
    # No signing identity in CI: build an unsigned/ad-hoc .app for testing.
    python3 -m briefcase build iOS --adhoc-sign
else
    python3 -m briefcase build iOS
fi
python3 -m briefcase package iOS --adhoc-sign -p ios 2>/dev/null || \
    python3 -m briefcase package iOS --adhoc-sign

echo
echo "==> Hotovo – výstup najdete v packaging/ios/dist/"
ls -la "$HERE/dist" 2>/dev/null || true
