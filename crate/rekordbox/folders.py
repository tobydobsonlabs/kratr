"""Coordinated folder moves that keep rekordbox content paths valid."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .importer import find_by_path, normalise_path

logger = logging.getLogger(__name__)


class FolderEditError(RuntimeError):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def move_and_reroute(db: Any, old_path: str, new_path: str, music_root: str) -> int:
    """Move a directory and rewrite every affected collection path in place.

    Content IDs never change, so cues, analysis, ratings, tags and playlist links all
    remain attached. If updating the database fails, the directory is moved back
    before the error escapes.
    """
    from sqlalchemy import func
    from pyrekordbox.db6 import tables

    root = Path(music_root).resolve()
    old = Path(old_path).resolve()
    new = Path(new_path).resolve()
    if not _inside(old, root) or not _inside(new, root):
        raise FolderEditError("Folder edits must stay inside the configured music root")
    if old == root:
        raise FolderEditError("The music root itself cannot be moved")
    if not old.is_dir():
        raise FolderEditError(f"Folder does not exist: {old}")
    if new.exists():
        raise FolderEditError(f"Destination already exists: {new}")
    if _inside(new, old):
        raise FolderEditError("A folder cannot be moved inside itself")

    old_prefix = normalise_path(old).rstrip("/")
    new_prefix = normalise_path(new).rstrip("/")
    rows = (
        db.query(tables.DjmdContent)
        .filter(func.lower(tables.DjmdContent.FolderPath).like(old_prefix.lower() + "/%"))
        .all()
    )
    originals = [(row, row.FolderPath) for row in rows]
    affected_ids = {str(row.ID) for row in rows}
    for row, original in originals:
        candidate = new_prefix + str(original)[len(old_prefix):]
        clash = find_by_path(db, candidate)
        if clash is not None and str(clash.ID) not in affected_ids:
            raise FolderEditError(
                f"Another collection entry already points at {candidate} (ID {clash.ID})"
            )

    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    try:
        for row, original in originals:
            suffix = str(original)[len(old_prefix):]
            row.FolderPath = new_prefix + suffix
        db.flush()
    except Exception:
        for row, original in originals:
            row.FolderPath = original
        try:
            db.flush()
        finally:
            if new.exists() and not old.exists():
                new.rename(old)
        raise

    logger.info("Moved folder %s → %s and rerouted %d track(s)", old, new, len(rows))
    return len(rows)


def roll_back_move(old_path: str, new_path: str) -> None:
    """Compensate a successful move when the surrounding DB commit fails."""
    old = Path(old_path)
    new = Path(new_path)
    if old.exists():
        return
    if not new.is_dir():
        raise FolderEditError(f"Cannot restore {old}; moved folder is missing at {new}")
    old.parent.mkdir(parents=True, exist_ok=True)
    new.rename(old)
    logger.warning("Rolled folder move back: %s → %s", new, old)
