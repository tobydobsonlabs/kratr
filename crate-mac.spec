# PyInstaller spec for macOS — produces dist/KRATR.app
#
# MUST be built on a Mac (PyInstaller cannot cross-compile). The build-mac.command
# script sets everything up and runs:  pyinstaller crate-mac.spec
#
# ffmpeg/ffprobe are bundled INSIDE the .app (unlike the Windows build, where the
# installer drops them next to the exe). build-mac.command downloads static macOS
# builds into vendor/ffmpeg-mac/ before this runs; if they're absent the app still
# builds and falls back to any ffmpeg on PATH.

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = [
    *collect_submodules("pyrekordbox"),
    "sqlcipher3",
    "sqlalchemy.dialects.sqlite",
]

datas = [
    ("crate/resources/style.qss", "crate/resources"),
    ("crate/resources/kratr-icon-inverted-v1.png", "crate/resources"),
    ("crate/resources/kratr-wordmark-fractured-v1.png", "crate/resources"),
]

# Bundle the macOS ffmpeg/ffprobe if the build script fetched them. Placed at the
# bundle root ('.') → Contents/MacOS (or Frameworks); config.bundled_tool_dirs()
# looks in both, so KRATR finds them with no PATH setup.
binaries = []
for tool in ("ffmpeg", "ffprobe"):
    path = os.path.join("vendor", "ffmpeg-mac", tool)
    if os.path.isfile(path):
        binaries.append((path, "."))

a = Analysis(
    ["run_crate.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.Qt3D"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# exclude_binaries=True: this is a .app (COLLECT + BUNDLE), not a onefile exe.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KRATR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed GUI app
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="KRATR",
)

app = BUNDLE(
    coll,
    name="KRATR.app",
    icon="crate/resources/kratr-icon.icns",
    bundle_identifier="net.kratr.app",
    info_plist={
        "CFBundleName": "KRATR",
        "CFBundleDisplayName": "KRATR",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        # KRATR is a single-window desktop tool, not a document app.
        "LSMinimumSystemVersion": "11.0",
    },
)
