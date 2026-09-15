"""Detect rekordbox updates, and refuse to write into a schema we haven't verified.

rekordbox is a closed, undocumented format that updates often, and **pyrekordbox
performs no version or schema checking whatsoever** — it will happily open a schema
it doesn't understand and write into it. Guarding against that is this module's job.

The policy is fail-closed, but not brittle:

* **BLOCKED** — a table or column KRATR actually writes has gone. Writes are
  impossible until the code is updated. This is the real breakage case.
* **REVIEW** — the rekordbox version or overall schema changed, but everything KRATR
  needs is still present. Writes are held until acknowledged once, so a routine point
  release costs one click rather than a broken app.
* **OK** — matches the last known-good state.

Additive drift is tolerated deliberately: rekordbox adding columns elsewhere is
routine and must not be treated as a break.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from .. import config

logger = logging.getLogger(__name__)


class SchemaState(enum.Enum):
    OK = "ok"
    REVIEW = "review"
    BLOCKED = "blocked"

    @property
    def writes_allowed(self) -> bool:
        return self is SchemaState.OK


#: Every table and column KRATR writes to. Verified against rekordbox 7.2.6.
#: Only these gate writes — anything else changing is drift, not breakage.
REQUIRED: dict[str, set[str]] = {
    "DjmdContent": {
        "ID", "FolderPath", "FileNameL", "FileType", "FileSize", "Title",
        "ArtistID", "AlbumID", "GenreID", "Rating", "DJPlayCount",
        "AnalysisDataPath", "ColorID", "Commnt", "UUID", "rb_local_usn",
        "BitRate", "SampleRate", "Length",
    },
    "DjmdPlaylist": {"ID", "Name", "ParentID", "Attribute", "Seq", "UUID", "rb_local_usn"},
    "DjmdSongPlaylist": {"ID", "PlaylistID", "ContentID", "TrackNo", "UUID", "rb_local_usn"},
    "DjmdMyTag": {"ID", "Seq", "Name", "Attribute", "ParentID", "UUID", "rb_local_usn"},
    "DjmdSongMyTag": {"ID", "MyTagID", "ContentID", "TrackNo", "UUID", "rb_local_usn"},
    "DjmdColor": {"ID", "ColorCode", "SortKey", "Commnt", "UUID", "rb_local_usn"},
}


@dataclass(slots=True)
class SchemaReport:
    state: SchemaState
    rekordbox_version: str | None
    fingerprint: str
    table_count: int
    #: Required things that have gone. Non-empty means BLOCKED.
    missing: dict[str, list[str]] = field(default_factory=dict)
    #: What changed since the last known-good run, for the user to read.
    changes: list[str] = field(default_factory=list)

    @property
    def writes_allowed(self) -> bool:
        return self.state.writes_allowed

    def summary(self) -> str:
        if self.state is SchemaState.OK:
            return f"rekordbox {self.rekordbox_version or '?'} — schema verified."
        if self.state is SchemaState.BLOCKED:
            broken = "; ".join(f"{t}: {', '.join(c)}" for t, c in self.missing.items())
            return (
                f"Writing is blocked. rekordbox {self.rekordbox_version or '?'} no longer has "
                f"something KRATR writes to — {broken}. KRATR needs updating before it can "
                f"safely touch your library again. Everything up to filing still works."
            )
        return (
            f"rekordbox looks different from last time "
            f"({'; '.join(self.changes) or 'schema changed'}). Everything KRATR needs is "
            f"still there, so this is probably a routine update — confirm once to continue."
        )


def installed_version() -> str | None:
    """Read the installed rekordbox version from the Windows uninstall registry."""
    if sys.platform != "win32":
        return None
    import winreg

    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                            name = winreg.QueryValueEx(sub, "DisplayName")[0]
                            if "rekordbox" in str(name).lower():
                                return str(winreg.QueryValueEx(sub, "DisplayVersion")[0])
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def _orm_tables() -> dict[str, Any]:
    """Map our logical names to the ORM classes, so real table names are never guessed."""
    from pyrekordbox.db6 import tables as t

    return {name: getattr(t, name) for name in REQUIRED}


def inspect_schema(db: Any) -> tuple[dict[str, set[str]], str]:
    """Return the live (table -> columns) map and a fingerprint over all of it."""
    import warnings

    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(db.engine)
    live: dict[str, set[str]] = {}
    with warnings.catch_warnings():
        # rekordbox declares columns like INTEGER(1), which SQLAlchemy can't rebuild
        # from reflection. Harmless — we only read column *names* here.
        warnings.filterwarnings("ignore", message="Could not instantiate type")
        for table in inspector.get_table_names():
            live[table] = {c["name"] for c in inspector.get_columns(table)}

    digest = hashlib.sha256()
    for table in sorted(live):
        digest.update(f"{table}:{','.join(sorted(live[table]))}\n".encode())
    return live, digest.hexdigest()[:16]


def check(db: Any, settings: config.Settings | None = None) -> SchemaReport:
    """Compare the live schema against what KRATR needs and last saw."""
    settings = settings or config.Settings.load()
    version = installed_version()
    live, fingerprint = inspect_schema(db)

    # --- the gate: is everything we write to still there? ------------------
    missing: dict[str, list[str]] = {}
    for logical, model in _orm_tables().items():
        table_name = model.__table__.name
        if table_name not in live:
            missing[table_name] = ["<entire table>"]
            continue
        gone = sorted(REQUIRED[logical] - live[table_name])
        if gone:
            missing[table_name] = gone

    if missing:
        logger.error("Schema check BLOCKED: %s", missing)
        return SchemaReport(
            state=SchemaState.BLOCKED,
            rekordbox_version=version,
            fingerprint=fingerprint,
            table_count=len(live),
            missing=missing,
        )

    # --- drift since the last confirmed run --------------------------------
    changes: list[str] = []
    if settings.known_rekordbox_version and settings.known_rekordbox_version != version:
        changes.append(f"version {settings.known_rekordbox_version} → {version}")
    if settings.known_schema_fingerprint and settings.known_schema_fingerprint != fingerprint:
        changes.append("schema fingerprint changed")

    if settings.known_schema_fingerprint is None:
        # First run: nothing to compare against, and the required schema is intact.
        state = SchemaState.OK
    else:
        state = SchemaState.REVIEW if changes else SchemaState.OK

    return SchemaReport(
        state=state,
        rekordbox_version=version,
        fingerprint=fingerprint,
        table_count=len(live),
        changes=changes,
    )


def accept(report: SchemaReport, settings: config.Settings) -> None:
    """Record a schema as known-good, clearing REVIEW.

    Only ever called from an explicit user action — never automatically, or the
    check would silently accept whatever it found and protect nothing.
    """
    if report.state is SchemaState.BLOCKED:
        raise ValueError("Refusing to accept a schema that is missing required columns")
    settings.known_rekordbox_version = report.rekordbox_version
    settings.known_schema_fingerprint = report.fingerprint
    settings.save()
    logger.info("Accepted schema %s (rekordbox %s)", report.fingerprint, report.rekordbox_version)
