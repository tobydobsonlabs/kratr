"""Core data types shared across the pipeline, rekordbox layer and UI."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path


class AudioFormat(enum.Enum):
    """A target (or source) audio format."""

    WAV = "wav"
    AIFF = "aiff"
    FLAC = "flac"
    ALAC = "m4a"
    MP3 = "mp3"

    @property
    def is_lossless(self) -> bool:
        return self in {AudioFormat.WAV, AudioFormat.AIFF, AudioFormat.FLAC, AudioFormat.ALAC}

    @property
    def label(self) -> str:
        return {
            AudioFormat.WAV: "WAV",
            AudioFormat.AIFF: "AIFF",
            AudioFormat.FLAC: "FLAC",
            AudioFormat.ALAC: "ALAC",
            AudioFormat.MP3: "MP3",
        }[self]


#: Codecs ffprobe reports, mapped onto our format enum. Trust the codec, never the extension.
CODEC_TO_FORMAT: dict[str, AudioFormat] = {
    "pcm_s16le": AudioFormat.WAV,
    "pcm_s24le": AudioFormat.WAV,
    "pcm_s32le": AudioFormat.WAV,
    "pcm_f32le": AudioFormat.WAV,
    "pcm_s16be": AudioFormat.AIFF,
    "pcm_s24be": AudioFormat.AIFF,
    "pcm_s32be": AudioFormat.AIFF,
    "flac": AudioFormat.FLAC,
    "alac": AudioFormat.ALAC,
    "mp3": AudioFormat.MP3,
    "mp3float": AudioFormat.MP3,
    "aac": AudioFormat.ALAC,  # lossy in an m4a container; treated as lossy via is_lossy_codec
}

#: Codecs that are lossy regardless of container.
LOSSY_CODECS = {"mp3", "mp3float", "aac", "vorbis", "opus", "wmav2", "ac3"}


class KeepOriginal:
    """Sentinel for 'do not convert this track'."""

    label = "Keep original"


#: A conversion target is either a real format or the keep-original sentinel.
ConversionTarget = AudioFormat | type[KeepOriginal]


class QualityRating(enum.Enum):
    """Verdict from spectral analysis, independent of what the file claims to be."""

    LOSSLESS = "lossless"
    HIGH = "high"           # ~320 kbps
    MEDIUM = "medium"       # ~192 kbps
    LOW = "low"             # ~128 kbps
    POOR = "poor"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            QualityRating.LOSSLESS: "Lossless",
            QualityRating.HIGH: "High (~320k)",
            QualityRating.MEDIUM: "Medium (~192k)",
            QualityRating.LOW: "Low (~128k)",
            QualityRating.POOR: "Poor",
            QualityRating.UNKNOWN: "Unknown",
        }[self]


@dataclass(slots=True)
class SourceInfo:
    """What ffprobe says about a file. Purely factual — no judgement."""

    path: Path
    codec: str
    container: str
    fmt: AudioFormat | None
    bitrate: int | None            # bits per second
    sample_rate: int | None
    bit_depth: int | None
    channels: int | None
    duration: float | None         # seconds
    file_size: int
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_lossy(self) -> bool:
        return self.codec in LOSSY_CODECS

    @property
    def bitrate_kbps(self) -> int | None:
        return round(self.bitrate / 1000) if self.bitrate else None


@dataclass(slots=True)
class QualityReport:
    """Result of spectral analysis: the cutoff, a verdict, and any mismatch with the container."""

    cutoff_hz: float | None
    rating: QualityRating
    #: True when the file claims to be better than its spectrum supports (upscale/transcode).
    is_suspect: bool = False
    notes: list[str] = field(default_factory=list)
    spectrogram_path: Path | None = None

    @property
    def cutoff_khz(self) -> float | None:
        return round(self.cutoff_hz / 1000, 1) if self.cutoff_hz else None


class TrackState(enum.Enum):
    """Where a track has got to in the pipeline."""

    PENDING = "pending"          # dropped, not yet probed
    ANALYSING = "analysing"
    READY = "ready"              # probed + analysed, awaiting decisions
    CONVERTING = "converting"
    FILED = "filed"              # converted and moved into the music library
    QUARANTINED = "quarantined"  # flagged as an upscale and pulled out of the library
    QUEUED = "queued"            # rekordbox changes queued, awaiting batch apply
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"


class LibraryStatus(enum.Enum):
    """How a dropped file relates to the existing rekordbox collection."""

    NEW = "new"                      # not in the collection
    FILED_CORRECTLY = "filed"        # in the collection, already under the music root
    MISFILED = "misfiled"            # in the collection, but not in a genre folder
    MISSING_FILE = "missing"         # in the collection, but the file isn't where rekordbox thinks
    DUPLICATE = "duplicate"          # a copy of something already in the collection


@dataclass(slots=True)
class ExistingTrack:
    """Snapshot of a track that rekordbox already knows about.

    Used to show the user what they'd be touching, and to assert afterwards that a
    relocation left everything except the path untouched.
    """

    content_id: str
    folder_path: str
    file_name: str
    title: str | None
    artist: str | None
    rating: int | None
    play_count: int | None
    analysis_path: str | None
    playlist_names: list[str] = field(default_factory=list)
    #: IDs as well as names — without these the playlists a track is already in
    #: could be described in a banner but never actually shown as selected.
    playlist_ids: list[str] = field(default_factory=list)
    my_tag_names: list[str] = field(default_factory=list)
    #: IDs as well as names, because applying tags *replaces* a track's set. Without
    #: the current IDs to pre-select, re-filing a tagged track would strip its tags.
    my_tag_ids: list[str] = field(default_factory=list)
    colour_id: str | None = None
    comment: str | None = None


@dataclass
class Track:
    """One file moving through the pipeline."""

    source_path: Path
    state: TrackState = TrackState.PENDING

    # Populated by the pipeline
    info: SourceInfo | None = None
    quality: QualityReport | None = None
    library_status: LibraryStatus = LibraryStatus.NEW
    existing: ExistingTrack | None = None

    # User decisions
    target: ConversionTarget | None = None
    target_bit_depth: int | None = None
    target_sample_rate: int | None = None
    genre_folder: str | None = None       # e.g. "House"
    subgenre_folder: str | None = None    # e.g. "Soulful House"
    playlist_ids: list[str] = field(default_factory=list)
    my_tag_ids: list[str] = field(default_factory=list)
    colour_id: str | None = None

    #: Comment-mirror tokens, e.g. "lowq" for a track filed despite being an upscale.
    #: Consumed by the Comments mirror so a flagged track stays identifiable — and
    #: CDJ-searchable — long after intake.
    flags: list[str] = field(default_factory=list)

    # Results
    final_path: Path | None = None
    error: str | None = None

    @property
    def display_name(self) -> str:
        if self.info and self.info.tags.get("title"):
            artist = self.info.tags.get("artist")
            title = self.info.tags["title"]
            return f"{artist} - {title}" if artist else title
        return self.source_path.name

    @property
    def is_in_library(self) -> bool:
        return self.library_status is not LibraryStatus.NEW
