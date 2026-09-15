"""Spotting the same recording arriving twice.

The hard part is **not** flagging different versions. A remix, dub or edit shares its
artist and title with the original but is a completely different record, and telling
someone to delete one would be worse than saying nothing at all. So the mix/version
in brackets is treated as part of a track's identity, and duration is required to
agree as well — two files are only the same recording if their artist, base title,
version *and* length all line up.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..models import QualityRating, SourceInfo, Track

logger = logging.getLogger(__name__)

#: Bracketed or dash-suffixed segments carry the version: "(Restless Soul Mix)",
#: "[Dub]", "- Radio Edit".
_BRACKETS = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_DASH_SUFFIX = re.compile(r"\s[-–—]\s([^-–—]+)$")

#: Words that mean "this is the plain version", so "Track" and "Track (Original Mix)"
#: are recognised as the same recording rather than two different ones.
_NEUTRAL_VERSIONS = {
    "original", "original mix", "originalmix", "album version", "album mix",
    "main", "main mix", "", "master",
}

#: Words that mark a segment as a version rather than part of the title, so
#: "Fingers (feat. Someone)" isn't mistaken for a different mix.
_VERSION_HINTS = (
    "mix", "remix", "edit", "dub", "version", "instrumental", "acapella", "vip",
    "rework", "bootleg", "live", "radio", "extended", "remaster", "re-edit",
    "reprise", "interlude", "beats",
)

#: Durations must agree within the looser of these to count as the same recording.
_DURATION_TOLERANCE_S = 12.0
_DURATION_TOLERANCE_RATIO = 0.04


def _normalise(text: str) -> str:
    text = text.casefold().strip()
    text = re.sub(r"&", " and ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_version(segment: str) -> bool:
    lowered = segment.casefold()
    return any(hint in lowered for hint in _VERSION_HINTS)


def split_title(title: str) -> tuple[str, str]:
    """Separate a title into its base and its version.

    ``"Telling On Me (Restless Soul Mix)"`` → ``("telling on me", "restless soul mix")``
    ``"Nocturne (feat. Someone)"``          → ``("nocturne feat someone", "")``
    """
    versions: list[str] = []

    def take(match: re.Match) -> str:
        segment = match.group(1)
        if _looks_like_version(segment):
            versions.append(segment)
            return " "
        return f" {segment} "

    base = _BRACKETS.sub(take, title)

    dash = _DASH_SUFFIX.search(base)
    if dash and _looks_like_version(dash.group(1)):
        versions.append(dash.group(1))
        base = base[: dash.start()]

    version = _normalise(" ".join(versions))
    if version in _NEUTRAL_VERSIONS:
        version = ""
    return _normalise(base), version


def _artist_and_title(info: SourceInfo) -> tuple[str, str]:
    """Best available artist/title, falling back to an ``Artist - Title`` filename."""
    artist = info.tags.get("artist", "")
    title = info.tags.get("title", "")
    if artist and title:
        return artist, title

    stem = info.path.stem
    if " - " in stem:
        left, right = stem.split(" - ", 1)
        return artist or left, title or right
    return artist, title or stem


@dataclass(frozen=True, slots=True)
class Signature:
    artist: str
    base_title: str
    version: str

    @property
    def is_usable(self) -> bool:
        # Without a title there is nothing to compare, and grouping on artist alone
        # would collapse an entire EP into one "duplicate" set.
        return bool(self.base_title)


def signature(info: SourceInfo) -> Signature:
    artist, title = _artist_and_title(info)
    base, version = split_title(title)
    return Signature(artist=_normalise(artist), base_title=base, version=version)


def same_length(a: SourceInfo, b: SourceInfo) -> bool:
    """True when two files are close enough in length to be the same recording."""
    if not a.duration or not b.duration:
        return True  # unknown duration shouldn't veto an otherwise strong match
    longest = max(a.duration, b.duration)
    tolerance = max(_DURATION_TOLERANCE_S, longest * _DURATION_TOLERANCE_RATIO)
    return abs(a.duration - b.duration) <= tolerance


def quality_score(track: Track) -> tuple:
    """Sort key, best first. Higher is better on every element."""
    info, report = track.info, track.quality
    if info is None:
        return (-1,)

    lossless = 0 if info.is_lossy else 1
    trustworthy = 0 if (report and report.is_suspect) else 1
    tier = {
        QualityRating.LOSSLESS: 5, QualityRating.HIGH: 4, QualityRating.MEDIUM: 3,
        QualityRating.LOW: 2, QualityRating.POOR: 1, QualityRating.UNKNOWN: 0,
    }.get(report.rating if report else QualityRating.UNKNOWN, 0)
    cutoff = report.cutoff_hz or 0 if report else 0

    # Suspect beats nothing, but a genuine lossy file beats a fake lossless one.
    return (trustworthy, lossless, tier, cutoff, info.bitrate or 0, info.file_size)


def describe_advantage(best: Track, other: Track) -> str:
    """Why the winner won, in the words that matter to a DJ."""
    best_info, other_info = best.info, other.info
    if best_info is None or other_info is None:
        return "more complete file"

    if other.quality and other.quality.is_suspect and not (best.quality and best.quality.is_suspect):
        return "the other copy is an upscale"
    if not best_info.is_lossy and other_info.is_lossy:
        return f"lossless vs {other_info.fmt.label if other_info.fmt else other_info.codec}"
    if best.quality and other.quality:
        if (best.quality.cutoff_hz or 0) > (other.quality.cutoff_hz or 0) + 500:
            return f"reaches {best.quality.cutoff_khz} kHz vs {other.quality.cutoff_khz} kHz"
    if (best_info.bitrate or 0) > (other_info.bitrate or 0):
        return f"{best_info.bitrate_kbps} kbps vs {other_info.bitrate_kbps} kbps"
    if best_info.file_size > other_info.file_size:
        return "larger file"
    return "equivalent — keep either"


@dataclass
class DuplicateGroup:
    """Tracks believed to be the same recording."""

    signature: Signature
    tracks: list[Track] = field(default_factory=list)

    @property
    def best(self) -> Track:
        return max(self.tracks, key=quality_score)

    @property
    def others(self) -> list[Track]:
        best = self.best
        return [t for t in self.tracks if t is not best]

    @property
    def label(self) -> str:
        name = self.signature.base_title.title()
        if self.signature.version:
            name += f" ({self.signature.version.title()})"
        return name

    def recommendation(self) -> str:
        best = self.best
        reasons = {describe_advantage(best, other) for other in self.others}
        why = "; ".join(sorted(reasons))
        return (
            f"{len(self.tracks)} copies of “{self.label}”. "
            f"Keep {best.display_name} — {why}."
        )


def find_groups(tracks: list[Track]) -> list[DuplicateGroup]:
    """Group tracks that are very likely the same recording.

    Different mixes of the same song are deliberately kept apart.
    """
    buckets: dict[Signature, list[Track]] = {}
    for track in tracks:
        if track.info is None:
            continue
        sig = signature(track.info)
        if not sig.is_usable:
            continue
        buckets.setdefault(sig, []).append(track)

    groups: list[DuplicateGroup] = []
    for sig, candidates in buckets.items():
        if len(candidates) < 2:
            continue
        # Duration has to agree too — same title and version but a wildly different
        # length usually means an extended cut or a mislabelled file.
        for cluster in _cluster_by_length(candidates):
            if len(cluster) > 1:
                groups.append(DuplicateGroup(sig, cluster))

    groups.sort(key=lambda g: g.label)
    return groups


def _cluster_by_length(tracks: list[Track]) -> list[list[Track]]:
    clusters: list[list[Track]] = []
    for track in sorted(tracks, key=lambda t: t.info.duration or 0):
        for cluster in clusters:
            if same_length(cluster[0].info, track.info):
                cluster.append(track)
                break
        else:
            clusters.append([track])
    return clusters


def duplicates_of(track: Track, groups: list[DuplicateGroup]) -> DuplicateGroup | None:
    for group in groups:
        if any(t is track for t in group.tracks):
            return group
    return None
