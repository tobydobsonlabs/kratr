"""Read-only health check. Run it after every rekordbox update, before writing.

When rekordbox does change something, this is what turns "the app is broken, why?"
into a small targeted fix: it names the exact tables and columns that moved.

Strictly read-only, and safe to run with rekordbox open — it works on a copy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from . import session as session_mod
from .sandbox import Sandbox
from .schema import REQUIRED, SchemaState, check, inspect_schema, installed_version

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str = ""

    @property
    def marker(self) -> str:
        return {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}[self.status]


@dataclass(slots=True)
class Diagnosis:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        lines = [f"{c.marker} {c.name}" + (f"\n         {c.detail}" if c.detail else "")
                 for c in self.checks]
        lines.append("")
        lines.append("DOCTOR PASSED" if self.ok else "DOCTOR FOUND PROBLEMS")
        return "\n".join(lines)


def run(settings: config.Settings | None = None) -> Diagnosis:
    settings = settings or config.Settings.load()
    diagnosis = Diagnosis()

    # --- environment ------------------------------------------------------
    version = installed_version()
    diagnosis.add(
        "rekordbox installed",
        OK if version else WARN,
        f"version {version}" if version else "could not read version from the registry",
    )

    if not config.REKORDBOX_DB_PATH.is_file():
        diagnosis.add("database present", FAIL, f"not found at {config.REKORDBOX_DB_PATH}")
        return diagnosis
    size_mb = config.REKORDBOX_DB_PATH.stat().st_size / 1e6
    diagnosis.add("database present", OK, f"{config.REKORDBOX_DB_PATH} ({size_mb:.1f} MB)")

    diagnosis.add(
        "rekordbox not running",
        OK if not session_mod.is_rekordbox_running() else WARN,
        "closed" if not session_mod.is_rekordbox_running()
        else "running — reads are fine, but writing is blocked until it closes",
    )

    for tool in ("ffmpeg", "ffprobe"):
        path = config.find_executable(tool)
        diagnosis.add(tool, OK if path else FAIL, path or "not found on PATH")

    # --- the library itself, via a throwaway copy -------------------------
    sandbox: Sandbox | None = None
    try:
        sandbox = Sandbox.create()
    except Exception as exc:  # noqa: BLE001
        diagnosis.add("open a copy of the database", FAIL, f"{type(exc).__name__}: {exc}")
        return diagnosis

    try:
        try:
            db = sandbox.open()
        except Exception as exc:  # noqa: BLE001
            diagnosis.add(
                "unlock the database",
                FAIL,
                f"{type(exc).__name__}: {exc}\n"
                f"         Key extraction may have broken. Try "
                f"`python -m pyrekordbox download-key`, or set the key in settings.",
            )
            return diagnosis

        diagnosis.add("unlock the database", OK, "SQLCipher key extraction works")

        _check_schema(diagnosis, db, settings)
        _check_contents(diagnosis, sandbox)
    finally:
        if sandbox is not None:
            sandbox.destroy()

    _check_backups(diagnosis, settings)
    return diagnosis


def _check_schema(diagnosis: Diagnosis, db: Any, settings: config.Settings) -> None:
    report = check(db, settings)
    live, _ = inspect_schema(db)

    if report.state is SchemaState.BLOCKED:
        detail = "\n         ".join(
            f"{table}: missing {', '.join(cols)}" for table, cols in report.missing.items()
        )
        diagnosis.add("required schema", FAIL, detail)
    else:
        total = sum(len(cols) for cols in REQUIRED.values())
        diagnosis.add(
            "required schema",
            OK,
            f"all {total} required columns across {len(REQUIRED)} tables present",
        )

    diagnosis.add(
        "schema fingerprint",
        OK if report.state is SchemaState.OK else WARN,
        f"{report.fingerprint} over {report.table_count} tables"
        + (f" — {'; '.join(report.changes)}" if report.changes else ""),
    )

    # Additive drift is expected and harmless; report it so it's visible, not alarming.
    from pyrekordbox.db6 import tables as t

    extra: list[str] = []
    for logical, required_cols in REQUIRED.items():
        table_name = getattr(t, logical).__table__.name
        if table_name in live:
            added = live[table_name] - required_cols
            if added:
                extra.append(f"{table_name}: +{len(added)} column(s) KRATR does not use")
    if extra:
        diagnosis.add("additive drift", OK, "\n         ".join(extra))


def _check_contents(diagnosis: Diagnosis, sandbox: Sandbox) -> None:
    from pyrekordbox.db6 import tables

    db = sandbox.open()
    tracks = db.get_content().count()
    playlists = db.get_playlist().count()
    my_tags = db.get_my_tag().count()
    colours = db.query(tables.DjmdColor).count()
    diagnosis.add(
        "library contents",
        OK,
        f"{tracks} tracks · {playlists} playlists · {my_tags} MyTags · {colours} colours",
    )

    orphans = (
        db.query(tables.DjmdSongMyTag)
        .filter(~tables.DjmdSongMyTag.ContentID.in_(db.query(tables.DjmdContent.ID)))
        .count()
    )
    diagnosis.add(
        "MyTag links intact",
        OK if orphans == 0 else WARN,
        "no orphaned links" if orphans == 0 else f"{orphans} link(s) point at missing tracks",
    )


def _check_backups(diagnosis: Diagnosis, settings: config.Settings) -> None:
    backups = session_mod.list_backups()
    if not backups:
        diagnosis.add(
            "backups", WARN, "none yet — one is taken automatically before the first write"
        )
        return
    newest = backups[0]
    diagnosis.add(
        "backups",
        OK,
        f"{len(backups)} kept (retain {settings.backup_retention}); "
        f"newest {newest.label}, {newest.age_hours:.1f}h ago",
    )


def render(settings: config.Settings | None = None) -> str:
    return run(settings).render()
