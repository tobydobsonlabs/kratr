# PyInstaller spec — build with:  pyinstaller crate.spec
#
# ffmpeg/ffprobe are deliberately NOT bundled. They live in a WinGet package directory
# whose name changes with every ffmpeg update, so hardcoding a path would break; KRATR
# finds them on PATH at runtime instead.

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = [
    # pyrekordbox imports its table modules dynamically.
    *collect_submodules("pyrekordbox"),
    "sqlcipher3",
    "sqlalchemy.dialects.sqlite",
]

a = Analysis(
    # Not crate/__main__.py directly — PyInstaller runs the entry script as __main__
    # with no package context, which breaks that module's relative imports.
    ["run_crate.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("crate/resources/style.qss", "crate/resources"),
        ("crate/resources/kratr-icon-inverted-v1.png", "crate/resources"),
        ("crate/resources/kratr-wordmark-fractured-v1.png", "crate/resources"),
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.Qt3D"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# The GUI. console=False so launching it doesn't flash a terminal.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="KRATR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon="crate/resources/kratr-icon.ico",
)

# A console twin, because `doctor` and `audit` print reports — and a windowed build
# has nowhere to print them. Running "check the library after a rekordbox update"
# shouldn't require the source checkout.
exe_cli = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="kratr-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    icon="crate/resources/kratr-icon.ico",
)
