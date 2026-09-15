"""A disposable copy of the rekordbox library, for developing and testing write paths.

Nothing in KRATR develops against the real ``master.db``. Every write path is proven
here first, against a clone that can be restored byte-for-byte.

Typical use::

    with Sandbox.create() as sb:
        db = sb.open()
        before = sb.snapshot_track(content_id)
        ...                       # mutate
        assert sb.content_count() == before_count
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .. import config

logger = logging.getLogger(__name__)


class SandboxError(RuntimeError):
    pass


class Sandbox:
    """A cloned rekordbox database in a scratch directory."""

    def __init__(self, directory: Path, source_dir: Path) -> None:
        self.dir = directory
        self.source_dir = source_dir
        self._db: Any | None = None
        self._pristine = directory / "_pristine"

    # ------------------------------------------------------------------ setup
    @classmethod
    def create(
        cls,
        source_dir: Path | None = None,
        directory: Path | None = None,
    ) -> "Sandbox":
        """Clone the rekordbox library into a scratch directory."""
        source_dir = source_dir or config.REKORDBOX_DB_DIR
        if not (source_dir / "master.db").is_file():
            raise SandboxError(f"No master.db found in {source_dir}")

        directory = directory or Path(tempfile.mkdtemp(prefix="crate-sandbox-"))
        directory.mkdir(parents=True, exist_ok=True)

        sb = cls(directory, source_dir)
        sb._copy_library(directory)
        # Keep an untouched reference copy so restore() is always available.
        sb._pristine.mkdir(parents=True, exist_ok=True)
        sb._copy_library(sb._pristine)

        logger.info("Sandbox created at %s (from %s)", directory, source_dir)
        return sb

    def _copy_library(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for name in config.REKORDBOX_BACKUP_FILES:
            src = self.source_dir / name
            if src.is_file():
                shutil.copy2(src, dest / name)
            elif name == "master.db":
                raise SandboxError(f"Required file missing: {src}")
            else:
                # pyrekordbox warns when masterPlaylists6.xml is absent and playlist
                # USN tracking depends on it, so its absence is worth surfacing.
                logger.warning("%s not found alongside master.db", name)

    @property
    def db_path(self) -> Path:
        return self.dir / "master.db"

    # ------------------------------------------------------------------- open
    def open(self, **kwargs: Any) -> Any:
        """Open the sandbox database. Import is local so the module stays importable
        without pyrekordbox present."""
        from pyrekordbox import Rekordbox6Database

        if self._db is None:
            self._db = Rekordbox6Database(path=str(self.db_path), unlock=True, **kwargs)
            self._allow_commits_while_rekordbox_runs()
        return self._db

    def _allow_commits_while_rekordbox_runs(self) -> None:
        """Let the sandbox commit even with rekordbox open.

        ``pyrekordbox.commit()`` refuses whenever a rekordbox process exists — it
        checks the *process*, not which file is being written. That guard exists to
        protect the live library, and a sandbox is a disposable clone in a temp
        directory, so the check is simply wrong here: it would block every write test
        purely because rekordbox happened to be open.

        The real gate for the live database lives in ``session.open_for_write`` and is
        untouched by this.
        """
        db = self._db
        original = type(db).commit

        def commit(self_db, autoinc: bool = True) -> None:  # noqa: ANN001
            from pyrekordbox.db6 import database as _db_mod

            saved = _db_mod.get_rekordbox_pid
            _db_mod.get_rekordbox_pid = lambda *a, **k: 0
            try:
                original(self_db, autoinc)
            finally:
                _db_mod.get_rekordbox_pid = saved

        # Bound to this instance only — never patched globally.
        db.commit = commit.__get__(db, type(db))

    def checkpoint(self) -> None:
        """Fold the write-ahead log back into ``master.db``.

        The rekordbox database is in WAL mode, so a commit alone leaves the change in
        ``master.db-wal`` and the main file untouched. Anything that inspects the file
        on disk has to checkpoint first or it reads stale bytes.
        """
        if self._db is None:
            return
        from sqlalchemy import text

        try:
            with self._db.engine.connect() as conn:
                conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        except Exception:  # noqa: BLE001 - best effort; close() disposes anyway
            logger.debug("wal_checkpoint failed", exc_info=True)

    def close(self) -> None:
        if self._db is None:
            return
        self.checkpoint()
        try:
            self._db.close()
        except Exception:  # noqa: BLE001 - closing must never mask a test failure
            logger.debug("Ignoring error while closing sandbox DB", exc_info=True)
        try:
            # Releases the pooled connection so the -wal/-shm files are finalised.
            self._db.engine.dispose()
        except Exception:  # noqa: BLE001
            logger.debug("engine.dispose failed", exc_info=True)
        self._db = None

    # --------------------------------------------------------------- restore
    def restore(self) -> None:
        """Return the sandbox to its pristine clone."""
        self.close()
        # Delete the WAL sidecars first: a leftover -wal would replay committed
        # changes straight back over the file we just restored.
        for name in config.REKORDBOX_SIDECAR_FILES:
            (self.dir / name).unlink(missing_ok=True)
        for name in config.REKORDBOX_BACKUP_FILES:
            src = self._pristine / name
            if src.is_file():
                shutil.copy2(src, self.dir / name)
        logger.info("Sandbox restored to pristine state")

    def is_pristine(self) -> bool:
        """True when the working DB matches the pristine clone.

        Closes first so the WAL is checkpointed — comparing while a connection is open
        would compare stale bytes and report a mutated database as untouched.
        """
        self.close()
        return _sha256(self.db_path) == _sha256(self._pristine / "master.db")

    def destroy(self) -> None:
        self.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc: object) -> None:
        self.destroy()

    # ----------------------------------------------------- assertion helpers
    def content_count(self) -> int:
        """Total tracks in the collection.

        A relocation must leave this unchanged; an import must increase it by exactly one.
        """
        return self.open().get_content().count()

    def playlist_sizes(self) -> dict[str, int]:
        """Track count per playlist, keyed by playlist ID.

        Compared before and after any operation to prove existing playlists were untouched.
        """
        from pyrekordbox.db6 import tables

        db = self.open()
        sizes: dict[str, int] = {}
        for pl in db.get_playlist():
            if pl.Attribute != 0:  # folders and smart playlists hold no songs directly
                continue
            sizes[str(pl.ID)] = (
                db.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=str(pl.ID)).count()
            )
        return sizes

    def snapshot_track(self, content_id: str) -> dict[str, Any]:
        """Everything attached to a track, for before/after comparison.

        This is what proves a relocation preserved playlists, rating, play count and
        analysis rather than merely appearing to.
        """
        from pyrekordbox.db6 import tables

        db = self.open()
        c = db.get_content(ID=str(content_id))
        fields = (
            "ID", "FolderPath", "FileNameL", "FileType", "FileSize", "Title",
            "Rating", "DJPlayCount", "AnalysisDataPath", "AnalysisUpdated",
            "ColorID", "Commnt", "ArtistID", "AlbumID", "GenreID",
        )
        snap: dict[str, Any] = {f: getattr(c, f, None) for f in fields}
        snap["playlists"] = sorted(
            str(r.PlaylistID)
            for r in db.query(tables.DjmdSongPlaylist).filter_by(ContentID=str(content_id))
        )
        snap["my_tags"] = sorted(
            str(r.MyTagID)
            for r in db.query(tables.DjmdSongMyTag).filter_by(ContentID=str(content_id))
        )
        return snap

    def find_track(self, path_fragment: str) -> Any | None:
        """First track whose FolderPath contains the given fragment."""
        from pyrekordbox.db6 import tables

        db = self.open()
        return (
            db.query(tables.DjmdContent)
            .filter(tables.DjmdContent.FolderPath.like(f"%{path_fragment}%"))
            .first()
        )

    def iter_tracks(self) -> Iterator[Any]:
        yield from self.open().get_content()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
