KRATR — quick start
===================

Thanks for trying KRATR. It takes a dropped track and, in one pass, converts it,
quality-checks it, files it into your genre folders, imports it into rekordbox, and
adds playlists, tags and a colour.

FIRST LAUNCH
------------
The first time you open KRATR it runs a short setup walkthrough:
  1. A quick check that everything works (ffmpeg is bundled, so this should pass).
  2. It finds your rekordbox library and shows what's in it.
  3. You pick your preferred format for lossless tracks (WAV, AIFF, or keep as-is).
  4. You point KRATR at the top folder of your music library.
That's it — you only do this once.

BEFORE YOU START
----------------
- Install rekordbox and open it at least once, so its library exists.
- Always let KRATR back up your library (it does this automatically). Your own
  backups are still your responsibility.
- KRATR will NOT write while rekordbox is open — close rekordbox before importing.

"WINDOWS PROTECTED YOUR PC"
--------------------------
KRATR isn't signed with a paid certificate, so Windows SmartScreen may show a blue
warning when you run the installer. This is expected for a small tool shared between
friends. To continue: click "More info", then "Run anyway".

TROUBLESHOOTING
---------------
- Something not working? Open "kratr-cli.exe" in the install folder and it will print
  a report. Useful commands (open a terminal in the install folder):
      kratr-cli verify     check everything works on this machine
      kratr-cli doctor     read-only health check of your rekordbox library
- Logs live in:  %APPDATA%\Crate\logs\kratr.log

Uninstall any time from Windows "Add or remove programs".
