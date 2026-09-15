"""Format conversion.

The target format is chosen **per track** — never forced. Converting everything to
WAV is the old workflow, and it is explicitly not what this does.

Two conversions damage a library quietly, so both are surfaced as warnings before
they happen (never hard-blocked — an override is always available):

* **lossy → lossy** loses another generation for nothing.
* **lossy → lossless** inflates the file without recovering anything, and manufactures
  exactly the fake-lossless files :mod:`crate.pipeline.quality` exists to catch.

Metadata survives every path. That matters most for WAV, whose native metadata
support is poor: without an explicit ID3v2 chunk a converted file arrives in
rekordbox with no title or artist at all.
"""

from __future__ import annotations

import enum
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..models import AudioFormat, ConversionTarget, KeepOriginal, SourceInfo
from . import ffmpeg

logger = logging.getLogger(__name__)

#: Formats that play on effectively all Pioneer/CDJ gear. FLAC and ALAC are the odd
#: ones out — FLAC support starts at the CDJ-2000NXS2, and ALAC/m4a is patchier still —
#: so a flagged upscale in one of those is defaulted to MP3 for guaranteed playback.
_UNIVERSALLY_PLAYABLE = {AudioFormat.WAV, AudioFormat.AIFF, AudioFormat.MP3}


class WarningKind(enum.Enum):
    LOSSY_TO_LOSSY = "lossy_to_lossy"
    LOSSY_TO_LOSSLESS = "lossy_to_lossless"
    DOWNSAMPLING = "downsampling"
    BIT_DEPTH_REDUCTION = "bit_depth_reduction"
    REENCODING_ANALYSED = "reencoding_analysed"


@dataclass(frozen=True, slots=True)
class ConversionWarning:
    kind: WarningKind
    message: str
    #: Warnings never block. They exist so the choice is made knowingly.
    severe: bool = False


@dataclass(slots=True)
class ConversionPlan:
    """What will happen to a file, and what the user should know before it does."""

    source: SourceInfo
    target: ConversionTarget
    bit_depth: int | None = None
    sample_rate: int | None = None
    warnings: list[ConversionWarning] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return self.target is KeepOriginal

    @property
    def target_format(self) -> AudioFormat | None:
        return None if self.target is KeepOriginal else self.target  # type: ignore[return-value]

    @property
    def extension(self) -> str:
        fmt = self.target_format
        return f".{fmt.value}" if fmt else self.source.path.suffix

    @property
    def has_severe_warning(self) -> bool:
        return any(w.severe for w in self.warnings)


# --------------------------------------------------------------------- planning
def plan(
    info: SourceInfo,
    target: ConversionTarget,
    *,
    bit_depth: int | None = None,
    sample_rate: int | None = None,
    is_analysed: bool = False,
    is_suspect: bool = False,
) -> ConversionPlan:
    """Work out what a conversion would do, including anything worth warning about.

    ``is_suspect`` marks a file the quality check flagged as a likely upscale. Such a
    file is lossy at heart, so re-encoding it to a compatible lossy format (MP3) is a
    deliberate, expected trade — the lossy→lossy warning stays informative but stops
    being a severe block on that path.
    """
    warnings: list[ConversionWarning] = []
    fmt = None if target is KeepOriginal else target

    if fmt is not None:
        if info.is_lossy and not fmt.is_lossless:
            if is_suspect:
                message = (
                    f"{info.codec.upper()} → {fmt.label} re-encodes an already-lossy file, "
                    f"so it loses another generation — but this track is a likely upscale, "
                    f"and MP3 makes it play on all gear without pretending to be lossless. "
                    f"Re-sourcing a real copy is still better if you can."
                )
            else:
                message = (
                    f"{info.codec.upper()} → {fmt.label} re-encodes an already-lossy file. "
                    f"You lose another generation and gain nothing."
                )
            warnings.append(
                ConversionWarning(
                    WarningKind.LOSSY_TO_LOSSY,
                    message,
                    severe=not is_suspect,
                )
            )
        elif info.is_lossy and fmt.is_lossless:
            size_mb = info.file_size / 1e6
            warnings.append(
                ConversionWarning(
                    WarningKind.LOSSY_TO_LOSSLESS,
                    f"{info.codec.upper()} → {fmt.label} cannot recover what the lossy "
                    f"encoder discarded. It only inflates {size_mb:.0f} MB into a much "
                    f"larger file that looks lossless but isn't.",
                    severe=True,
                )
            )

        if sample_rate and info.sample_rate and sample_rate < info.sample_rate:
            warnings.append(
                ConversionWarning(
                    WarningKind.DOWNSAMPLING,
                    f"Resampling {info.sample_rate / 1000:.1f} kHz → "
                    f"{sample_rate / 1000:.1f} kHz discards high frequencies permanently.",
                )
            )

        if bit_depth and info.bit_depth and bit_depth < info.bit_depth:
            warnings.append(
                ConversionWarning(
                    WarningKind.BIT_DEPTH_REDUCTION,
                    f"Reducing {info.bit_depth}-bit → {bit_depth}-bit loses dynamic range.",
                )
            )

        if is_analysed:
            warnings.append(
                ConversionWarning(
                    WarningKind.REENCODING_ANALYSED,
                    "This track is already in your rekordbox collection and analysed. "
                    "A lossless→lossless conversion keeps sample timing exact so cues and "
                    "beatgrids stay valid; anything else may shift them.",
                    severe=info.is_lossy or (fmt is not None and not fmt.is_lossless),
                )
            )

    return ConversionPlan(
        source=info,
        target=target,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        warnings=warnings,
    )


def plan_from_settings(
    info: SourceInfo,
    settings: config.Settings,
    *,
    is_analysed: bool = False,
    is_suspect: bool = False,
) -> ConversionPlan:
    """Build the default plan for a file from the per-source rules.

    A track flagged as an upscale is lossy at heart. Inflating it into a big WAV/FLAC
    only manufactures a fake-lossless file, so it is never converted *up*. But leaving
    a suspect FLAC (or ALAC) as-is can mean it won't play on older gear — CDJ FLAC
    support starts at the NXS2 — so those default to **MP3 320**: honest about the
    quality and playable everywhere. A suspect file already in a universally playable
    format (WAV, AIFF, MP3) is left alone — re-encoding it only loses a generation to
    save disk. The dropdown stays editable, so quarantining and re-sourcing a real
    copy is always one click away.
    """
    if is_suspect:
        already_playable = info.fmt is None or info.fmt in _UNIVERSALLY_PLAYABLE
        target: ConversionTarget = KeepOriginal if already_playable else AudioFormat.MP3
        return plan(info, target, is_analysed=is_analysed, is_suspect=True)

    target_value = settings.target_for_source(info.fmt)
    target: ConversionTarget = KeepOriginal
    if target_value is not None:
        try:
            target = AudioFormat(target_value)
        except ValueError:
            logger.warning("Unknown target format %r in settings; keeping original", target_value)
            target = KeepOriginal

    # No point "converting" to the format it already is.
    if target is not KeepOriginal and target is info.fmt:
        target = KeepOriginal

    return plan(
        info,
        target,
        bit_depth=settings.default_bit_depth,
        sample_rate=settings.default_sample_rate,
        is_analysed=is_analysed,
    )


# --------------------------------------------------------------- encoder args
#: PCM sample formats by container endianness and bit depth.
_PCM_CODECS: dict[tuple[AudioFormat, int], str] = {
    (AudioFormat.WAV, 16): "pcm_s16le",
    (AudioFormat.WAV, 24): "pcm_s24le",
    (AudioFormat.WAV, 32): "pcm_s32le",
    (AudioFormat.AIFF, 16): "pcm_s16be",
    (AudioFormat.AIFF, 24): "pcm_s24be",
    (AudioFormat.AIFF, 32): "pcm_s32be",
}

#: Formats whose ffmpeg muxer needs an explicit flag to write ID3v2 metadata.
#: Without this a converted WAV reaches rekordbox with no title or artist.
_NEEDS_ID3_FLAG = {AudioFormat.WAV, AudioFormat.AIFF}

#: Metadata fields carried across, mapped to the keys ffmpeg understands.
_METADATA_FIELDS = (
    ("title", "title"),
    ("artist", "artist"),
    ("album", "album"),
    ("album_artist", "album_artist"),
    ("genre", "genre"),
    ("date", "date"),
    ("track", "track"),
    ("composer", "composer"),
    ("comment", "comment"),
)


def _codec_args(plan_: ConversionPlan) -> list[str]:
    fmt = plan_.target_format
    assert fmt is not None

    depth = plan_.bit_depth or plan_.source.bit_depth or 16
    if depth not in (16, 24, 32):
        depth = 16

    if fmt in (AudioFormat.WAV, AudioFormat.AIFF):
        codec = _PCM_CODECS.get((fmt, depth), _PCM_CODECS[(fmt, 16)])
        return ["-c:a", codec]

    if fmt is AudioFormat.FLAC:
        # FLAC supports 16 and 24 bit; s32 is how ffmpeg represents 24.
        return ["-c:a", "flac", "-sample_fmt", "s16" if depth == 16 else "s32"]

    if fmt is AudioFormat.ALAC:
        return ["-c:a", "alac", "-sample_fmt", "s16p" if depth == 16 else "s32p"]

    if fmt is AudioFormat.MP3:
        return ["-c:a", "libmp3lame", "-b:a", "320k"]

    raise ValueError(f"No encoder configured for {fmt}")


def _metadata_args(info: SourceInfo, fmt: AudioFormat) -> list[str]:
    args: list[str] = []
    if fmt in _NEEDS_ID3_FLAG:
        args += ["-write_id3v2", "1"]
    for source_key, ffmpeg_key in _METADATA_FIELDS:
        value = info.tags.get(source_key)
        if value:
            args += ["-metadata", f"{ffmpeg_key}={value}"]
    return args


def output_path_for(plan_: ConversionPlan, dest_dir: Path) -> Path:
    return dest_dir / (plan_.source.path.stem + plan_.extension)


# --------------------------------------------------------------- conversion
def convert(
    plan_: ConversionPlan,
    output: Path,
    settings: config.Settings | None = None,
    *,
    overwrite: bool = False,
) -> Path:
    """Execute a conversion plan, writing to ``output``.

    Returns the written path. If the plan is a no-op the source is copied unchanged.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise FileExistsError(output)

    if plan_.is_noop:
        if output.resolve() != plan_.source.path.resolve():
            shutil.copy2(plan_.source.path, output)
        return output

    fmt = plan_.target_format
    assert fmt is not None

    # Write to a temp file first so a failed encode can't leave a half-written
    # track sitting in the music library looking valid.
    tmp = output.with_name(output.name + ".crate-tmp" + output.suffix)
    tmp.unlink(missing_ok=True)

    cmd = [
        ffmpeg.ffmpeg_path(settings),
        "-v", "error",
        "-y",
        "-i", str(plan_.source.path),
        "-map", "0:a:0",
        "-map_metadata", "0",
    ]
    cmd += _codec_args(plan_)
    if plan_.sample_rate:
        cmd += ["-ar", str(plan_.sample_rate)]
    cmd += _metadata_args(plan_.source, fmt)
    cmd.append(str(tmp))

    try:
        ffmpeg.run(cmd, timeout=1800)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    tmp.replace(output)
    logger.info(
        "Converted %s → %s (%s)", plan_.source.path.name, output.name, fmt.label
    )
    return output
