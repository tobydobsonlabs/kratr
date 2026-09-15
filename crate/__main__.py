"""Entry point.

``kratr``            launch the app
``kratr verify``     check the runtime and dependencies work on this machine
``kratr doctor``     read-only health check of the rekordbox library
"""

from __future__ import annotations

import argparse
import sys

from . import __version__, config


def cmd_verify(_args: argparse.Namespace) -> int:
    """Prove the two risky dependencies work on this Python before anything is built on them."""
    ok = True

    print(f"{config.APP_NAME} {__version__}")
    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    print()

    # --- PySide6 ---------------------------------------------------------
    try:
        import PySide6
        from PySide6 import QtCore
        from PySide6.QtWidgets import QApplication, QLabel

        print(f"[ok]   PySide6 {PySide6.__version__} (Qt {QtCore.qVersion()})")
        app = QApplication.instance() or QApplication([])
        label = QLabel(config.APP_NAME)
        label.show()
        QtCore.QTimer.singleShot(0, app.quit)
        app.exec()
        label.close()
        print("[ok]   Qt window created and event loop ran")
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic
        ok = False
        print(f"[FAIL] PySide6: {exc!r}")

    # --- pyrekordbox + SQLCipher ----------------------------------------
    try:
        import pyrekordbox

        print(f"[ok]   pyrekordbox {pyrekordbox.__version__}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] pyrekordbox import: {exc!r}")
        return 1

    if not config.REKORDBOX_DB_PATH.is_file():
        print(f"[warn] No rekordbox database at {config.REKORDBOX_DB_PATH}")
    else:
        try:
            from .rekordbox.sandbox import Sandbox

            with Sandbox.create() as sb:
                db = sb.open()
                n_tracks = db.get_content().count()
                n_playlists = db.get_playlist().count()
                n_tags = db.get_my_tag().count()
                print(
                    f"[ok]   Unlocked a COPY of master.db — "
                    f"{n_tracks} tracks, {n_playlists} playlists, {n_tags} MyTags"
                )
                print(f"[ok]   Sandbox pristine check: {sb.is_pristine()}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[FAIL] opening master.db copy: {exc!r}")

    # --- ffmpeg ----------------------------------------------------------
    for tool in ("ffmpeg", "ffprobe"):
        path = config.find_executable(tool)
        if path:
            print(f"[ok]   {tool}: {path}")
        else:
            ok = False
            print(f"[FAIL] {tool} not found on PATH")

    print()
    print("VERIFY PASSED" if ok else "VERIFY FAILED")
    return 0 if ok else 1


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Read-only health check. Run after every rekordbox update, before writing."""
    from .rekordbox import doctor

    diagnosis = doctor.run()
    print(diagnosis.render())
    return 0 if diagnosis.ok else 1


def cmd_audit(args: argparse.Namespace) -> int:
    """Scan the whole library for upscales. Read-only; changes nothing."""
    from pathlib import Path

    from .pipeline import audit

    settings = config.Settings.load()
    root = Path(args.root) if args.root else settings.music_root_path
    print(f"Auditing {root} …  (read-only; nothing is modified)")

    def progress(done: int, total: int, path: Path) -> None:
        if done % 25 == 0 or done == total:
            print(f"  [{done}/{total}] {path.name[:60]}")

    rows = audit.run(root, settings, workers=args.workers, limit=args.limit, progress=progress)
    output = Path(args.output) if args.output else config.APP_DIR / "library-audit.csv"
    audit.write_report(rows, output)

    print()
    print(audit.summarise(rows))
    print()
    print(f"Full ranked report: {output}")
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    from .ui.app import run

    return run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kratr", description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("verify", help="check dependencies work on this machine").set_defaults(
        func=cmd_verify
    )
    sub.add_parser("doctor", help="read-only health check of the rekordbox library").set_defaults(
        func=cmd_doctor
    )
    audit_parser = sub.add_parser(
        "audit", help="scan the whole library for upscaled tracks (read-only)"
    )
    audit_parser.add_argument("--root", help="folder to scan (defaults to your music root)")
    audit_parser.add_argument("--output", help="where to write the CSV report")
    audit_parser.add_argument("--workers", type=int, default=4)
    audit_parser.add_argument("--limit", type=int, help="stop after N tracks (for a trial run)")
    audit_parser.set_defaults(func=cmd_audit)

    sub.add_parser("gui", help="launch the app").set_defaults(func=cmd_gui)

    args = parser.parse_args(argv)

    # The Windows console defaults to a legacy code page, which mangles the separators
    # and arrows used throughout the reports.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    config.setup_logging(args.verbose)

    func = getattr(args, "func", cmd_gui)
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
