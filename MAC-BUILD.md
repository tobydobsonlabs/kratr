# KRATR on a Mac — the simple version

You can't build a Mac app on Windows, so this needs to run **once on any Mac**. After
that you get a `KRATR.dmg` that installs like any normal Mac app (drag into
Applications) and can be shared with other Mac friends.

It doesn't matter whether the Mac is Apple Silicon (M1/M2/M3/M4) or Intel — both work.

---

## For the person with the Mac (no tech knowledge needed)

**1. Get the folder.** You'll be sent a `kratr-mac` folder (probably as a `.zip`).
Double-click the zip to unzip it. Keep everything together in one folder.

**2. Double-click `build-mac.command`.**
   - If macOS says *"cannot be opened because it is from an unidentified developer"*,
     don't worry: **right-click** (or Control-click) the file → **Open** → **Open**.
     You only have to do this once.
   - If double-clicking opens the file as **text** instead of running it (this can
     happen because it came from a Windows zip), do this once:
     1. Open the **Terminal** app (find it with Spotlight: press ⌘-Space, type
        "Terminal", press Return).
     2. Type `chmod +x ` — the word chmod, a space, x, then **another space**.
     3. **Drag `build-mac.command` from the Finder window into the Terminal window**
        and let go. It pastes the file's location for you.
     4. Press **Return**. Now double-click `build-mac.command` again — it will run.
   - A black Terminal window opens and starts working. The first time it runs it
     downloads what it needs, so give it **10–20 minutes**. You'll see lots of text —
     that's normal. If it asks you to install "developer tools", click **Install**,
     let it finish, then double-click `build-mac.command` again.

**3. When it says "Done!"** you'll have, in the same folder:
   - **`KRATR.dmg`** — the thing to share with other Mac friends.
   - `dist/KRATR.app` — the app itself.

That's it. You can close the Terminal window.

---

## Installing KRATR (you, and anyone you send `KRATR.dmg` to)

1. Double-click **`KRATR.dmg`**.
2. Drag the **KRATR** icon onto the **Applications** folder shown next to it.
3. Open **Applications**, then the **first time only**: **right-click KRATR → Open →
   Open**. (Double-clicking the normal way the first time may show a warning and
   refuse — right-click → Open gets past it. After that, open it however you like.)

The first launch runs KRATR's short setup walkthrough, same as on Windows.

> **Why the right-click step?** KRATR isn't signed with Apple's paid certificate
> ($99/year), so macOS is cautious the first time. Right-click → Open tells it you
> trust the app. Nothing about the app is unsafe — it's the same tool, just not
> paying Apple's toll.

---

## If something goes wrong

- **"The build failed"** in Terminal: scroll up to the first red line and send it to
  whoever gave you this. The most common cause is a dropped internet connection during
  the downloads — just run `build-mac.command` again.
- **App won't open at all / "damaged":** open Terminal and run
  `xattr -cr /Applications/KRATR.app`, then try again.
- **rekordbox not found:** make sure rekordbox has been installed and opened at least
  once on that Mac, so its library exists at `~/Library/Pioneer/rekordbox`.
- **Troubleshooting report:** in Terminal, run
  `/Applications/KRATR.app/Contents/MacOS/KRATR verify`.

---

## Notes for a technical helper

- Build: `build-mac.command` creates `.venv-mac`, installs deps + PyInstaller, fetches
  static `ffmpeg`/`ffprobe` from evermeet.cx into `vendor/ffmpeg-mac/`, runs
  `pyinstaller crate-mac.spec`, ad-hoc-signs the `.app`, and packages `KRATR.dmg`.
- ffmpeg from evermeet is x86_64; on Apple Silicon it runs via Rosetta 2 (macOS
  prompts to install it automatically on first use). The `.app` itself is native to
  whatever arch it's built on.
- The code is already macOS-aware: `config.py` puts app data under
  `~/Library/Application Support/Crate` and reads rekordbox from
  `~/Library/Pioneer/rekordbox`; `bundled_tool_dirs()` finds ffmpeg inside the bundle.
- To remove the right-click-to-open step for everyone, sign + notarize with an Apple
  Developer ID (`codesign` with a Developer ID cert, then `notarytool`). That needs a
  paid Apple Developer account.
