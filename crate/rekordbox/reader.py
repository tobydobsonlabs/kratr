"""Read-only view of the library, safe to use while rekordbox is running.

Reads go through a cached clone rather than the live database. That costs a 25 MB
copy once per refresh and removes any chance of the app holding a lock on the file
rekordbox is using — worth it, given the whole design rests on never interfering
with rekordbox's own writes.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..models import ExistingTrack, LibraryStatus, SourceInfo
from . import importer

logger = logging.getLogger(__name__)


def cached_clone(source_dir: Path | None = None) -> Path:
    """A clone of the library, re-copied only when the live database changes.

    Reads go through a copy so KRATR never holds a handle on the file rekordbox is
    using. Copying 24 MB on every read would be wasteful, so the clone is keyed on the
    source's size and mtime and reused until either moves.
    """
    source_dir = source_dir or config.REKORDBOX_DB_DIR
    source = source_dir / "master.db"
    if not source.is_file():
        raise FileNotFoundError(f"No master.db in {source_dir}")

    target_dir = config.CACHE_DIR / "library"
    stamp_file = target_dir / ".source"
    stat = source.stat()
    stamp = f"{stat.st_mtime_ns}:{stat.st_size}"

    if stamp_file.is_file() and stamp_file.read_text(encoding="utf-8") == stamp:
        if (target_dir / "master.db").is_file():
            return target_dir

    target_dir.mkdir(parents=True, exist_ok=True)
    for name in (*config.REKORDBOX_BACKUP_FILES, *config.REKORDBOX_SIDECAR_FILES):
        src = source_dir / name
        dst = target_dir / name
        if src.is_file():
            shutil.copy2(src, dst)
        else:
            dst.unlink(missing_ok=True)

    stamp_file.write_text(stamp, encoding="utf-8")
    logger.info("Refreshed cached library clone at %s", target_dir)
    return target_dir


@dataclass(slots=True)
class PlaylistNode:
    id: str
    name: str
    #: 0 = playlist, 1 = folder, 4 = smart playlist.
    attribute: int
    track_count: int = 0
    children: list["PlaylistNode"] = field(default_factory=list)

    @property
    def is_folder(self) -> bool:
        return self.attribute == 1

    @property
    def is_smart(self) -> bool:
        return self.attribute == 4

    @property
    def accepts_tracks(self) -> bool:
        return self.attribute == 0


class LibraryReader:
    """Cached, read-only access to the rekordbox collection."""

    def __init__(self) -> None:
        self._db: Any = None
        self._available = False
        self.error: str | None = None

    # ----------------------------------------------------------------- lifecycle
    def refresh(self) -> bool:
        """Open the library for reading. Returns True on success."""
        self.close()
        try:
            from pyrekordbox import Rekordbox6Database

            directory = cached_clone()
            self._db = Rekordbox6Database(path=str(directory / "master.db"), unlock=True)
            self._available = True
            self.error = None
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            logger.warning("Could not open the rekordbox library: %s", exc)
            self._db = None
            self._available = False
            self.error = f"{type(exc).__name__}: {exc}"
        return self._available

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
                self._db.engine.dispose()
            except Exception:  # noqa: BLE001
                logger.debug("Error closing the reader", exc_info=True)
        self._db = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def db(self) -> Any:
        if self._db is None:
            raise RuntimeError("Library is not open; call refresh() first")
        return self._db

    # -------------------------------------------------------------------- reads
    def playlist_tree(self) -> list[PlaylistNode]:
        """The full nested playlist structure, in rekordbox's own order."""
        from pyrekordbox.db6 import tables

        db = self.db
        counts: dict[str, int] = {}
        for row in db.query(tables.DjmdSongPlaylist.PlaylistID).all():
            key = str(row[0])
            counts[key] = counts.get(key, 0) + 1

        nodes: dict[str, PlaylistNode] = {}
        parents: dict[str, str] = {}
        order: dict[str, int] = {}

        for playlist in db.get_playlist():
            pid = str(playlist.ID)
            nodes[pid] = PlaylistNode(
                id=pid,
                name=playlist.Name or "(unnamed)",
                attribute=playlist.Attribute or 0,
                track_count=counts.get(pid, 0),
            )
            parents[pid] = str(playlist.ParentID)
            order[pid] = playlist.Seq or 0

        roots: list[PlaylistNode] = []
        for pid, node in nodes.items():
            parent = parents.get(pid)
            if parent and parent in nodes:
                nodes[parent].children.append(node)
            else:
                roots.append(node)

        def sort(items: list[PlaylistNode]) -> None:
            items.sort(key=lambda n: (order.get(n.id, 0), n.name.casefold()))
            for item in items:
                sort(item.children)

        sort(roots)
        return roots

    def classify(self, info: SourceInfo, library: Any) -> tuple[LibraryStatus, ExistingTrack | None]:
        """How a dropped file relates to the collection."""
        if not self.available:
            return LibraryStatus.NEW, None
        return importer.classify(self.db, info, library)

    def counts(self) -> dict[str, int]:
        from pyrekordbox.db6 import tables

        db = self.db
        return {
            "tracks": db.get_content().count(),
            "playlists": db.get_playlist().count(),
            "my_tags": db.get_my_tag().count(),
            "colours": db.query(tables.DjmdColor).count(),
        }
