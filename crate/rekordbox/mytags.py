"""MyTag read/write — the component pyrekordbox lacks entirely.

pyrekordbox exposes ``get_my_tag()`` and ``get_my_tag_songs()`` and nothing else, so
every write here is built directly against the ORM, mirroring the row conventions
pyrekordbox uses for playlists (``database.py:892``).

Row conventions, confirmed by reading the live library:

* **Categories** — ``Attribute=1``, ``ParentID='root'``, ``ID``/``UUID`` = ``'1'``–``'4'``.
* **Tags** — ``Attribute=0``, ``ParentID=<category ID>``, ``ID`` a random 32-bit int
  *as a string*, ``UUID`` a uuid4.
* **Links** — ``DjmdSongMyTag.ID`` and ``.UUID`` are both uuid4 strings.

Two hard limits come from rekordbox, not from here: **four categories** (there is no
fifth, and no way to make one) and **50 tags per category**.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

#: rekordbox hard limits. Not stylistic — the format has no room for more.
MAX_CATEGORIES = 4
MAX_TAGS_PER_CATEGORY = 50

#: rekordbox's stock category names. A category left at its default risks being
#: dropped from USB export while still looking fine in rekordbox, so KRATR always
#: writes an explicit name over these.
DEFAULT_CATEGORY_NAMES = {"Genre", "Components", "Situation", "Untitled Column"}

_CATEGORY = 1
_TAG = 0


@dataclass(slots=True)
class Tag:
    id: str
    name: str
    seq: int
    category_id: str
    track_count: int = 0


@dataclass(slots=True)
class Category:
    id: str
    name: str
    seq: int
    tags: list[Tag] = field(default_factory=list)

    @property
    def is_default_name(self) -> bool:
        return self.name in DEFAULT_CATEGORY_NAMES

    @property
    def is_full(self) -> bool:
        return len(self.tags) >= MAX_TAGS_PER_CATEGORY

    @property
    def remaining(self) -> int:
        return MAX_TAGS_PER_CATEGORY - len(self.tags)


class MyTagError(RuntimeError):
    pass


# --------------------------------------------------------------------- reading
def _rows(db: Any) -> list[Any]:
    from pyrekordbox.db6 import tables

    return db.query(tables.DjmdMyTag).all()


def link_counts(db: Any) -> dict[str, int]:
    """How many tracks carry each tag."""
    from pyrekordbox.db6 import tables

    counts: dict[str, int] = {}
    for row in db.query(tables.DjmdSongMyTag.MyTagID).all():
        key = str(row[0])
        counts[key] = counts.get(key, 0) + 1
    return counts


def categories(db: Any) -> list[Category]:
    """The four category slots and their tags, in rekordbox's order."""
    counts = link_counts(db)
    rows = _rows(db)

    result: list[Category] = []
    for row in rows:
        if row.Attribute == _CATEGORY:
            result.append(Category(id=str(row.ID), name=row.Name or "", seq=row.Seq or 0))
    result.sort(key=lambda c: c.seq)

    by_id = {c.id: c for c in result}
    for row in rows:
        if row.Attribute != _TAG:
            continue
        parent = str(row.ParentID)
        if parent in by_id:
            by_id[parent].tags.append(
                Tag(
                    id=str(row.ID),
                    name=row.Name or "",
                    seq=row.Seq or 0,
                    category_id=parent,
                    track_count=counts.get(str(row.ID), 0),
                )
            )

    for category in result:
        category.tags.sort(key=lambda t: (t.seq, t.name.casefold()))
    return result


def all_tags(db: Any) -> list[Tag]:
    return [tag for category in categories(db) for tag in category.tags]


def find_tag(db: Any, name: str, category_id: str | None = None) -> Tag | None:
    wanted = name.strip().casefold()
    for tag in all_tags(db):
        if tag.name.casefold() == wanted and (category_id is None or tag.category_id == category_id):
            return tag
    return None


def _row(db: Any, tag_id: str) -> Any:
    from pyrekordbox.db6 import tables

    row = db.query(tables.DjmdMyTag).filter_by(ID=str(tag_id)).first()
    if row is None:
        raise MyTagError(f"No MyTag with ID {tag_id}")
    return row


# -------------------------------------------------------------------- categories
def rename_category(db: Any, category_id: str, name: str) -> Any:
    """Rename one of the four slots. This is also how a slot is 'created'."""
    cleaned = name.strip()
    if not cleaned:
        raise MyTagError("A category needs a name")

    row = _row(db, category_id)
    if row.Attribute != _CATEGORY:
        raise MyTagError(f"{category_id} is a tag, not a category")

    row.Name = cleaned
    db.flush()
    logger.info("Renamed category %s → %r", category_id, cleaned)
    return row


def clear_category(db: Any, category_id: str, placeholder: str | None = None) -> int:
    """Empty a slot: delete its tags and every link pointing at them.

    This is what 'delete a category' means — the slot itself cannot be removed,
    because rekordbox has exactly four and no mechanism to add or drop one.
    """
    row = _row(db, category_id)
    if row.Attribute != _CATEGORY:
        raise MyTagError(f"{category_id} is a tag, not a category")

    removed = 0
    for tag in list(categories_by_id(db).get(str(category_id), Category("", "", 0)).tags):
        delete_tag(db, tag.id)
        removed += 1

    # Never leave a slot at a rekordbox default name — see DEFAULT_CATEGORY_NAMES.
    row.Name = placeholder or f"Unused {row.Seq}"
    db.flush()
    logger.info("Cleared category %s (%d tag(s) removed)", category_id, removed)
    return removed


def categories_by_id(db: Any) -> dict[str, Category]:
    return {c.id: c for c in categories(db)}


def reorder_categories(db: Any, ordered_ids: list[str]) -> None:
    """Set the order the four slots appear in, including on the CDJ filter screen."""
    for position, category_id in enumerate(ordered_ids, start=1):
        row = _row(db, category_id)
        if row.Attribute != _CATEGORY:
            raise MyTagError(f"{category_id} is not a category")
        row.Seq = position
    db.flush()


# --------------------------------------------------------------------- tags
def create_tag(db: Any, category_id: str, name: str) -> Any:
    """Add a tag to a category, respecting the 50-per-category ceiling."""
    from pyrekordbox.db6 import tables

    cleaned = name.strip()
    if not cleaned:
        raise MyTagError("A tag needs a name")

    category = categories_by_id(db).get(str(category_id))
    if category is None:
        raise MyTagError(f"No category with ID {category_id}")
    if category.is_full:
        raise MyTagError(
            f"{category.name!r} already has {MAX_TAGS_PER_CATEGORY} tags, which is "
            f"rekordbox's limit per category."
        )
    if any(t.name.casefold() == cleaned.casefold() for t in category.tags):
        raise MyTagError(f"{category.name!r} already has a tag called {cleaned!r}")

    tag_id = db.generate_unused_id(tables.DjmdMyTag)
    next_seq = max((t.seq for t in category.tags), default=0) + 1

    row = tables.DjmdMyTag.create(
        ID=str(tag_id),
        Seq=next_seq,
        Name=cleaned,
        Attribute=_TAG,
        ParentID=str(category_id),
        UUID=str(uuid4()),
    )
    db.add(row)
    db.flush()
    logger.info("Created tag %r in %r (ID %s)", cleaned, category.name, tag_id)
    return row


def rename_tag(db: Any, tag_id: str, name: str) -> Any:
    cleaned = name.strip()
    if not cleaned:
        raise MyTagError("A tag needs a name")
    row = _row(db, tag_id)
    if row.Attribute != _TAG:
        raise MyTagError(f"{tag_id} is a category, not a tag")
    row.Name = cleaned
    db.flush()
    return row


def delete_tag(db: Any, tag_id: str) -> int:
    """Delete a tag and every link to it. Returns how many tracks were affected."""
    from pyrekordbox.db6 import tables

    row = _row(db, tag_id)
    if row.Attribute != _TAG:
        raise MyTagError(f"{tag_id} is a category; use clear_category()")

    links = db.query(tables.DjmdSongMyTag).filter_by(MyTagID=str(tag_id)).all()
    for link in links:
        db.delete(link)
    db.delete(row)
    db.flush()
    logger.info("Deleted tag %s and %d link(s)", tag_id, len(links))
    return len(links)


def move_tag(db: Any, tag_id: str, category_id: str) -> Any:
    """Move a tag to a different category, keeping every track already tagged with it.

    Reorganising the taxonomy must never cost tagging work — only ``ParentID`` moves,
    so the ``djmdSongMyTag`` links are untouched.
    """
    row = _row(db, tag_id)
    if row.Attribute != _TAG:
        raise MyTagError(f"{tag_id} is a category, not a tag")

    target = categories_by_id(db).get(str(category_id))
    if target is None:
        raise MyTagError(f"No category with ID {category_id}")
    if str(row.ParentID) == str(category_id):
        return row
    if target.is_full:
        raise MyTagError(f"{target.name!r} is already at rekordbox's {MAX_TAGS_PER_CATEGORY}-tag limit")

    row.ParentID = str(category_id)
    row.Seq = max((t.seq for t in target.tags), default=0) + 1
    db.flush()
    logger.info("Moved tag %s to category %s", tag_id, category_id)
    return row


def merge_tags(db: Any, source_id: str, target_id: str) -> int:
    """Relink everything from one tag onto another, then delete the source.

    For cleaning up near-duplicates without losing the tracks tagged with either.
    """
    from pyrekordbox.db6 import tables

    if str(source_id) == str(target_id):
        raise MyTagError("Cannot merge a tag into itself")
    _row(db, target_id)

    existing = {
        str(r.ContentID)
        for r in db.query(tables.DjmdSongMyTag).filter_by(MyTagID=str(target_id)).all()
    }

    moved = 0
    for link in db.query(tables.DjmdSongMyTag).filter_by(MyTagID=str(source_id)).all():
        if str(link.ContentID) in existing:
            db.delete(link)  # already tagged with the target; drop the duplicate
        else:
            link.MyTagID = str(target_id)
            moved += 1

    db.flush()
    delete_tag(db, source_id)
    logger.info("Merged tag %s into %s (%d link(s) moved)", source_id, target_id, moved)
    return moved


def reorder_tags(db: Any, category_id: str, ordered_tag_ids: list[str]) -> None:
    for position, tag_id in enumerate(ordered_tag_ids, start=1):
        row = _row(db, tag_id)
        if str(row.ParentID) != str(category_id):
            raise MyTagError(f"Tag {tag_id} is not in category {category_id}")
        row.Seq = position
    db.flush()


# ------------------------------------------------------------------- track links
def track_tag_ids(db: Any, content_id: str) -> list[str]:
    from pyrekordbox.db6 import tables

    return [
        str(r.MyTagID)
        for r in db.query(tables.DjmdSongMyTag).filter_by(ContentID=str(content_id)).all()
    ]


def set_track_tags(db: Any, content_id: str, tag_ids: list[str]) -> tuple[int, int]:
    """Make a track's tags exactly ``tag_ids``. Returns (added, removed)."""
    from pyrekordbox.db6 import tables

    wanted = {str(t) for t in tag_ids}
    current = {str(t) for t in track_tag_ids(db, content_id)}

    to_remove = current - wanted
    to_add = wanted - current

    for tag_id in to_remove:
        link = (
            db.query(tables.DjmdSongMyTag)
            .filter_by(ContentID=str(content_id), MyTagID=tag_id)
            .first()
        )
        if link is not None:
            db.delete(link)

    for tag_id in to_add:
        _row(db, tag_id)  # fail loudly rather than create a dangling link
        existing = db.query(tables.DjmdSongMyTag).filter_by(MyTagID=tag_id).count()
        db.add(
            tables.DjmdSongMyTag.create(
                ID=str(uuid4()),
                MyTagID=str(tag_id),
                ContentID=str(content_id),
                TrackNo=existing + 1,
                UUID=str(uuid4()),
            )
        )

    db.flush()
    return len(to_add), len(to_remove)


def tracks_with_tag(db: Any, tag_id: str) -> list[str]:
    from pyrekordbox.db6 import tables

    return [
        str(r.ContentID)
        for r in db.query(tables.DjmdSongMyTag).filter_by(MyTagID=str(tag_id)).all()
    ]
