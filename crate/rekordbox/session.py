"""The only place KRATR is allowed to open the rekordbox library for writing.

Two hard rules, both non-negotiable:

1. **Never write while rekordbox is running.** It holds the library in memory and
   flushes on exit, so writing underneath it loses changes or corrupts them. This is
   a block, not a warning.
2. **Always back up first** — and back up the WAL sidecars too. The database runs in
   WAL mode (verified: ``PRAGMA journal_mode`` returns ``wal``), so a copy of
   ``master.db`` alone can be missing committed changes, and a restore that leaves a
   stale ``-wal`` behind will have it replay straight back over the restored file.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psutil

from .. import config
from .schema import SchemaReport, SchemaState, check

logger = logging.getLogger(__name__)


class RekordboxRunningError(RuntimeError):
    """rekordbox is open, so writing is refused."""


class SchemaBlockedError(RuntimeError):
    """The schema isn't verified, so writing is refused."""


@dataclass(frozen=True, slots=True)
class Backup:
    directory: Path
    created: float

    @property
    def label(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created))

    @property
    def age_hours(self) -> float:
        return (time.time() - self.created) / 3600


def rekordbox_processes() -> list[psutil.Process]:
    """Any running rekordbox process."""
    found = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in {n.lower() for n in config.REKORDBOX_PROCESS_NAMES}:
            found.append(proc)
    return found


def is_rekordbox_running() -> bool:
    return bool(rekordbox_processes())


# ------------------------------------------------------------------- backups
def create_backup(
    source_dir: Path | None = None, backup_root: Path | None = None
) -> Backup:
    """Snapshot the library before touching it.

    Copies the WAL sidecars alongside the database — without them the snapshot can
    be missing changes that were committed but not yet checkpointed.
    """
    source_dir = source_dir or config.REKORDBOX_DB_DIR
    backup_root = backup_root or config.BACKUP_DIR
    backup_root.mkdir(parents=True, exist_ok=True)

    directory = backup_root / time.strftime("%Y%m%d-%H%M%S")
    suffix = 1
    while directory.exists():
        suffix += 1
        directory = backup_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{suffix}"
    directory.mkdir(parents=True)

    copied = []
    for name in (*config.REKORDBOX_BACKUP_FILES, *config.REKORDBOX_SIDECAR_FILES):
        src = source_dir / name
        if src.is_file():
            shutil.copy2(src, directory / name)
            copied.append(name)

    if "master.db" not in copied:
        shutil.rmtree(directory, ignore_errors=True)
        raise FileNotFoundError(f"No master.db in {source_dir}")

    logger.info("Backed up %s → %s", ", ".join(copied), directory)
    return Backup(directory, time.time())


def list_backups(backup_root: Path | None = None) -> list[Backup]:
    """Newest first."""
    backup_root = backup_root or config.BACKUP_DIR
    if not backup_root.is_dir():
        return []
    backups = [
        Backup(d, d.stat().st_mtime)
        for d in backup_root.iterdir()
        if d.is_dir() and (d / "master.db").is_file()
    ]
    return sorted(backups, key=lambda b: b.created, reverse=True)


def prune_backups(keep: int, backup_root: Path | None = None) -> int:
    removed = 0
    for backup in list_backups(backup_root)[keep:]:
        shutil.rmtree(backup.directory, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("Pruned %d old backup(s)", removed)
    return removed


def restore_backup(backup: Backup, target_dir: Path | None = None) -> None:
    """Put a backup back.

    Deletes the live WAL sidecars first — otherwise a stale ``-wal`` replays the very
    changes being rolled back, straight over the restored file.
    """
    target_dir = target_dir or config.REKORDBOX_DB_DIR
    if is_rekordbox_running():
        raise RekordboxRunningError("Close rekordbox before restoring a backup.")

    for name in config.REKORDBOX_SIDECAR_FILES:
        (target_dir / name).unlink(missing_ok=True)

    for name in (*config.REKORDBOX_BACKUP_FILES, *config.REKORDBOX_SIDECAR_FILES):
        src = backup.directory / name
        if src.is_file():
            shutil.copy2(src, target_dir / name)

    logger.warning("Restored library from %s", backup.directory)


# ------------------------------------------------------------------- session
class LibrarySession:
    """An open, writable rekordbox database."""

    def __init__(self, db: Any, backup: Backup | None, report: SchemaReport) -> None:
        self.db = db
        self.backup = backup
        self.schema = report
        self._rollback_actions: list[Callable[[], None]] = []

    def add_rollback_action(self, action: Callable[[], None]) -> None:
        """Register a non-database side effect to undo if the transaction fails."""
        self._rollback_actions.append(action)

    def _run_rollback_actions(self) -> None:
        actions, self._rollback_actions = self._rollback_actions, []
        for action in reversed(actions):
            try:
                action()
            except Exception:  # noqa: BLE001 - attempt every compensating action
                logger.exception("Could not roll back a filesystem side effect")

    def checkpoint(self) -> None:
        """Fold the WAL back into master.db so the file on disk is complete."""
        from sqlalchemy import text

        try:
            with self.db.engine.connect() as conn:
                conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        except Exception:  # noqa: BLE001 - best effort
            logger.debug("wal_checkpoint failed", exc_info=True)

    def commit(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            self._run_rollback_actions()
            raise
        self._rollback_actions.clear()
        self.checkpoint()

    def rollback(self) -> None:
        self.db.rollback()
        self._run_rollback_actions()

    def close(self) -> None:
        self.checkpoint()
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            logger.debug("close failed", exc_info=True)
        try:
            self.db.engine.dispose()
        except Exception:  # noqa: BLE001
            logger.debug("dispose failed", exc_info=True)


@contextmanager
def open_for_write(
    settings: config.Settings | None = None,
    *,
    db_path: Path | None = None,
    backup: bool = True,
    require_schema_ok: bool = True,
) -> Iterator[LibrarySession]:
    """Open the library for writing, with every safety check applied.

    Commits on clean exit; rolls back on any exception. A failed write must leave the
    library exactly as it was.
    """
    settings = settings or config.Settings.load()

    if is_rekordbox_running():
        raise RekordboxRunningError(
            "rekordbox is running. Close it before applying changes — it holds the "
            "library in memory and would overwrite anything KRATR writes."
        )

    snapshot = None
    if backup:
        snapshot = create_backup(
            (db_path.parent if db_path else None) or config.REKORDBOX_DB_DIR
        )
        prune_backups(settings.backup_retention)

    from pyrekordbox import Rekordbox6Database

    db = Rekordbox6Database(path=str(db_path or config.REKORDBOX_DB_PATH), unlock=True)
    report = check(db, settings)

    if require_schema_ok and not report.writes_allowed:
        db.close()
        raise SchemaBlockedError(report.summary())

    session = LibrarySession(db, snapshot, report)
    try:
        yield session
    except Exception:
        logger.exception("Write session failed — rolling back")
        session.rollback()
        raise
    else:
        session.commit()
    finally:
        session.close()


def status(settings: config.Settings | None = None) -> dict[str, Any]:
    """Everything the UI needs to decide whether applying is possible right now."""
    settings = settings or config.Settings.load()
    backups = list_backups()
    return {
        "rekordbox_running": is_rekordbox_running(),
        "database_present": config.REKORDBOX_DB_PATH.is_file(),
        "backup_count": len(backups),
        "latest_backup": backups[0].label if backups else None,
        "latest_backup_age_hours": backups[0].age_hours if backups else None,
    }


__all__ = [
    "Backup",
    "LibrarySession",
    "RekordboxRunningError",
    "SchemaBlockedError",
    "SchemaState",
    "create_backup",
    "is_rekordbox_running",
    "list_backups",
    "open_for_write",
    "prune_backups",
    "restore_backup",
    "status",
]
