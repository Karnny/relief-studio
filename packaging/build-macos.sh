#!/bin/sh
# Build "Relief Studio.app" and a .dmg for macOS.
#   ./packaging/build-macos.sh
set -e
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10+ is required (set PYTHON=/path/to/python3)." >&2; exit 1; }

if [ ! -d .venv-build-mac ]; then
  echo "Creating build environment…"
  "$PY" -m venv .venv-build-mac
fi
# shellcheck disable=SC1091
. .venv-build-mac/bin/activate

echo "Installing build dependencies…"
python -m pip install --upgrade pip --quiet
python -m pip install numpy pillow pyinstaller --quiet

echo "Building the app bundle…"
python -m PyInstaller ReliefStudio.spec --noconfirm --clean

# Ad-hoc signature: enough for local use and for moving the app between Macs
# with a right-click → Open. Distribution still needs a Developer ID.
codesign --force --deep --sign - "dist/Relief Studio.app" >/dev/null 2>&1 || true

ARCH=$(uname -m)
DMG="dist/ReliefStudio-1.0.0-macOS-${ARCH}.dmg"
echo "Creating ${DMG}…"
hdiutil create -volname "Relief Studio" -srcfolder "dist/Relief Studio.app" \
  -ov -format UDZO "$DMG" >/dev/null

echo
echo "Done:"
echo "  dist/Relief Studio.app"
echo "  $DMG"
echo
echo "Double-click the app; the browser opens at http://127.0.0.1:8765/"
