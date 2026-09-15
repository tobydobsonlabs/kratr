"""The re-source list — every track flagged as an upscale, whatever was done with it.

Both routes through the upscale protocol land here: quarantining a track and filing
it anyway both add a row. Nothing flagged passes unrecorded, so re-buying is a batch
job against a list rather than something remembered at 2am.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .. import config
from ..models import QualityReport, SourceInfo

logger = logging.getLogger(__name__)

#: Marker written into the rekordbox comment field by CRA-25, so a flagged track
#: stays identifiable — and searchable on a CDJ — long after intake.
LOW_QUALITY_FLAG = "lowq"


class Action:
    FILED_ANYWAY = "filed-anyway"
    QUARANTINED = "quarantined"


@dataclass(slots=True)
class ReSourceEntry:
    timestamp: str
    action: str
    artist: str
    title: str
    cutoff_khz: str
    rating: str
    source_format: str
    bitrate_kbps: str
    path: str
    resolved: str = ""  # set by hand once a better copy has been bought

    @classmethod
    def build(
        cls,
        info: SourceInfo,
        report: QualityReport,
        action: str,
        final_path: Path | None = None,
    ) -> "ReSourceEntry":
        return cls(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            action=action,
            artist=info.tags.get("artist", ""),
            title=info.tags.get("title", "") or info.path.stem,
            cutoff_khz=f"{report.cutoff_khz}" if report.cutoff_khz else "",
            rating=report.rating.value,
            source_format=info.fmt.label if info.fmt else info.codec,
            bitrate_kbps=str(info.bitrate_kbps or ""),
            path=str(final_path or info.path),
        )


def _fieldnames() -> list[str]:
    return [f.name for f in fields(ReSourceEntry)]


def append(entry: ReSourceEntry, path: Path | None = None) -> Path:
    """Append a row, writing the header if the file is new."""
    config.ensure_dirs()
    path = path or config.RESOURCE_LIST_PATH
    is_new = not path.is_file() or path.stat().st_size == 0

    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_fieldnames())
        if is_new:
            writer.writeheader()
        writer.writerow(asdict(entry))

    logger.info("Re-source list: %s — %s (%s)", entry.action, entry.title, entry.cutoff_khz)
    return path


def read_all(path: Path | None = None) -> list[ReSourceEntry]:
    path = path or config.RESOURCE_LIST_PATH
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            ReSourceEntry(**{k: (row.get(k) or "") for k in _fieldnames()})
            for row in csv.DictReader(fh)
        ]


def outstanding(path: Path | None = None) -> list[ReSourceEntry]:
    """Entries not yet marked as resolved — the actual shopping list."""
    return [e for e in read_all(path) if not e.resolved.strip()]
