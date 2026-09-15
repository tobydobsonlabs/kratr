"""Track colour — the final step of the pipeline, and freely relabellable.

``ColorID`` is used on **0 of 3,328 tracks**, so the whole channel is free. It is also
the only attribute readable at a glance on a CDJ without opening a menu, which makes
it the best home for whatever single dimension you most want to eyeball mid-set.

Labels live in ``DjmdColor.Commnt`` — currently rekordbox's stock ``Pink``, ``Red``,
``Orange``, ``Yellow``, ``Green``, ``Aqua``, ``Blue``, ``Purple``. Renaming one is a
field update, and because tracks reference ``ColorID`` rather than the label, renaming
colour 3 from "Energy 3" to "Peak" reinterprets every track already set to it without
touching a single track row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: rekordbox's eight colours, in its own order. The swatches are for KRATR's UI —
#: rekordbox stores no RGB value, only the ID and an editable label.
SWATCHES: dict[str, str] = {
    "1": "#FF6EC7",  # Pink
    "2": "#E63946",  # Red
    "3": "#F4842C",  # Orange
    "4": "#F2D544",  # Yellow
    "5": "#4CAF50",  # Green
    "6": "#2BC4C4",  # Aqua
    "7": "#3B82F6",  # Blue
    "8": "#9B5DE5",  # Purple
}

STOCK_NAMES = {"Pink", "Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple"}

#: rekordbox spells "no colour" several ways — NULL, an empty string, and the string
#: "0". Treating any of them as a real colour has caused two separate bugs: the whole
#: library reporting as already sorted, and the import wizard crashing on a colour ID
#: that has no button. One place decides what "unset" means.
_UNSET = (None, "", "0", 0)


def normalise_colour_id(value: object) -> str | None:
    """The colour ID as KRATR uses it, or None when no colour is set."""
    return None if value in _UNSET else str(value)


@dataclass(slots=True)
class Colour:
    id: str
    name: str
    sort_key: int
    track_count: int = 0

    @property
    def swatch(self) -> str:
        return SWATCHES.get(self.id, "#888888")

    @property
    def is_stock_name(self) -> bool:
        return self.name in STOCK_NAMES


class ColourError(RuntimeError):
    pass


def colours(db: Any) -> list[Colour]:
    """The eight colours with their current labels and usage counts."""
    from pyrekordbox.db6 import tables

    counts: dict[str, int] = {}
    for row in db.query(tables.DjmdContent.ColorID).all():
        key = normalise_colour_id(row[0])
        if key is not None:
            counts[key] = counts.get(key, 0) + 1

    result = [
        Colour(
            id=str(c.ID),
            name=c.Commnt or "",
            sort_key=c.SortKey or 0,
            track_count=counts.get(str(c.ID), 0),
        )
        for c in db.query(tables.DjmdColor).all()
    ]
    result.sort(key=lambda c: c.sort_key)
    return result


def rename_colour(db: Any, colour_id: str, name: str) -> Any:
    """Relabel a colour. Existing assignments are untouched by design."""
    from pyrekordbox.db6 import tables

    cleaned = name.strip()
    if not cleaned:
        raise ColourError("A colour needs a label")

    row = db.query(tables.DjmdColor).filter_by(ID=str(colour_id)).first()
    if row is None:
        raise ColourError(f"No colour with ID {colour_id}")

    row.Commnt = cleaned
    db.flush()
    logger.info("Renamed colour %s → %r", colour_id, cleaned)
    return row


def set_track_colour(db: Any, content_id: str, colour_id: str | None) -> Any:
    """Set (or clear) a track's colour. One colour per track — it is single-valued."""
    from pyrekordbox.db6 import tables

    content = db.query(tables.DjmdContent).filter_by(ID=str(content_id)).first()
    if content is None:
        raise ColourError(f"No track with ID {content_id}")

    if colour_id is None:
        content.ColorID = None
    else:
        if db.query(tables.DjmdColor).filter_by(ID=str(colour_id)).first() is None:
            raise ColourError(f"No colour with ID {colour_id}")
        content.ColorID = str(colour_id)

    db.flush()
    return content


def track_colour(db: Any, content_id: str) -> str | None:
    from pyrekordbox.db6 import tables

    content = db.query(tables.DjmdContent).filter_by(ID=str(content_id)).first()
    if content is None:
        raise ColourError(f"No track with ID {content_id}")
    return normalise_colour_id(content.ColorID)


def tracks_with_colour(db: Any, colour_id: str) -> list[str]:
    from pyrekordbox.db6 import tables

    return [
        str(r.ID)
        for r in db.query(tables.DjmdContent.ID).filter(
            tables.DjmdContent.ColorID == str(colour_id)
        )
    ]
