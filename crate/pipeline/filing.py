"""Filing tracks into the genre/sub-genre folder tree.

Moves rather than copies, so the intake cleans up behind itself — which is the whole
point, but also means every move is logged so any intake can be reversed by hand.

The library's shape (verified): ``D:\\Music\\<Genre>\\<Sub-genre>\\track.wav``, with 22
top-level genre folders and sub-genres one level below.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

#: Characters Windows forbids in file and folder names.
_ILLEGAL = '<>:"/\\|?*'


class CollisionPolicy:
    """What to do when a file of the same name is already at the destination."""

    FAIL = "fail"
    RENAME = "rename"
    OVERWRITE = "overwrite"


@dataclass(frozen=True, slots=True)
class MoveRecord:
    source: str
    destination: str
    timestamp: float

    def as_json(self) -> str:
        return json.dumps(
            {"source": self.source, "destination": self.destination, "ts": self.timestamp}
        )


def sanitise_folder_name(name: str) -> str:
    """Make a user-typed genre name safe as a folder name.

    Kept deliberately permissive: the existing library has folders like
    ``Soul + Funk``, ``R&B``, ``Nu Disco - Italo - 80s Synths`` and
    ``90s - New Jack Swing``, all of which are legal and must survive untouched.
    """
    cleaned = "".join("-" if ch in _ILLEGAL else ch for ch in name)
    cleaned = cleaned.strip().rstrip(".")  # trailing dots are invalid on Windows
    # Require some actual content: "///" would otherwise sanitise to a legal but
    # meaningless "---" folder. Every real genre name has alphanumerics in it.
    if not any(ch.isalnum() for ch in cleaned):
        raise ValueError(f"Folder name {name!r} has no usable characters")
    return cleaned


class MusicLibrary:
    """The genre/sub-genre folder tree on disk."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def exists(self) -> bool:
        return self.root.is_dir()

    def genres(self) -> list[str]:
        if not self.exists():
            return []
        return sorted(
            (p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=str.casefold,
        )

    def subgenres(self, genre: str) -> list[str]:
        folder = self.root / genre
        if not folder.is_dir():
            return []
        return sorted(
            (p.name for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=str.casefold,
        )

    def all_folders(self) -> list[tuple[str, str | None]]:
        """Every (genre, sub-genre) pair, plus (genre, None) for each top level.

        Feeds the searchable picker — House alone has ~29 sub-folders, so typing to
        filter matters more than browsing.
        """
        out: list[tuple[str, str | None]] = []
        for genre in self.genres():
            out.append((genre, None))
            out.extend((genre, sub) for sub in self.subgenres(genre))
        return out

    def folder_for(self, genre: str, subgenre: str | None = None) -> Path:
        path = self.root / sanitise_folder_name(genre)
        if subgenre:
            path = path / sanitise_folder_name(subgenre)
        return path

    def contains(self, path: Path) -> bool:
        """True when a path sits anywhere under the library root."""
        try:
            Path(path).resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return False
        return True

    def classify(self, path: Path) -> tuple[str, str | None] | None:
        """The (genre, sub-genre) a path currently sits in, or None if outside the tree.

        Used to tell a correctly-filed track from a misfiled one when something already
        in the collection gets dropped in.
        """
        try:
            relative = Path(path).resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return None
        parts = relative.parts
        if len(parts) < 2:  # a bare file sitting in the root isn't filed
            return None
        genre = parts[0]
        subgenre = parts[1] if len(parts) >= 3 else None
        return genre, subgenre


def resolve_collision(destination: Path, policy: str) -> Path:
    """Apply the collision policy, returning the path actually to write."""
    if not destination.exists():
        return destination

    if policy == CollisionPolicy.OVERWRITE:
        return destination
    if policy == CollisionPolicy.FAIL:
        raise FileExistsError(
            f"{destination.name} already exists in {destination.parent}"
        )

    stem, suffix = destination.stem, destination.suffix
    for n in range(2, 1000):
        candidate = destination.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find a free name for {destination.name}")


def log_move(source: Path, destination: Path) -> None:
    """Append to the move log so an intake can be undone manually."""
    config.ensure_dirs()
    record = MoveRecord(str(source), str(destination), time.time())
    with config.MOVE_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(record.as_json() + "\n")


def file_track(
    source: Path,
    library: MusicLibrary,
    genre: str,
    subgenre: str | None = None,
    *,
    collision: str = CollisionPolicy.RENAME,
    create_folders: bool = True,
) -> Path:
    """Move a file into its genre folder, creating the folder if needed.

    Returns the final path. Uses :func:`shutil.move`, which handles the
    cross-volume case (files usually arrive on C: and land on D:).
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(source)

    folder = library.folder_for(genre, subgenre)
    if not folder.is_dir():
        if not create_folders:
            raise FileNotFoundError(f"Folder does not exist: {folder}")
        folder.mkdir(parents=True, exist_ok=True)
        logger.info("Created folder %s", folder)

    destination = resolve_collision(folder / source.name, collision)

    if destination.exists() and destination.samefile(source):
        return destination

    shutil.move(str(source), str(destination))
    log_move(source, destination)
    logger.info("Filed %s → %s", source.name, destination)
    return destination


def quarantine_track(
    source: Path,
    library: MusicLibrary,
    folder_name: str = "_Re-source",
    *,
    collision: str = CollisionPolicy.RENAME,
) -> Path:
    """Move a flagged track out of the library, into the re-source folder.

    Deliberately a sibling of the genre folders rather than inside one: a quarantined
    track must not be reachable by browsing the library, or it will eventually get
    played by accident.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(source)

    folder = library.root / sanitise_folder_name(folder_name)
    folder.mkdir(parents=True, exist_ok=True)

    destination = resolve_collision(folder / source.name, collision)
    shutil.move(str(source), str(destination))
    log_move(source, destination)
    logger.info("Quarantined %s → %s", source.name, destination)
    return destination
