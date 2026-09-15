"""Read what a file actually is.

Purely factual — no judgement about quality lives here. Crucially the *codec* is
trusted over the file extension, because files that lie about their format are
exactly what the quality checker exists to catch.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import config
from ..models import CODEC_TO_FORMAT, AudioFormat, SourceInfo
from . import ffmpeg

logger = logging.getLogger(__name__)

#: Metadata keys we care about, mapped from the many spellings ffprobe reports
#: across Vorbis comments, MP4 atoms, ID3 and RIFF INFO.
_TAG_ALIASES: dict[str, str] = {
    "title": "title",
    "tit2": "title",
    "inam": "title",
    "artist": "artist",
    "tpe1": "artist",
    "author": "artist",
    "iart": "artist",
    "album_artist": "album_artist",
    "albumartist": "album_artist",
    "tpe2": "album_artist",
    "album": "album",
    "talb": "album",
    "iprd": "album",
    "genre": "genre",
    "tcon": "genre",
    "ignr": "genre",
    "date": "date",
    "year": "date",
    "tyer": "date",
    "tdrc": "date",
    "icrd": "date",
    "comment": "comment",
    "comm": "comment",
    "icmt": "comment",
    "track": "track",
    "trck": "track",
    "label": "label",
    "publisher": "label",
    "tpub": "label",
    "bpm": "bpm",
    "tbpm": "bpm",
    "initialkey": "key",
    "tkey": "key",
    "remixer": "remixer",
    "composer": "composer",
    "tcom": "composer",
}


def normalise_tags(raw: dict[str, str]) -> dict[str, str]:
    """Fold ffprobe's many tag spellings into a small canonical set."""
    out: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        canonical = _TAG_ALIASES.get(key.strip().lower())
        if canonical and canonical not in out:
            out[canonical] = text
    return out


def _first_audio_stream(data: dict) -> dict | None:
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream
    return None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _bit_depth(stream: dict) -> int | None:
    # ffprobe reports bit depth differently depending on codec.
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        depth = _int_or_none(stream.get(key))
        if depth:
            return depth
    return None


def probe(path: Path, settings: config.Settings | None = None) -> SourceInfo:
    """Inspect a media file.

    Raises:
        FileNotFoundError: the path doesn't exist.
        ValueError: the file contains no audio stream.
        ffmpeg.FFmpegError: ffprobe rejected the file.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    data = ffmpeg.probe_json(path, settings)
    stream = _first_audio_stream(data)
    if stream is None:
        raise ValueError(f"No audio stream in {path.name}")

    fmt_section = data.get("format", {})
    codec = (stream.get("codec_name") or "").lower()

    # Bitrate: prefer the stream's, fall back to the container's.
    bitrate = _int_or_none(stream.get("bit_rate")) or _int_or_none(fmt_section.get("bit_rate"))
    duration = _float_or_none(stream.get("duration")) or _float_or_none(
        fmt_section.get("duration")
    )

    # Tags can live on either the container or the stream depending on format.
    raw_tags: dict[str, str] = {}
    raw_tags.update(fmt_section.get("tags") or {})
    raw_tags.update(stream.get("tags") or {})

    fmt = CODEC_TO_FORMAT.get(codec)
    if fmt is None:
        logger.debug("Unmapped codec %r for %s", codec, path.name)

    # AAC and ALAC share the .m4a container, so disambiguate on the codec.
    if codec == "alac":
        fmt = AudioFormat.ALAC

    info = SourceInfo(
        path=path,
        codec=codec,
        container=(fmt_section.get("format_name") or "").split(",")[0],
        fmt=fmt,
        bitrate=bitrate,
        sample_rate=_int_or_none(stream.get("sample_rate")),
        bit_depth=_bit_depth(stream),
        channels=_int_or_none(stream.get("channels")),
        duration=duration,
        file_size=path.stat().st_size,
        tags=normalise_tags(raw_tags),
    )

    # A mismatch here is a real signal, not a curiosity: it means the extension lies.
    if info.fmt and info.fmt.value != path.suffix.lstrip(".").lower():
        logger.info(
            "%s: extension says %s but codec is %s (%s)",
            path.name, path.suffix, codec, info.fmt.label,
        )

    return info
