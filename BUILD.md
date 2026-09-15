# KRATR friend-build

This is an **isolated copy** of KRATR set up to produce a single installer you can send
to friends. Your personal working copy (`../crate`) is untouched by anything here.

## What's different from the personal copy

Two additive changes, both invisible to an existing install:

1. **Bundled ffmpeg.** `crate/config.py` → `find_executable()` now also looks for
   `ffmpeg.exe`/`ffprobe.exe` next to `KRATR.exe` (see `bundled_tool_dir()`), *before*
   falling back to PATH. In a source checkout there is no bundled copy, so it behaves
   exactly as before. The installer drops ffmpeg beside `KRATR.exe`.

2. **First-run walkthrough.** `crate/ui/onboarding.py`, launched from `ui/app.py` only
   when `%APPDATA%\Crate\settings.json` is missing (i.e. a fresh machine). Screens:
   Welcome → Dependencies → rekordbox → Format → What to include → Library folder. On
   finish it writes `settings.json`; the app then loads exactly those settings.

3. **Optional tags & colours.** Two new settings, `use_tags` and `use_colours`
   (`config.py`, default `True`). The "What to include" onboarding screen sets them —
   tags on, colours off by default, with wording that KRATR only *suggests* a tag set
   and either page can be skipped. `ui/wizard.py` builds the import flow from these
   flags (the Tags/Colour step widgets are always constructed so `main_window` can keep
   configuring them, but only enabled ones are shown). They can also be changed under
   Settings → "Import steps"; that change takes effect on the next launch (the
   onboarding choice is live immediately).

Because the walkthrough fires only when there are no settings, anyone who already runs
KRATR never sees it.

## Layout

```
crate/            the app source (copy of your personal package + the two changes)
vendor/ffmpeg/    bundled ffmpeg.exe + ffprobe.exe + license  (git-ignore these; large)
installer/
  kratr.iss       Inno Setup script  → produces installer/out/KRATR-Setup-v<ver>.exe
  assets/         license notice + friend-facing README shown/installed by the wizard
dist-friend/      PyInstaller output: KRATR.exe + kratr-cli.exe
```

## Rebuilding the installer from scratch

Uses your existing venv as the build interpreter (it is never modified).

```bash
# 1. Build the executables (run from this folder so it picks up THIS crate/ package)
"../crate/.venv/Scripts/pyinstaller.exe" crate.spec \
    --distpath dist-friend --workpath build-friend --noconfirm

# 2. Refresh bundled ffmpeg only if you want a newer one (essentials static build):
#    download ffmpeg-release-essentials.zip from https://www.gyan.dev/ffmpeg/builds/
#    and drop bin\ffmpeg.exe + bin\ffprobe.exe into vendor/ffmpeg/

# 3. Compile the installer
"$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe" installer/kratr.iss
#    → installer/out/KRATR-Setup-v0.1.0.exe
```

Bump the version by editing `crate/__init__.py` and passing `/DAppVersion=x.y.z` to
ISCC (or editing the default in `kratr.iss`).

## Sending it to friends

Send `installer/out/KRATR-Setup-v<ver>.exe`. It's unsigned, so Windows SmartScreen
shows a blue "Windows protected your PC" screen — tell them to click **More info →
Run anyway**. It installs per-user (no admin prompt), bundles ffmpeg, and the app walks
them through setup on first launch.

They still need rekordbox installed and opened once so its library exists.

## macOS

A Mac build **cannot be produced on Windows** (PyInstaller can't cross-compile), so the
Mac app is built on a Mac. Everything for it is prepared:

- `crate-mac.spec` — PyInstaller spec producing `dist/KRATR.app`.
- `build-mac.command` — double-clickable script that sets up Python, deps and Mac
  ffmpeg, builds the app, and packages `KRATR.dmg`.
- `MAC-BUILD.md` — plain-language instructions for a non-technical Mac owner.
- `crate/resources/kratr-icon.icns` — the Mac app icon.
- `kratr-mac.zip` — a clean bundle of just the above + source (no Windows binaries), to
  forward to whoever has the Mac.

The code is already macOS-aware (`config.py` uses `~/Library/Application Support/Crate`
and `~/Library/Pioneer/rekordbox`; `bundled_tool_dirs()` finds ffmpeg inside the
`.app`). Same additive approach — Windows behaviour is unchanged.

Unsigned, so first launch on macOS is right-click → Open (see MAC-BUILD.md). Removing
that needs a paid Apple Developer ID for signing + notarization.
