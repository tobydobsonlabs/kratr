#!/bin/bash
# ============================================================================
#  KRATR — Mac build.  Double-click this file to build KRATR for macOS.
#
#  It sets up everything it needs, builds the app, and produces a "KRATR.dmg"
#  in this folder that anyone can install by dragging KRATR into Applications.
#
#  You only need to do this once. It may take 10–20 minutes the first time.
# ============================================================================

set -u
cd "$(dirname "$0")" || exit 1

say()  { printf "\n\033[1;36m==> %s\033[0m\n" "$1"; }
warn() { printf "\n\033[1;33m!!  %s\033[0m\n" "$1"; }
die()  { printf "\n\033[1;31mXX  %s\033[0m\n" "$1"; echo; echo "Press Return to close."; read -r _; exit 1; }

say "KRATR Mac builder starting"

# --- 1. Python -------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  warn "Python 3 isn't installed yet. macOS will now offer to install the developer tools."
  warn "Click \"Install\" in the popup, wait for it to finish, then double-click this file again."
  xcode-select --install 2>/dev/null
  die "Re-run this after the developer tools finish installing."
fi
PYVER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?")"
say "Using Python $PYVER ($(command -v python3))"

# --- 2. Virtual environment + dependencies ---------------------------------
say "Setting up a private build environment (this can take a few minutes)…"
python3 -m venv .venv-mac || die "Could not create the build environment."
# shellcheck disable=SC1091
source .venv-mac/bin/activate || die "Could not activate the build environment."
python -m pip install --upgrade pip >/dev/null 2>&1

say "Installing KRATR's building blocks (PySide6, pyrekordbox, …)…"
pip install PySide6 pyrekordbox numpy psutil mutagen pyinstaller \
  || die "Could not install dependencies. Check your internet connection and try again."

# --- 3. ffmpeg / ffprobe (bundled into the app) ----------------------------
mkdir -p vendor/ffmpeg-mac
fetch_tool() {
  local tool="$1"
  if [ -x "vendor/ffmpeg-mac/$tool" ]; then say "$tool already present — skipping download"; return; fi
  say "Downloading $tool…"
  curl -L -sS -o "/tmp/$tool.zip" "https://evermeet.cx/ffmpeg/getrelease/$tool/zip" \
    || die "Could not download $tool."
  ( cd vendor/ffmpeg-mac && unzip -o -q "/tmp/$tool.zip" ) || die "Could not unpack $tool."
  chmod +x "vendor/ffmpeg-mac/$tool"
  rm -f "/tmp/$tool.zip"
}
fetch_tool ffmpeg
fetch_tool ffprobe

# --- 4. Build the app ------------------------------------------------------
say "Building KRATR.app (the longest step)…"
rm -rf build-mac dist
pyinstaller crate-mac.spec --distpath dist --workpath build-mac --noconfirm \
  || die "The build failed. Scroll up for the error, or send the output to whoever gave you this."
[ -d "dist/KRATR.app" ] || die "Build finished but KRATR.app is missing."

# --- 5. Make it launchable without a paid Apple certificate ----------------
# Ad-hoc sign so macOS treats it as a coherent app; friends still do the
# right-click → Open step the first time (see MAC-BUILD.md).
say "Signing the app locally…"
codesign --force --deep --sign - "dist/KRATR.app" >/dev/null 2>&1 \
  || warn "Local signing didn't complete — the app will still run, just with an extra warning."

# --- 6. Package a drag-to-install .dmg -------------------------------------
say "Packaging KRATR.dmg…"
rm -f KRATR.dmg
STAGE="$(mktemp -d)"
cp -R "dist/KRATR.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "KRATR" -srcfolder "$STAGE" -ov -format UDZO "KRATR.dmg" >/dev/null \
  || warn "Could not build the .dmg. You can still use dist/KRATR.app directly."
rm -rf "$STAGE"

say "Done!"
echo
echo "  •  App:  dist/KRATR.app   (drag it to your Applications folder)"
[ -f KRATR.dmg ] && echo "  •  Installer to share:  KRATR.dmg"
echo
echo "  First launch: right-click KRATR → Open → Open (a one-time macOS step)."
echo "  See MAC-BUILD.md for details."
echo
echo "Press Return to close."
read -r _
