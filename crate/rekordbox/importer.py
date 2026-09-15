"""Adding tracks to the rekordbox collection, and pointing existing ones somewhere new.

**The path trap.** rekordbox stores ``FolderPath`` with forward slashes
(``D:/Music/House/...`` — verified across the live library), but ``str(Path(...))`` on
Windows produces backslashes, and ``pyrekordbox.add_content`` stores whatever string it
is handed. Left alone it writes ``C:\\Users\\...`` and rekordbox never resolves the
file. Every path that reaches the database goes through :func:`normalise_path` first.

Analysis is deliberately left unset: ``AnalysisDataPath`` stays ``None`` so rekordbox
analyses the track itself on next launch, producing a real waveform, BPM, key and
beatgrid. Fabricating that data would be worse than not having it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..models import ExistingTrack, LibraryStatus, SourceInfo
from . import colours

logger = logging.getLogger(__name__)


def normalise_path(path: Path | str) -> str:
    """Absolute path in the form rekordbox stores: forward slashes, drive letter."""
    resolved = Path(path).resolve()
    return str(resolved).replace("\\", "/")


def find_by_path(db: Any, path: Path | str) -> Any | None:
    """Locate a track by file path.

    Case-insensitive because Windows paths are, and rows written by different tools
    disagree about capitalisation.
    """
    from sqlalchemy import func

    from pyrekordbox.db6 import tables

    wanted = normalise_path(path)
    return (
        db.query(tables.DjmdContent)
        .filter(func.lower(tables.DjmdContent.FolderPath) == wanted.lower())
        .first()
    )


def find_possible_duplicates(db: Any, info: SourceInfo) -> list[Any]:
    """Tracks that look like the same recording sitting somewhere else.

    Matched on filename plus duration, so dropping a second copy of something already
    in the collection is surfaced rather than silently imported twice.
    """
    from pyrekordbox.db6 import tables

    candidates = (
        db.query(tables.DjmdContent)
        .filter(tables.DjmdContent.FileNameL == info.path.name)
        .all()
    )
    if info.duration is None:
        return candidates
    return [
        c for c in candidates
        if c.Length is None or abs((c.Length or 0) - info.duration) <= 2
    ]


def snapshot_existing(db: Any, content: Any) -> ExistingTrack:
    """Everything attached to a collection entry.

    Shown to the user before they touch a track they already own, and compared
    afterwards to prove a relocation disturbed nothing but the path.
    """
    from pyrekordbox.db6 import tables

    playlist_rows = (
        db.query(tables.DjmdPlaylist)
        .join(
            tables.DjmdSongPlaylist,
            tables.DjmdSongPlaylist.PlaylistID == tables.DjmdPlaylist.ID,
        )
        .filter(tables.DjmdSongPlaylist.ContentID == str(content.ID))
        .all()
    )
    playlists = [p.Name for p in playlist_rows]
    playlist_ids = [str(p.ID) for p in playlist_rows]
    my_tag_rows = (
        db.query(tables.DjmdMyTag)
        .join(tables.DjmdSongMyTag, tables.DjmdSongMyTag.MyTagID == tables.DjmdMyTag.ID)
        .filter(tables.DjmdSongMyTag.ContentID == str(content.ID))
        .all()
    )
    my_tags = [t.Name for t in my_tag_rows]
    my_tag_ids = [str(t.ID) for t in my_tag_rows]

    artist_name = None
    if content.ArtistID:
        artist = db.query(tables.DjmdArtist).filter_by(ID=str(content.ArtistID)).first()
        artist_name = artist.Name if artist else None

    return ExistingTrack(
        content_id=str(content.ID),
        folder_path=content.FolderPath or "",
        file_name=content.FileNameL or "",
        title=content.Title,
        artist=artist_name,
        rating=content.Rating,
        play_count=content.DJPlayCount,
        analysis_path=content.AnalysisDataPath,
        playlist_names=playlists,
        playlist_ids=playlist_ids,
        my_tag_names=my_tags,
        my_tag_ids=my_tag_ids,
        colour_id=colours.normalise_colour_id(content.ColorID),
        comment=content.Commnt,
    )


def classify(db: Any, info: SourceInfo, library: Any) -> tuple[LibraryStatus, ExistingTrack | None]:
    """Work out how a dropped file relates to the collection.

    Dropping something rekordbox already knows about must never be treated as a new
    import — 3,306 of the 3,328 tracks already live under the music root, carrying
    ratings, play counts, cues and playlist membership that a re-import would lose.
    """
    existing = find_by_path(db, info.path)

    if existing is not None:
        snapshot = snapshot_existing(db, existing)
        if not Path(existing.FolderPath).is_file():
            return LibraryStatus.MISSING_FILE, snapshot
        if library.classify(info.path) is not None:
            return LibraryStatus.FILED_CORRECTLY, snapshot
        return LibraryStatus.MISFILED, snapshot

    duplicates = find_possible_duplicates(db, info)
    if duplicates:
        return LibraryStatus.DUPLICATE, snapshot_existing(db, duplicates[0])

    return LibraryStatus.NEW, None


# ------------------------------------------------------------------- relations
def _get_or_create(db: Any, model: Any, adder: Any, name: str) -> Any | None:
    """Reuse an existing Artist/Album/Genre row rather than creating a duplicate.

    The collection already holds 2,735 albums and 2,222 genre assignments; duplicate
    rows differing only by case are tedious to clean up afterwards.
    """
    from sqlalchemy import func

    cleaned = (name or "").strip()
    if not cleaned:
        return None

    existing = (
        db.query(model).filter(func.lower(model.Name) == cleaned.lower()).first()
    )
    if existing is not None:
        return existing
    return adder(cleaned)


def set_relations(db: Any, content: Any, info: SourceInfo) -> None:
    """Attach artist, album and genre, creating rows only where needed.

    IDs are coerced to ``str``: rekordbox stores these columns as TEXT, and
    ``add_artist`` and friends hand back a freshly generated *integer* ID. Storing the
    int would work today thanks to SQLite's dynamic typing, then quietly fail later
    when something compares it against the string form.
    """
    from pyrekordbox.db6 import tables

    artist = _get_or_create(db, tables.DjmdArtist, db.add_artist, info.tags.get("artist", ""))
    if artist is not None:
        content.ArtistID = str(artist.ID)

    album = _get_or_create(db, tables.DjmdAlbum, db.add_album, info.tags.get("album", ""))
    if album is not None:
        content.AlbumID = str(album.ID)

    genre = _get_or_create(db, tables.DjmdGenre, db.add_genre, info.tags.get("genre", ""))
    if genre is not None:
        content.GenreID = str(genre.ID)


# ---------------------------------------------------------------------- import
def import_track(db: Any, path: Path | str, info: SourceInfo | None = None) -> Any:
    """Add a new file to the collection.

    Builds the ``DjmdContent`` row directly rather than calling
    ``pyrekordbox.add_content``, for two reasons that both bite in this app:

    1. **Path format.** ``add_content`` stores ``str(Path(...))``, which is backslashed
       on Windows, while rekordbox stores forward slashes. It would never find the file.
    2. **ID type.** ``add_content`` assigns an *integer* primary key, but every existing
       row's ``ID`` is a string. Mixing the two makes SQLAlchemy fail to sort pending
       objects — ``'<' not supported between instances of 'int' and 'str'`` — which
       breaks any flush containing more than one new track. Since batching imports is
       the entire point of the op queue, that is not an edge case.

    Raises:
        ValueError: a track with that path is already in the collection.
    """
    import datetime
    from uuid import uuid4

    from pyrekordbox.db6 import tables
    from pyrekordbox.db6.tables import FileType

    normalised = normalise_path(path)
    file_path = Path(normalised)

    if find_by_path(db, normalised) is not None:
        raise ValueError(f"Already in the collection: {normalised}")

    suffix = file_path.suffix.lstrip(".").upper()
    try:
        file_type = getattr(FileType, suffix)
    except AttributeError as exc:
        raise ValueError(f"Unsupported file type: {file_path.suffix}") from exc

    # Strings throughout — rekordbox stores these columns as TEXT.
    content_id = str(db.generate_unused_id(tables.DjmdContent))
    file_id = db.generate_unused_id(tables.DjmdContent, id_field_name="rb_file_id")
    device = db.get_device().first()
    menu_item = db.get_menu_items(Name="TRACK").one()
    today = datetime.date.today()

    fields: dict[str, Any] = {
        "ID": content_id,
        "UUID": str(uuid4()),
        "ContentLink": menu_item.rb_local_usn,
        "DateCreated": today,
        "StockDate": today,
        "DeviceID": device.ID,
        "MasterDBID": device.MasterDBID,
        "MasterSongID": content_id,
        "FileNameL": file_path.name,
        "FileSize": file_path.stat().st_size,
        "FileType": file_type.value,
        "FolderPath": normalised,
        "HotCueAutoLoad": "on",
        "rb_file_id": file_id,
    }

    if info is not None:
        fields["Title"] = info.tags.get("title") or file_path.stem
        if info.duration:
            fields["Length"] = int(round(info.duration))
        if info.bitrate_kbps:
            fields["BitRate"] = info.bitrate_kbps
        if info.sample_rate:
            fields["SampleRate"] = info.sample_rate
    else:
        fields["Title"] = file_path.stem

    content = tables.DjmdContent.create(**fields)
    db.add(content)
    db.flush()

    if info is not None:
        set_relations(db, content, info)
        db.flush()

    logger.info("Imported %s as content %s", file_path.name, content.ID)
    return content


# -------------------------------------------------------------------- playlists
def add_to_playlists(db: Any, content: Any, playlist_ids: list[str]) -> list[Any]:
    """Add a track to several playlists, skipping any it is already in."""
    from pyrekordbox.db6 import tables

    added = []
    for playlist_id in playlist_ids:
        playlist = db.get_playlist(ID=str(playlist_id))
        if playlist is None:
            raise ValueError(f"No such playlist: {playlist_id}")
        if playlist.Attribute != 0:
            # Folders (1) and smart playlists (4) cannot hold songs directly.
            raise ValueError(f"{playlist.Name!r} is not a normal playlist")

        already = (
            db.query(tables.DjmdSongPlaylist)
            .filter_by(PlaylistID=str(playlist_id), ContentID=str(content.ID))
            .first()
        )
        if already is not None:
            logger.debug("Already in playlist %s", playlist.Name)
            continue

        added.append(db.add_to_playlist(playlist, content))
    return added


# ------------------------------------------------------------------- relocation
#: Fields a relocation is allowed to touch. Everything else on the row — Rating,
#: DJPlayCount, AnalysisDataPath, ColorID, Commnt — is left strictly alone, which is
#: what preserves playlists, ratings, play counts and cues across a move.
RELOCATION_FIELDS = ("FolderPath", "FileNameL", "FileType", "FileSize", "BitRate", "SampleRate")


def relocate(db: Any, content: Any, new_path: Path | str, info: SourceInfo | None = None) -> Any:
    """Point an existing collection entry at a new file location.

    An **in-place row update**, never a delete-and-re-add. The ``ContentID`` is
    unchanged, so every ``djmdSongPlaylist`` and ``djmdSongMyTag`` row still points at
    this track, and rating, play count and analysis stay exactly as they were.
    """
    from pyrekordbox.db6.tables import FileType

    normalised = normalise_path(new_path)
    previous = content.FolderPath

    clash = find_by_path(db, normalised)
    if clash is not None and str(clash.ID) != str(content.ID):
        raise ValueError(
            f"Another collection entry already points at {normalised} (ID {clash.ID})"
        )

    content.FolderPath = normalised
    content.FileNameL = Path(normalised).name

    # Only when the file itself was replaced (i.e. converted) do the format fields move.
    if info is not None:
        suffix = Path(normalised).suffix.lstrip(".").upper()
        try:
            content.FileType = getattr(FileType, suffix).value
        except AttributeError:
            logger.warning("Unknown file type %r; leaving FileType unchanged", suffix)
        content.FileSize = info.file_size
        if info.bitrate_kbps:
            content.BitRate = info.bitrate_kbps
        if info.sample_rate:
            content.SampleRate = info.sample_rate

    db.flush()
    logger.info("Relocated content %s: %s → %s", content.ID, previous, normalised)
    return content
