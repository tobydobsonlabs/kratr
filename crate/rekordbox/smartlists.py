"""Auto-generated smart playlists — the practical fix for mid-set filtering.

The CDJ Track Filter is a menu-dive. Intelligent playlists auto-populate from
criteria **and export to USB as ordinary browsable playlists**, so instead of tapping
through a filter for "sunset + vocal" you browse to a list called ``Sunset Vocal``.

They also update themselves, which removes most of the manual playlist-picking step:
tag a track and it appears in every matching list.

Everything generated lives in one dedicated folder. **The 197 existing manual
playlists are never touched** — gig sets stay exactly as they are.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: All generated playlists live here, so they can never be confused with, or
#: accidentally overwrite, a hand-built set.
FOLDER_NAME = "KRATR · Auto"
LEGACY_FOLDER_NAME = "Crate · Auto"


@dataclass(slots=True)
class ComboRule:
    """A user-defined combination, e.g. 'Sunset Vocal' = Sunset AND Vocal."""

    name: str
    tag_ids: list[str] = field(default_factory=list)
    match_all: bool = True


def _smart_list_for(tag_ids: list[str], match_all: bool = True):
    from pyrekordbox.db6.smartlist import LogicalOperator, Operator, Property, SmartList

    smart = SmartList(
        logical_operator=LogicalOperator.ALL if match_all else LogicalOperator.ANY,
        auto_update=1,
    )
    for tag_id in tag_ids:
        # MYTAG accepts only CONTAINS / NOT_CONTAINS — rekordbox stores a track's tags
        # as a combined value, so membership is a substring test rather than equality.
        smart.add_condition(Property.MYTAG, operator=Operator.CONTAINS, value_left=str(tag_id))
    return smart


def get_folder(db: Any, name: str = FOLDER_NAME) -> Any | None:
    from pyrekordbox.db6 import tables

    return (
        db.query(tables.DjmdPlaylist)
        .filter(tables.DjmdPlaylist.Name == name, tables.DjmdPlaylist.Attribute == 1)
        .first()
    )


def ensure_folder(db: Any, name: str = FOLDER_NAME) -> Any:
    folder = get_folder(db, name)
    if folder is None and name == FOLDER_NAME:
        folder = get_folder(db, LEGACY_FOLDER_NAME)
        if folder is not None:
            folder.Name = FOLDER_NAME
            db.flush()
            logger.info("Renamed smart-playlist folder %r to %r", LEGACY_FOLDER_NAME, FOLDER_NAME)
    if folder is None:
        folder = db.create_playlist_folder(name)
        db.flush()
        logger.info("Created smart-playlist folder %r", name)
    return folder


def existing_in_folder(db: Any, folder: Any) -> dict[str, Any]:
    """Generated playlists, keyed by name."""
    from pyrekordbox.db6 import tables

    rows = (
        db.query(tables.DjmdPlaylist)
        .filter(tables.DjmdPlaylist.ParentID == str(folder.ID))
        .all()
    )
    return {r.Name: r for r in rows}


def create_for_tag(db: Any, tag_id: str, tag_name: str, folder: Any) -> Any:
    """One auto-updating playlist for a single tag."""
    return db.create_smart_playlist(tag_name, _smart_list_for([tag_id]), parent=folder)


def create_for_combo(db: Any, rule: ComboRule, folder: Any) -> Any:
    return db.create_smart_playlist(
        rule.name, _smart_list_for(rule.tag_ids, rule.match_all), parent=folder
    )


def sync(
    db: Any,
    tags: list[Any],
    combos: list[ComboRule] | None = None,
    *,
    folder_name: str = FOLDER_NAME,
    prune: bool = True,
) -> dict[str, int]:
    """Bring the generated folder in line with the current taxonomy.

    Idempotent: running it twice creates nothing the second time. Only playlists
    *inside the generated folder* are ever removed, so a hand-built list can never be
    caught by the pruning.
    """
    combos = combos or []
    folder = ensure_folder(db, folder_name)
    existing = existing_in_folder(db, folder)

    wanted = {t.name for t in tags} | {c.name for c in combos}
    created = 0

    for tag in tags:
        if tag.name not in existing:
            create_for_tag(db, tag.id, tag.name, folder)
            created += 1

    for combo in combos:
        if combo.name not in existing:
            create_for_combo(db, combo, folder)
            created += 1

    removed = 0
    if prune:
        for name, playlist in existing.items():
            if name not in wanted:
                db.delete_playlist(playlist)
                removed += 1

    db.flush()
    logger.info(
        "Smart playlists synced: %d created, %d removed, %d total",
        created, removed, len(wanted),
    )
    return {"created": created, "removed": removed, "total": len(wanted)}
