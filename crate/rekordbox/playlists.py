"""Safe playlist-tree edits that preserve track membership by playlist ID."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PlaylistError(RuntimeError):
    pass


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise PlaylistError("A playlist needs a name")
    return cleaned


def _row(db: Any, playlist_id: str) -> Any:
    row = db.get_playlist(ID=str(playlist_id))
    if row is None:
        raise PlaylistError(f"No playlist with ID {playlist_id}")
    return row


def _parent(db: Any, parent_id: str | None) -> Any | None:
    if parent_id in (None, "", "root"):
        return None
    row = _row(db, str(parent_id))
    if row.Attribute != 1:
        raise PlaylistError(f"{row.Name!r} is not a playlist folder")
    return row


def create(
    db: Any, name: str, *, parent_id: str | None = None, folder: bool = False
) -> Any:
    """Create a normal playlist or folder using pyrekordbox's tracked API."""
    cleaned = _clean_name(name)
    parent = _parent(db, parent_id)
    siblings = db.get_playlist(ParentID=str(parent.ID) if parent is not None else "root")
    if any((p.Name or "").casefold() == cleaned.casefold() for p in siblings):
        raise PlaylistError(f"A playlist named {cleaned!r} already exists here")
    creator = db.create_playlist_folder if folder else db.create_playlist
    row = creator(cleaned, parent=parent)
    db.flush()
    return row


def rename(db: Any, playlist_id: str, name: str) -> Any:
    """Rename in place; song links remain attached to the unchanged ID."""
    row = _row(db, playlist_id)
    cleaned = _clean_name(name)
    db.rename_playlist(row, cleaned)
    db.flush()
    logger.info("Renamed playlist %s → %r", playlist_id, cleaned)
    return row


def _descendant_ids(db: Any, playlist_id: str) -> set[str]:
    descendants: set[str] = set()
    frontier = [str(playlist_id)]
    while frontier:
        parent_id = frontier.pop()
        for child in db.get_playlist(ParentID=parent_id):
            child_id = str(child.ID)
            if child_id not in descendants:
                descendants.add(child_id)
                frontier.append(child_id)
    return descendants


def move(db: Any, playlist_id: str, parent_id: str | None) -> Any:
    """Move a playlist/folder without replacing it or its song links."""
    row = _row(db, playlist_id)
    parent = _parent(db, parent_id)
    target_id = str(parent.ID) if parent is not None else "root"
    if str(row.ID) == target_id or target_id in _descendant_ids(db, str(row.ID)):
        raise PlaylistError("A playlist folder cannot be moved inside itself")
    if str(row.ParentID) == target_id:
        return row

    if parent is not None:
        db.move_playlist(row, parent=parent)
    else:
        # pyrekordbox treats parent=None as “keep the current parent”, so moving to
        # the root needs the same tracked row updates explicitly.
        old_parent = str(row.ParentID)
        old_seq = row.Seq or 0
        root_count = db.get_playlist(ParentID="root").count()
        row.ParentID = "root"
        row.Seq = root_count + 1
        for sibling in db.get_playlist(ParentID=old_parent):
            if str(sibling.ID) != str(row.ID) and (sibling.Seq or 0) > old_seq:
                sibling.Seq -= 1
    db.flush()
    logger.info("Moved playlist %s → %s", playlist_id, target_id)
    return row


def delete(db: Any, playlist_id: str) -> int:
    """Delete one playlist, refusing non-empty folders and counting lost links."""
    from pyrekordbox.db6 import tables

    row = _row(db, playlist_id)
    children = db.get_playlist(ParentID=str(row.ID)).count()
    if row.Attribute == 1 and children:
        raise PlaylistError(
            f"{row.Name!r} contains {children} item(s). Move or delete those first."
        )

    links = db.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=str(row.ID)).all()
    for link in links:
        db.delete(link)
    db.flush()
    db.delete_playlist(row)
    db.flush()
    logger.info("Deleted playlist %s and %d song link(s)", playlist_id, len(links))
    return len(links)

