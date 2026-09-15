"""Finding tracks that haven't been through KRATR yet.

Two signals, used together:

1. **A processed log.** Every track that completes an import is recorded by content ID
   and path. Authoritative, but only covers work done since the log existed.

2. **Tags and colour.** Before this project the library had *zero* MyTag links and
   *zero* colour assignments across all 3,328 tracks — both channels were completely
   unused. So a track carrying either has, by definition, been sorted. That makes the
   whole existing library correctly classifiable as "not yet done" without any
   back-filling, and it stays true for anything tagged directly in rekordbox.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config

logger = logging.getLogger(__name__)

PROCESSED_PATH = config.APP_DIR / "processed.json"


class ProcessedLog:
    """Tracks KRATR has already taken through an import."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or PROCESSED_PATH
        self.content_ids: set[str] = set()
        self.paths: set[str] = set()
        self.load()

    def load(self) -> "ProcessedLog":
        if not self.path.is_file():
            return self
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.content_ids = set(data.get("content_ids", []))
            self.paths = {p.casefold() for p in data.get("paths", [])}
        except (OSError, json.JSONDecodeError):
            logger.warning("Processed log unreadable; treating everything as unsorted")
        return self

    def save(self) -> None:
        config.ensure_dirs()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"content_ids": sorted(self.content_ids), "paths": sorted(self.paths)},
                indent=1,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def record(self, content_id: str | None, path: Path | str | None) -> None:
        if content_id:
            self.content_ids.add(str(content_id))
        if path:
            self.paths.add(str(path).replace("\\", "/").casefold())

    def contains(self, content_id: str | None, path: str | None = None) -> bool:
        if content_id and str(content_id) in self.content_ids:
            return True
        return bool(path) and path.replace("\\", "/").casefold() in self.paths

    def __len__(self) -> int:
        return len(self.content_ids)


@dataclass(slots=True)
class UnsortedTrack:
    content_id: str
    path: Path
    title: str
    artist: str
    folder: str


def _sorted_content_ids(db: Any) -> set[str]:
    """Content IDs that already carry a tag or a colour."""
    from pyrekordbox.db6 import tables

    tagged = {
        str(row[0])
        for row in db.query(tables.DjmdSongMyTag.ContentID).distinct().all()
    }
    # rekordbox spells "no colour" as NULL, "" or "0" — see colours.normalise_colour_id.
    from . import colours

    coloured = {
        str(row[0])
        for row in db.query(tables.DjmdContent.ID, tables.DjmdContent.ColorID).all()
        if colours.normalise_colour_id(row[1]) is not None
    }
    return tagged | coloured


def unsorted_tracks(
    db: Any, music_root: Path | str, processed: ProcessedLog | None = None
) -> list[UnsortedTrack]:
    """Every track under the music root that hasn't been sorted yet."""
    from pyrekordbox.db6 import tables

    processed = processed or ProcessedLog()
    already = _sorted_content_ids(db)
    root = str(music_root).replace("\\", "/").rstrip("/") + "/"

    artists = {
        str(a.ID): a.Name for a in db.query(tables.DjmdArtist).all() if a.Name
    }

    results: list[UnsortedTrack] = []
    for content in db.query(tables.DjmdContent).all():
        folder_path = content.FolderPath or ""
        if not folder_path.casefold().startswith(root.casefold()):
            continue
        content_id = str(content.ID)
        if content_id in already or processed.contains(content_id, folder_path):
            continue

        relative = folder_path[len(root):]
        results.append(
            UnsortedTrack(
                content_id=content_id,
                path=Path(folder_path),
                title=content.Title or Path(folder_path).stem,
                artist=artists.get(str(content.ArtistID), ""),
                folder=str(Path(relative).parent) if "/" in relative else "",
            )
        )
    return results


def progress(db: Any, music_root: Path | str, processed: ProcessedLog | None = None) -> tuple[int, int]:
    """(sorted, total) for tracks under the music root."""
    from pyrekordbox.db6 import tables

    root = str(music_root).replace("\\", "/").rstrip("/") + "/"
    total = sum(
        1
        for row in db.query(tables.DjmdContent.FolderPath).all()
        if (row[0] or "").casefold().startswith(root.casefold())
    )
    return total - len(unsorted_tracks(db, music_root, processed)), total
