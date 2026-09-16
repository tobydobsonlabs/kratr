# Building KRATR for macOS

Most Mac users never need this page. Every release ships a ready-made `KRATR.dmg` on the
[Releases page](../../releases/latest): download it, drag KRATR into Applications, and
you're set. This page is for building the app yourself from a clone of the repo.

Apple Silicon (M1/M2/M3/M4) or Intel, either works. The build produces an x86_64 app that
runs natively on Intel and through Rosetta on Apple Silicon.

---

## Build it

Get the repo onto a Mac first. Clone it, or download the ZIP from GitHub and unzip it, and
keep the folder intact.

**1. Double-click `build-mac.command`.**

The first time, macOS may say the file *"cannot be opened because it is from an
unidentified developer."* Right-click (or Control-click) the file, choose **Open**, then
**Open** again. You only do this once.

Sometimes double-clicking opens the script as plain text instead of running it. That means
the file lost its executable bit, which is common after a round-trip through a Windows ZIP.
Fix it once from Terminal:

1. Open the **Terminal** app. Press Cmd-Space, type "Terminal", press Return.
2. Type `chmod +x ` with a trailing space.
3. Drag `build-mac.command` from Finder into the Terminal window. The path pastes itself.
4. Press Return, then double-click `build-mac.command` again.

**2. Let it run.** A Terminal window opens and gets to work. The first run downloads the
Python packages, PyInstaller, and a static ffmpeg build, so give it 10 to 20 minutes. Walls
of scrolling text are normal. If macOS offers to install *"developer tools"*, click
**Install**, wait for it to finish, then run `build-mac.command` again.

**3. Collect the output.** When it prints "Done!", the same folder holds two things:
`KRATR.dmg`, the installer, and `dist/KRATR.app`, the app itself. You can close Terminal.

---

## Installing the `.dmg`

Open `KRATR.dmg`, drag the **KRATR** icon onto the **Applications** shortcut beside it, then
open Applications and, the first time only, right-click **KRATR → Open → Open**. Opening it
the ordinary way on that first launch can pop a warning and refuse. After that, launch it
however you like. First launch runs KRATR's short setup walkthrough, the same one Windows
shows.

Why the right-click? KRATR is ad-hoc signed rather than signed with an Apple Developer ID,
and that certificate costs $99 a year. So macOS stays cautious the first time it sees the
app. Right-click → Open tells it you trust the thing. Nothing here is unsafe. It's the same
tool the Windows installer ships, without the Apple toll.

---

## When the build breaks

- **"The build failed" in Terminal.** Scroll up to the first red line. A dropped connection
  during the downloads is the usual cause, so run `build-mac.command` again before anything
  else. If it keeps failing, open an issue on GitHub and paste that first red line.
- **App won't open, or calls itself "damaged".** Run `xattr -cr /Applications/KRATR.app` in
  Terminal, then try again.
- **rekordbox not found.** Install rekordbox and open it once, so its library exists at
  `~/Library/Pioneer/rekordbox`.
- **Deeper diagnostics.** Run `/Applications/KRATR.app/Contents/MacOS/KRATR verify`.

---

## How the build works

`build-mac.command` creates a `.venv-mac`, installs the dependencies plus PyInstaller, pulls
static `ffmpeg` and `ffprobe` from evermeet.cx into `vendor/ffmpeg-mac/`, runs
`pyinstaller crate-mac.spec`, ad-hoc-signs the resulting `.app`, and wraps it into
`KRATR.dmg`. The GitHub Actions release workflow runs the exact same steps on a cloud runner
for every tagged version.

The evermeet ffmpeg binaries are x86_64, which is why the whole app is built x86_64. One
`.dmg` then covers both architectures: it runs natively on Intel, and Rosetta 2 fills the
gap on Apple chips, where macOS prompts to install Rosetta on first use. The code already
knows about macOS. `config.py` keeps app data under `~/Library/Application Support/Crate`
and reads rekordbox from `~/Library/Pioneer/rekordbox`, and `bundled_tool_dirs()` locates
ffmpeg inside the bundle.

Want to drop the right-click-to-open step for everyone who installs the `.dmg`? Sign the app
with a Developer ID certificate and notarize it with `notarytool`. Both need a paid Apple
Developer account.
