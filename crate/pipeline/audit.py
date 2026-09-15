"""Whole-library upscale audit.

The intake checker only protects tracks from here on. There are 2,913 WAVs already in
the library, some of which are certainly 320 kbps downloads converted before they ever
arrived — and those are the ones already in live sets.

Strictly read-only: it measures and reports, and every action is taken by hand
afterwards. Results are cached per file (path + mtime + size), so a re-run after a
partial pass costs almost nothing.

A full pass is also the best available check on the detector's false-positive rate.
The library is full of Street Soul, Dub/Reggae, Soul + Funk and 90s material where an
early rolloff is genuine, so if the gradual-vs-cliff discrimination is wrong, 3,300
real files will show it far more convincingly than synthetic fixtures can.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import config
from ..models import QualityRating
from . import probe, quality

logger = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".wav", ".aiff", ".aif", ".flac", ".mp3", ".m4a"}

CACHE_PATH = config.CACHE_DIR / "audit-cache.json"

#: Worst first — the ordering that makes the output a to-do list.
_RANK = {
    QualityRating.POOR: 0,
    QualityRating.LOW: 1,
    QualityRating.MEDIUM: 2,
    QualityRating.HIGH: 3,
    QualityRating.LOSSLESS: 4,
    QualityRating.UNKNOWN: 5,
}


@dataclass(slots=True)
class AuditRow:
    path: str
    folder: str
    artist: str
    title: str
    source_format: str
    bitrate_kbps: str
    cutoff_khz: str
    rating: str
    suspect: str
    note: str


def find_tracks(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES and not p.name.startswith(".")
    )


def _key(path: Path) -> str:
    stat = path.stat()
    return f"{path}|{stat.st_mtime_ns}|{stat.st_size}"


def load_cache(cache_path: Path | None = None) -> dict[str, dict]:
    cache_path = cache_path or CACHE_PATH
    if not cache_path.is_file():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Audit cache unreadable; starting fresh")
        return {}


def save_cache(cache: dict[str, dict], cache_path: Path | None = None) -> None:
    cache_path = cache_path or CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache), encoding="utf-8")


def analyse_one(path: Path, settings: config.Settings, root: Path) -> AuditRow:
    info = probe.probe(path, settings)
    report = quality.analyse(info, settings)

    try:
        folder = str(path.parent.relative_to(root))
    except ValueError:
        folder = str(path.parent)

    return AuditRow(
        path=str(path),
        folder=folder,
        artist=info.tags.get("artist", ""),
        title=info.tags.get("title", "") or path.stem,
        source_format=info.fmt.label if info.fmt else info.codec,
        bitrate_kbps=str(info.bitrate_kbps or ""),
        cutoff_khz=str(report.cutoff_khz or ""),
        rating=report.rating.value,
        suspect="YES" if report.is_suspect else "",
        note=report.notes[-1] if report.notes else "",
    )


def run(
    root: Path | None = None,
    settings: config.Settings | None = None,
    *,
    workers: int = 4,
    limit: int | None = None,
    progress: Callable[[int, int, Path], None] | None = None,
    cache_path: Path | None = None,
) -> list[AuditRow]:
    """Analyse every track under ``root``. Returns rows ranked worst-first."""
    settings = settings or config.Settings.load()
    root = Path(root or settings.music_root)
    if not root.is_dir():
        raise FileNotFoundError(root)

    tracks = find_tracks(root)
    if limit:
        tracks = tracks[:limit]

    cache = load_cache(cache_path)
    rows: list[AuditRow] = []
    pending: list[Path] = []

    for path in tracks:
        cached = cache.get(_key(path))
        if cached is not None:
            rows.append(AuditRow(**cached))
        else:
            pending.append(path)

    logger.info("Audit: %d track(s), %d cached, %d to analyse",
                len(tracks), len(rows), len(pending))

    done = len(rows)
    total = len(tracks)
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(analyse_one, p, settings, root): p for p in pending}
            for index, future in enumerate(as_completed(futures), start=1):
                path = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop the pass
                    logger.warning("Could not analyse %s: %s", path.name, exc)
                    continue
                rows.append(row)
                cache[_key(path)] = asdict(row)
                done += 1
                if progress:
                    progress(done, total, path)
                if index % 50 == 0:
                    save_cache(cache, cache_path)  # resumable

    save_cache(cache, cache_path)
    rows.sort(key=lambda r: (
        0 if r.suspect else 1,
        _RANK.get(QualityRating(r.rating), 9),
        float(r.cutoff_khz) if r.cutoff_khz else 99.0,
    ))
    return rows


def write_report(rows: Iterable[AuditRow], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [f for f in AuditRow.__dataclass_fields__]
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    return output


def summarise(rows: list[AuditRow]) -> str:
    suspect = [r for r in rows if r.suspect]
    by_rating: dict[str, int] = {}
    for row in rows:
        by_rating[row.rating] = by_rating.get(row.rating, 0) + 1

    lines = [f"{len(rows)} track(s) analysed", ""]
    for rating in ("lossless", "high", "medium", "low", "poor", "unknown"):
        if rating in by_rating:
            lines.append(f"  {rating:<10} {by_rating[rating]}")
    lines.append("")
    lines.append(f"{len(suspect)} flagged as likely upscales:")
    for row in suspect[:20]:
        who = f"{row.artist} - {row.title}" if row.artist else row.title
        lines.append(f"  {row.cutoff_khz:>5} kHz  {who}   [{row.folder}]")
    if len(suspect) > 20:
        lines.append(f"  … and {len(suspect) - 20} more")
    return "\n".join(lines)
