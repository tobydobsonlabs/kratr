"""Spectral quality analysis — the automated replacement for eyeballing Spek.

Two outputs:

* a Spek-equivalent spectrogram image, for when you want to look at it yourself;
* a **verdict**: the frequency cutoff, an inferred quality tier, and a flag when the
  file claims to be better than its spectrum supports.

The interesting part is the second one. A file can be a 50 MB WAV and still be an
upscaled 128 kbps MP3 underneath, and that is not obvious from anything except the
spectrum. What gives it away is not merely a low cutoff — plenty of genuinely
lossless old records roll off early — but a **brick wall**: lossy encoders discard
everything above their cutoff, producing a near-vertical cliff that nature never
makes. So the cutoff and the steepness of the drop are measured separately, and
only their combination is treated as evidence of a transcode.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import config
from ..models import QualityRating, QualityReport, SourceInfo
from . import ffmpeg

logger = logging.getLogger(__name__)

#: FFT size for the averaged spectrum. ~5.4 Hz resolution at 44.1 kHz.
_FFT_SIZE = 8192
#: Hop between analysis frames (50% overlap).
_HOP = _FFT_SIZE // 2
#: Bins to smooth over when looking for the cutoff, to ignore single-bin noise spikes.
_SMOOTH_BINS = 9
#: A drop steeper than this across ~1 kHz is a brick wall, i.e. an encoder's doing.
_BRICKWALL_DB = 25.0
#: How far above the estimated noise floor a bin must sit to count as real content.
_FLOOR_MARGIN_DB = 12.0
#: The floor-relative threshold is never allowed closer than this to the peak.
_MIN_PEAK_MARGIN_DB = 6.0
#: Above this cutoff a file is treated as full-bandwidth.
_FULL_BANDWIDTH_HZ = 20_000
#: Never decode more than this, as a guard against pathological input.
_MAX_DECODE_SECONDS = 20 * 60


def _decode_mono(
    path: Path, sample_rate: int, settings: config.Settings | None = None
) -> np.ndarray:
    """Decode to mono float32 at the file's own sample rate.

    The native rate is preserved deliberately: resampling would move or destroy the
    very cutoff we're trying to measure.
    """
    raw = ffmpeg.decode_pcm(
        path,
        sample_rate=sample_rate,
        channels=1,
        max_seconds=_MAX_DECODE_SECONDS,
        settings=settings,
    )
    return np.frombuffer(raw, dtype=np.float32)


def _loudest_window(samples: np.ndarray, sample_rate: int, seconds: float) -> np.ndarray:
    """Return the loudest contiguous window, by RMS.

    Analysing the loudest section avoids intros, fades and silence dragging the
    spectrum down and faking an early cutoff.
    """
    window = int(seconds * sample_rate)
    if window <= 0 or samples.size <= window:
        return samples

    # Coarse RMS envelope over 0.5 s blocks, then slide a window over it.
    block = max(1, sample_rate // 2)
    n_blocks = samples.size // block
    if n_blocks == 0:
        return samples
    trimmed = samples[: n_blocks * block].reshape(n_blocks, block)
    energy = np.mean(np.square(trimmed, dtype=np.float64), axis=1)

    blocks_per_window = max(1, window // block)
    if blocks_per_window >= n_blocks:
        return samples

    cumulative = np.concatenate([[0.0], np.cumsum(energy)])
    sums = cumulative[blocks_per_window:] - cumulative[:-blocks_per_window]
    start_block = int(np.argmax(sums))
    start = start_block * block
    return samples[start : start + window]


def _average_spectrum(samples: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Welch-style averaged magnitude spectrum, in dB.

    Returns (frequencies, levels_db).
    """
    if samples.size < _FFT_SIZE:
        samples = np.pad(samples, (0, _FFT_SIZE - samples.size))

    window = np.hanning(_FFT_SIZE).astype(np.float32)
    n_frames = 1 + (samples.size - _FFT_SIZE) // _HOP
    n_frames = max(1, min(n_frames, 4096))  # cap work on long windows

    accumulator = np.zeros(_FFT_SIZE // 2 + 1, dtype=np.float64)
    for i in range(n_frames):
        start = i * _HOP
        frame = samples[start : start + _FFT_SIZE]
        if frame.size < _FFT_SIZE:
            break
        spectrum = np.fft.rfft(frame * window)
        accumulator += np.abs(spectrum) ** 2

    accumulator /= max(1, n_frames)
    # Floor before log to avoid -inf on digital silence.
    levels_db = 10.0 * np.log10(np.maximum(accumulator, 1e-20))
    freqs = np.fft.rfftfreq(_FFT_SIZE, d=1.0 / sample_rate)
    return freqs, levels_db


def _smooth(values: np.ndarray, width: int) -> np.ndarray:
    """Moving average with edge replication.

    ``mode="same"`` would zero-pad, and these are dB values around −100, so padding
    with 0 dB lifts the top bins enormously — enough to put the measured cutoff on the
    very last bin of every file. Replicating the edges instead keeps the ends honest.
    """
    if width <= 1:
        return values
    pad = width // 2
    padded = np.pad(values, pad, mode="edge")
    kernel = np.ones(width) / width
    return np.convolve(padded, kernel, mode="valid")[: values.size]


def _find_cutoff(
    freqs: np.ndarray, levels_db: np.ndarray, threshold_db: float
) -> float | None:
    """Highest frequency still carrying real energy.

    Measured against the **noise floor**, not the spectral peak. Real music has a steep
    tilt — the peak sits in the bass and the top end can be 60–80 dB below it — so a
    peak-relative threshold reads the cutoff far too low. Verified on real files: a
    genuine 320 kbps track measured 7.7 kHz that way.

    What actually marks a cutoff is the drop to the encoder's dead zone. Estimating
    that floor from the quiet part of the upper spectrum and looking for the last bin
    meaningfully above it is tilt-independent, and gives a lossless file a cutoff near
    Nyquist because it never drops to a dead zone at all.
    """
    smoothed = _smooth(levels_db, _SMOOTH_BINS)
    peak = float(np.max(smoothed))

    upper_half = smoothed[len(smoothed) // 2:]
    floor = float(np.percentile(upper_half, 5)) if upper_half.size else peak - threshold_db

    # Clamped so a flat, noisy spectrum can't push the threshold above the content
    # itself and report no cutoff at all.
    threshold = min(floor + _FLOOR_MARGIN_DB, peak - _MIN_PEAK_MARGIN_DB)

    above = np.nonzero(smoothed >= threshold)[0]
    if above.size == 0:
        # Fall back to the peak-relative reading rather than giving up.
        above = np.nonzero(smoothed >= peak - threshold_db)[0]
        if above.size == 0:
            return None
    return float(freqs[above[-1]])


def _brickwall_drop(freqs: np.ndarray, levels_db: np.ndarray, cutoff_hz: float) -> float:
    """dB drop across the cutoff.

    Lossy encoders leave a cliff; natural high-frequency rolloff is gradual. This is
    what separates "an old recording with no top end" from "a transcode".
    """
    smoothed = _smooth(levels_db, _SMOOTH_BINS)

    below = (freqs >= cutoff_hz - 1500) & (freqs <= cutoff_hz - 200)
    above = (freqs >= cutoff_hz + 200) & (freqs <= cutoff_hz + 1500)
    if not below.any() or not above.any():
        return 0.0
    return float(np.mean(smoothed[below]) - np.mean(smoothed[above]))


def _rate_bitrate(kbps: int | None) -> QualityRating | None:
    """Rate a lossy file from its declared bitrate, which the container reports honestly."""
    if not kbps:
        return None
    if kbps >= 320:
        return QualityRating.HIGH
    if kbps >= 192:
        return QualityRating.MEDIUM
    if kbps >= 128:
        return QualityRating.LOW
    return QualityRating.POOR


def _rate_cutoff(cutoff_hz: float | None) -> QualityRating:
    if cutoff_hz is None:
        return QualityRating.UNKNOWN
    if cutoff_hz >= _FULL_BANDWIDTH_HZ:
        return QualityRating.LOSSLESS
    if cutoff_hz >= 19_000:
        return QualityRating.HIGH
    if cutoff_hz >= 17_500:
        return QualityRating.MEDIUM
    if cutoff_hz >= 15_500:
        return QualityRating.LOW
    return QualityRating.POOR


def analyse(
    info: SourceInfo, settings: config.Settings | None = None
) -> QualityReport:
    """Measure a file's real bandwidth and compare it against what it claims to be."""
    settings = settings or config.Settings()
    sample_rate = info.sample_rate or 44_100

    try:
        samples = _decode_mono(info.path, sample_rate, settings)
    except ffmpeg.FFmpegError as exc:
        logger.warning("Could not decode %s for analysis: %s", info.path.name, exc)
        return QualityReport(
            cutoff_hz=None,
            rating=QualityRating.UNKNOWN,
            notes=[f"Could not decode for analysis: {exc}"],
        )

    if samples.size == 0:
        return QualityReport(
            cutoff_hz=None, rating=QualityRating.UNKNOWN, notes=["File decoded to silence."]
        )

    window = _loudest_window(samples, sample_rate, settings.analysis_window_s)
    freqs, levels = _average_spectrum(window, sample_rate)
    cutoff = _find_cutoff(freqs, levels, settings.cutoff_threshold_db)

    notes: list[str] = []
    rating = _rate_cutoff(cutoff)
    suspect = False

    nyquist = sample_rate / 2.0
    if cutoff is not None:
        drop = _brickwall_drop(freqs, levels, cutoff)
        is_brickwall = drop >= _BRICKWALL_DB

        # Content right up to Nyquist means the rate, not the encoder, is the limit.
        if cutoff >= nyquist * 0.98:
            rating = QualityRating.LOSSLESS
            notes.append(f"Full bandwidth to {nyquist / 1000:.1f} kHz (Nyquist).")
        elif is_brickwall:
            notes.append(
                f"Sharp {drop:.0f} dB cliff at {cutoff / 1000:.1f} kHz — "
                f"the signature of lossy encoding."
            )
        else:
            notes.append(
                f"Gradual rolloff to {cutoff / 1000:.1f} kHz — consistent with the "
                f"recording itself rather than an encoder."
            )

        # The case worth catching: it claims lossless (or 320) but the spectrum disagrees.
        claims_lossless = not info.is_lossy
        claims_high_bitrate = bool(info.bitrate_kbps and info.bitrate_kbps >= 320)

        # A brick wall is the evidence, not where it happens to sit: a 320 kbps encoder
        # cuts around 20.4 kHz, above the 20 kHz "full bandwidth" mark, and that upscale
        # still needs catching. Anything genuinely lossless rolls off gradually instead.
        # The cap stops a steep sample-rate-conversion filter right at Nyquist — which
        # is not an encoder — from being read as one.
        suspect_below = nyquist * 0.95

        if is_brickwall and cutoff < suspect_below:
            if claims_lossless:
                suspect = True
                notes.append(
                    f"⚠ This is a lossless {info.fmt.label if info.fmt else info.codec} file, "
                    f"but its spectrum is that of a lossy source. Very likely an upscale — "
                    f"converting a lossy file to WAV/FLAC does not restore anything."
                )
            elif claims_high_bitrate and cutoff < 19_000:
                suspect = True
                notes.append(
                    f"⚠ Claims {info.bitrate_kbps} kbps but only reaches "
                    f"{cutoff / 1000:.1f} kHz — likely re-encoded from a lower bitrate."
                )

    if info.is_lossy:
        # For a natively-lossy file the container states the bitrate, and that is more
        # reliable than the spectrum: a float decode of an MP3 preserves the decoder's
        # own ultra-low-level artefacts all the way to Nyquist, so the measured cutoff
        # reads far too high. (The same audio written to 16-bit WAV quantises those
        # artefacts to silence, which is why the upscale check below still works.)
        bitrate_rating = _rate_bitrate(info.bitrate_kbps)
        if bitrate_rating is not None:
            rating = bitrate_rating
        elif rating is QualityRating.LOSSLESS:
            rating = QualityRating.HIGH

    return QualityReport(
        cutoff_hz=cutoff,
        rating=rating,
        is_suspect=suspect,
        notes=notes,
    )


class RecommendedAction(enum.Enum):
    FILE = "file"
    QUARANTINE = "quarantine"


#: Where an encoder's lowpass sits tells you the bitrate it was encoded at. LAME at
#: 44.1 kHz: 128 kbps ≈ 16 kHz, 192 ≈ 18.5, V0/256 ≈ 19.5, 320 ≈ 20.5.
#:
#: The distinction matters because an upscale is not one thing. A 320 kbps source in a
#: WAV wrapper is *transparent* — it will not be heard on a club system — and pulling it
#: out of the library costs a playable track to fix a labelling problem. A 128 kbps
#: source genuinely falls apart when it's loud. Same detection, different consequence,
#: so they get different advice.
_TRANSPARENT_CLIFF_HZ = 19_000   # at or above: 256/V0/320 underneath
_POOR_CLIFF_HZ = 17_500          # below: 128–160 underneath


@dataclass(slots=True)
class _Tier:
    """What a cliff frequency implies about the source, and what to do about it."""

    source: str
    action: RecommendedAction
    severity: str
    guidance: str


def _upscale_tier(cutoff_hz: float | None) -> _Tier:
    if cutoff_hz is not None and cutoff_hz >= _TRANSPARENT_CLIFF_HZ:
        return _Tier(
            source="a 320 kbps file",
            action=RecommendedAction.FILE,
            severity="warn",
            guidance=(
                "At this bitrate that is a sound-quality non-issue: 320 kbps is "
                "transparent in a mix on a club system, and nobody will hear it. What "
                "is wrong is the labelling — it takes up lossless disk space and looks "
                "lossless in your library when it isn't.\n\n"
                "File it. It will be marked and added to the re-source list so you can "
                "replace it if a real lossless copy ever turns up. Quarantine it "
                "instead only if you can re-buy it right now."
            ),
        )

    if cutoff_hz is not None and cutoff_hz < _POOR_CLIFF_HZ:
        return _Tier(
            source="around 128 kbps",
            action=RecommendedAction.QUARANTINE,
            severity="bad",
            guidance=(
                "That is low enough to hear on a loud rig — hi-hats and cymbals go "
                "grainy, and EQing the top back in only makes it worse.\n\n"
                "Quarantine it and re-source. File it anyway only if the track is rare "
                "and this is genuinely the only copy in existence."
            ),
        )

    return _Tier(
        source="a 192–256 kbps file",
        action=RecommendedAction.FILE,
        severity="warn",
        guidance=(
            "That is the awkward middle: better than most people notice, short of "
            "transparent. It will hold up in a busy mix and can sound slightly hard on "
            "exposed cymbals at volume.\n\n"
            "Genuinely a toss-up. Quarantine it if the track is easy to re-buy; file it "
            "and move on if it isn't."
        ),
    )


@dataclass(slots=True)
class Recommendation:
    action: RecommendedAction
    headline: str
    explanation: str
    severity: str  # "good" | "warn" | "bad"


def recommend(info: SourceInfo, report: QualityReport) -> Recommendation:
    """Turn the measurement into advice.

    The verdict is about **sourcing**, not the file: whether a better copy is worth
    chasing. So the recommendation says what to do, and why.
    """
    cutoff = f"{report.cutoff_khz} kHz" if report.cutoff_khz else "unknown"

    if report.is_suspect:
        tier = _upscale_tier(report.cutoff_hz)
        verdict = (
            "fine to play, worth replacing"
            if tier.action is RecommendedAction.FILE
            else "worth re-sourcing"
        )
        return Recommendation(
            action=tier.action,
            headline=f"Upscale — {tier.source} underneath, {verdict}",
            explanation=(
                f"It is a {info.fmt.label if info.fmt else info.codec} file, so it looks "
                f"lossless, but the spectrum stops dead at {cutoff} with a sharp cliff. "
                f"Nothing natural produces that edge — only a lossy encoder does. "
                f"Converting it to WAV or FLAC cannot bring back what was discarded.\n\n"
                f"{tier.guidance}"
            ),
            severity=tier.severity,
        )

    if report.rating in (QualityRating.LOSSLESS, QualityRating.HIGH):
        if info.is_lossy:
            return Recommendation(
                action=RecommendedAction.FILE,
                headline=f"Good quality {info.bitrate_kbps or 320} kbps — fine to file",
                explanation=(
                    "A genuine high-bitrate encode. Indistinguishable from lossless on a "
                    "club system in a mix. Nothing to worry about.\n\n"
                    "It will not be re-encoded — converting one lossy file to another only "
                    "loses more."
                ),
                severity="good",
            )
        return Recommendation(
            action=RecommendedAction.FILE,
            headline="Genuine lossless — file it",
            explanation=(
                f"Full bandwidth up to {cutoff}, rolling off naturally rather than being "
                f"cut. This is a real lossless file, not something upscaled from a lossy "
                f"source."
            ),
            severity="good",
        )

    if info.is_lossy:
        return Recommendation(
            action=RecommendedAction.FILE,
            headline=f"Low bitrate ({info.bitrate_kbps} kbps) — audible on a big system",
            explanation=(
                "Honestly labelled, but genuinely low quality. Hi-hats and cymbals will "
                "sound washy on a loud rig, and EQing it will make that worse.\n\n"
                "Worth re-sourcing if you can. File it if the track is rare."
            ),
            severity="warn",
        )

    return Recommendation(
        action=RecommendedAction.FILE,
        headline=f"Limited bandwidth to {cutoff} — but no sign of transcoding",
        explanation=(
            "The top end rolls off gradually rather than stopping at a cliff, which is "
            "what an older recording, a vinyl rip or a period master looks like. That is "
            "the recording itself, not damage from an encoder.\n\n"
            "Perfectly normal for older material — file it."
        ),
        severity="warn",
    )


def render_spectrogram(
    path: Path,
    output: Path,
    settings: config.Settings | None = None,
    size: str = "900x420",
) -> Path:
    """Render a Spek-style spectrogram image."""
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg.ffmpeg_path(settings),
        "-v", "error",
        "-y",
        "-t", str(_MAX_DECODE_SECONDS),
        "-i", str(path),
        "-lavfi", f"showspectrumpic=s={size}:legend=1:gain=3:color=intensity",
        "-frames:v", "1",
        str(output),
    ]
    ffmpeg.run(cmd, timeout=300)
    return output


def spectrogram_cache_path(path: Path) -> Path:
    """Stable cache location for a file's spectrogram, keyed by path + mtime + size."""
    import hashlib

    stat = path.stat()
    key = f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return config.CACHE_DIR / "spectrograms" / f"{digest}.png"
