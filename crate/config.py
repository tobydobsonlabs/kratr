"""Paths and persisted settings.

Everything KRATR writes outside the music library and the rekordbox database remains
under ``%APPDATA%\\Crate`` so an application update preserves existing settings,
queues, backups, and processed-track history from before the rebrand.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import AudioFormat

logger = logging.getLogger(__name__)

APP_NAME = "KRATR"

# Keep the original data directory name for backward compatibility. This is storage
# plumbing rather than visible branding; changing it would make an update look like a
# fresh install and strand safety-critical backups in the old directory.
DATA_DIR_NAME = "Crate"
LOG_FILE_NAME = "kratr.log"

def _user_data_root() -> Path:
    """Per-user application-data directory for the running OS.

    Windows: ``%APPDATA%`` · macOS: ``~/Library/Application Support`` · other:
    ``$XDG_CONFIG_HOME`` or ``~/.config``. Keeps KRATR's own files in the place each
    platform expects, so the app is at home on a friend's Mac as well as Windows.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _rekordbox_library_dir() -> Path:
    """Where rekordbox 6/7 keeps its library, per OS."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        # rekordbox on macOS stores the library under ~/Library/Pioneer/rekordbox.
        base = Path.home() / "Library"
    return base / "Pioneer" / "rekordbox"


APP_DIR = _user_data_root() / DATA_DIR_NAME
SETTINGS_PATH = APP_DIR / "settings.json"
QUEUE_PATH = APP_DIR / "queue.json"
COMMENT_BACKUP_PATH = APP_DIR / "comment-backup.jsonl"
MOVE_LOG_PATH = APP_DIR / "move-log.jsonl"
RESOURCE_LIST_PATH = APP_DIR / "re-source.csv"
BACKUP_DIR = APP_DIR / "backups"
CACHE_DIR = APP_DIR / "cache"
LOG_DIR = APP_DIR / "logs"

#: Where rekordbox 6/7 keeps its library.
REKORDBOX_DB_DIR = _rekordbox_library_dir()
REKORDBOX_DB_PATH = REKORDBOX_DB_DIR / "master.db"
REKORDBOX_PLAYLIST_XML = REKORDBOX_DB_DIR / "masterPlaylists6.xml"

#: Files that must be backed up together — the XML carries playlist USN state.
REKORDBOX_BACKUP_FILES = ("master.db", "masterPlaylists6.xml")

#: SQLite write-ahead-log sidecars. The rekordbox database runs in **WAL mode**, so
#: committed changes can live in ``master.db-wal`` until a checkpoint folds them in.
#: They must be copied with a backup (or the backup is incomplete) and deleted on
#: restore (or a stale WAL replays changes back over the restored file).
REKORDBOX_SIDECAR_FILES = ("master.db-wal", "master.db-shm")

#: rekordbox's process name, for the "is it running?" gate.
REKORDBOX_PROCESS_NAMES = {"rekordbox.exe", "rekordbox"}


def ensure_dirs() -> None:
    for d in (APP_DIR, BACKUP_DIR, CACHE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def bundled_tool_dirs() -> list[Path]:
    """Directories that may hold the ffmpeg/ffprobe shipped *with* an installed KRATR.

    Windows: next to ``KRATR.exe`` (and an ``ffmpeg`` subfolder). macOS: the several
    places a ``.app`` may carry helper binaries — ``Contents/MacOS`` (where the main
    binary lives), ``Contents/Frameworks`` and ``Contents/Resources``. Only meaningful
    in a frozen build; a source checkout returns ``[]`` so the lookup falls straight
    through to PATH, exactly as before.
    """
    if not getattr(sys, "frozen", False):
        return []
    exe_dir = Path(sys.executable).resolve().parent
    dirs = [exe_dir, exe_dir / "ffmpeg"]
    if sys.platform == "darwin":
        contents = exe_dir.parent  # exe_dir is KRATR.app/Contents/MacOS
        dirs += [contents / "Frameworks", contents / "Resources"]
    return [d for d in dirs if d.is_dir()]


def find_executable(name: str, configured: str | None = None) -> str | None:
    """Locate ffmpeg/ffprobe.

    Resolution order: an explicit configured path → the copy bundled inside an
    installed KRATR → whatever is on PATH. The bundled copy is preferred over PATH so a
    friend with no ffmpeg installed still works out of the box, and a known-good version
    is used even when an older ffmpeg happens to be on PATH.

    In a source checkout there is no bundled copy, so this is just ``shutil.which`` —
    unchanged from before.
    """
    if configured and Path(configured).is_file():
        return configured

    for tool_dir in bundled_tool_dirs():
        for suffix in (".exe", ""):
            candidate = tool_dir / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)

    return shutil.which(name)


@dataclass
class Settings:
    """User-configurable behaviour, persisted as JSON."""

    #: Root of the genre/sub-genre folder tree.
    music_root: str = "D:/Music"

    #: Folder for tracks flagged as upscales and pulled out of the library.
    #: Leading underscore keeps it sorted away from the genre folders.
    quarantine_folder: str = "_Re-source"

    # --- conversion -------------------------------------------------------
    #: Default target when no per-source rule matches. ``None`` means keep original.
    default_target: str | None = AudioFormat.WAV.value
    default_bit_depth: int | None = 16
    default_sample_rate: int | None = 44100

    #: Per-source-format rules, e.g. {"flac": "wav", "aiff": None}.
    #: ``None`` as a value means "keep original" for that source format.
    conversion_rules: dict[str, str | None] = field(
        default_factory=lambda: {
            AudioFormat.FLAC.value: AudioFormat.WAV.value,
            AudioFormat.ALAC.value: AudioFormat.WAV.value,
            AudioFormat.AIFF.value: AudioFormat.WAV.value,
            AudioFormat.MP3.value: None,
            AudioFormat.WAV.value: None,
        }
    )

    # --- quality analysis -------------------------------------------------
    #: Energy floor below the spectral peak, in dB, used to find the cutoff.
    cutoff_threshold_db: float = 50.0
    #: Seconds of the loudest section to analyse.
    analysis_window_s: float = 30.0

    # --- tagging ----------------------------------------------------------
    #: Prefix for tags mirrored into the rekordbox comment field. Makes CDJ search
    #: precise: "hazy" would also match track titles, "/hazy" won't.
    comment_tag_prefix: str = "/"
    #: Mirror applied tags into the comment field.
    mirror_tags_to_comments: bool = True

    # --- optional import steps -------------------------------------------
    #: Show the Tags step in the import wizard. When off, tracks are filed and
    #: imported without tagging and the tag page is skipped entirely. Tags are
    #: personal — KRATR only ever *suggests* a starting set, so this is opt-in.
    use_tags: bool = True
    #: Show the Colour step in the import wizard. Many DJs don't colour-code at all,
    #: so this can be turned off to drop the colour page.
    use_colours: bool = True

    # --- updates ----------------------------------------------------------
    #: Check GitHub for a newer KRATR release on launch and offer a one-click download.
    #: The only network call KRATR makes on its own; turn off to never phone home.
    check_for_updates: bool = True

    # --- tooling ----------------------------------------------------------
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None

    #: Number of timestamped DB backups to retain.
    backup_retention: int = 20

    # --- state ------------------------------------------------------------
    #: Last known-good rekordbox version + schema fingerprint, for Safe Mode.
    known_rekordbox_version: str | None = None
    known_schema_fingerprint: str | None = None

    @property
    def music_root_path(self) -> Path:
        return Path(self.music_root)

    def target_for_source(self, source: AudioFormat | None) -> str | None:
        """Resolve the default conversion target for a given source format."""
        if source is not None and source.value in self.conversion_rules:
            return self.conversion_rules[source.value]
        return self.default_target

    # --- persistence ------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        if not SETTINGS_PATH.is_file():
            return cls()
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read settings; falling back to defaults")
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        ensure_dirs()
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)


def setup_logging(verbose: bool = False) -> None:
    ensure_dirs()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_DIR / LOG_FILE_NAME, encoding="utf-8"),
        ],
    )
