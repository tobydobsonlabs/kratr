# Building KRATR

This covers building the shipped apps yourself: the Windows installer and the macOS
`.app`/`.dmg`. Every tagged release already builds both in GitHub Actions and attaches them
to a GitHub Release, so most people never build anything. Read on if you want to do it
locally, or to understand what the release pipeline does.

## What the packaged app adds over a plain source run

Three additive changes. Each one stays invisible to an existing install and inert in a bare
source checkout.

1. **Bundled ffmpeg.** `crate/config.py` → `find_executable()` also looks for
   `ffmpeg.exe`/`ffprobe.exe` next to `KRATR.exe` (see `bundled_tool_dir()`) before falling
   back to PATH. A source checkout has no bundled copy, so it behaves exactly as before. The
   Windows installer drops ffmpeg beside `KRATR.exe`, and the Mac build tucks it inside the
   `.app`.

2. **First-run walkthrough.** `crate/ui/onboarding.py`, launched from `ui/app.py` only when
   `%APPDATA%\Crate\settings.json` is missing, which means a fresh machine. The screens run
   Welcome → Dependencies → rekordbox → Format → What to include → Library folder. On finish
   it writes `settings.json`, and the app loads exactly those settings. Anyone already
   running KRATR never sees it.

3. **Optional tags and colours.** Two settings, `use_tags` and `use_colours` (`config.py`,
   both default `True`). The "What to include" screen sets them, with tags on and colours
   off by default, and wording that makes clear KRATR only suggests a tag set and that either
   page can be skipped. `ui/wizard.py` builds the import flow from these flags. You can also
   change them under Settings → "Import steps", which takes effect on the next launch.

## Layout

```
crate/            the app source
vendor/ffmpeg/    bundled ffmpeg.exe + ffprobe.exe + license  (git-ignored, large)
installer/
  kratr.iss       Inno Setup script  → installer/out/KRATR-Setup-v<ver>.exe
  assets/         license notice + the first-run README the wizard shows
dist-friend/      PyInstaller output: KRATR.exe + kratr-cli.exe
```

## Windows

You need Python 3.13. From the repo root:

```bash
python -m venv .venv
.venv/Scripts/pip install PySide6 pyrekordbox numpy psutil mutagen pyinstaller

# 1. Build the executables
.venv/Scripts/pyinstaller crate.spec --distpath dist-friend --workpath build-friend --noconfirm

# 2. Put a static ffmpeg build into vendor/ffmpeg/
#    ffmpeg.exe + ffprobe.exe from ffmpeg-release-essentials.zip
#    (https://www.gyan.dev/ffmpeg/builds/)

# 3. Compile the installer with Inno Setup 6
"$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe" installer/kratr.iss
#    → installer/out/KRATR-Setup-v0.1.0.exe
```

Set the version by editing `crate/__init__.py` and passing `/DAppVersion=x.y.z` to ISCC. The
installer runs per-user with no admin prompt, bundles ffmpeg, and walks the user through
setup on first launch. It's unsigned, so Windows SmartScreen throws its blue "Windows
protected your PC" screen. The fix is **More info → Run anyway**. Whoever installs it still
needs rekordbox installed and opened once, so its library exists.

## macOS

A Mac build can't be produced on Windows, because PyInstaller doesn't cross-compile, so the
`.app` gets built on a Mac. The double-clickable `build-mac.command` handles the whole run,
and [MAC-BUILD.md](MAC-BUILD.md) walks through it step by step. The pieces that ship for
macOS:

- `crate-mac.spec`, the PyInstaller spec that produces `dist/KRATR.app`.
- `build-mac.command`, which sets up Python, the dependencies, and Mac ffmpeg, builds the
  app, and packages `KRATR.dmg`.
- `crate/resources/kratr-icon.icns`, the Mac app icon.

The code is already macOS-aware: `config.py` uses `~/Library/Application Support/Crate` and
`~/Library/Pioneer/rekordbox`, and `bundled_tool_dirs()` finds ffmpeg inside the `.app`. The
Mac app is unsigned too, so the first launch is right-click → Open. Removing that step needs
a paid Apple Developer ID for signing and notarization.

## Cutting a release

Push a version tag and GitHub Actions builds and publishes both installers for you:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The workflow in `.github/workflows/release.yml` stamps the version into `crate/__init__.py`,
builds the Windows installer and the macOS `.dmg` on cloud runners, and attaches both to a
generated GitHub Release. You can also run it by hand from the Actions tab to test without
publishing.
