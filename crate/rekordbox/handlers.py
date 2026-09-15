"""Wiring the op queue to the rekordbox engines.

Operations are keyed by **file path** rather than content ID, because a track's ID
doesn't exist until its import op runs. Ops apply in the order they were queued, so
an import always precedes the playlist, tag and colour ops for the same track.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import SourceInfo
from . import colours, comments, folders, importer, mytags, playlists, taxonomy
from .queue import Op, OpKind

logger = logging.getLogger(__name__)


def _content(db: Any, op: Op) -> Any:
    path = op.payload.get("path")
    content = importer.find_by_path(db, path) if path else None
    if content is None:
        raise ValueError(f"No collection entry for {path}")
    return content


def _info_from(payload: dict[str, Any]) -> SourceInfo | None:
    """Rebuild just enough of a SourceInfo to populate a row."""
    from pathlib import Path

    if "tags" not in payload:
        return None
    return SourceInfo(
        path=Path(payload["path"]),
        codec=payload.get("codec", ""),
        container=payload.get("container", ""),
        fmt=None,
        bitrate=payload.get("bitrate"),
        sample_rate=payload.get("sample_rate"),
        bit_depth=payload.get("bit_depth"),
        channels=payload.get("channels"),
        duration=payload.get("duration"),
        file_size=payload.get("file_size", 0),
        tags=payload.get("tags", {}),
    )


# ------------------------------------------------------------------- handlers
def handle_add_content(session: Any, op: Op) -> None:
    importer.import_track(session.db, op.payload["path"], _info_from(op.payload))


def handle_relocate(session: Any, op: Op) -> None:
    db = session.db
    content = db.get_content(ID=str(op.payload["content_id"]))
    if content is None:
        raise ValueError(f"No track with ID {op.payload['content_id']}")
    importer.relocate(db, content, op.payload["path"], _info_from(op.payload))


def handle_add_to_playlist(session: Any, op: Op) -> None:
    importer.add_to_playlists(session.db, _content(session.db, op), op.payload["playlist_ids"])


#: A tag created in the UI has no database ID until its create op is applied, so it
#: carries "pending:<category_id>:<name>" until then.
PENDING_PREFIX = "pending:"


def _resolve_tag_ids(db: Any, tag_ids: list[str]) -> list[str]:
    """Turn provisional tag IDs into real ones.

    Safe because ops apply in the order they were queued, so the ``create_tag`` for a
    provisional tag always runs before the ``set_mytags`` that references it.
    """
    resolved: list[str] = []
    for tag_id in tag_ids:
        if not tag_id.startswith(PENDING_PREFIX):
            resolved.append(tag_id)
            continue

        _, category_id, name = tag_id.split(":", 2)
        tag = mytags.find_tag(db, name, category_id)
        if tag is None:
            raise ValueError(
                f"Tag {name!r} was never created, so it cannot be applied. "
                f"Its create step may have failed earlier in this batch."
            )
        resolved.append(tag.id)
    return resolved


def handle_set_mytags(session: Any, op: Op) -> None:
    content = _content(session.db, op)
    tag_ids = _resolve_tag_ids(session.db, op.payload["tag_ids"])
    mytags.set_track_tags(session.db, str(content.ID), tag_ids)


def handle_set_colour(session: Any, op: Op) -> None:
    content = _content(session.db, op)
    colours.set_track_colour(session.db, str(content.ID), op.payload.get("colour_id"))


def handle_set_comment(session: Any, op: Op) -> None:
    content = _content(session.db, op)
    comments.sync_from_tags(
        session.db,
        str(content.ID),
        op.payload.get("tag_names", []),
        op.payload.get("extra_tokens", []),
        prefix=op.payload.get("prefix", comments.DEFAULT_PREFIX),
        preserve_existing=op.payload.get("preserve_existing", False),
    )


def handle_tag_manager(session: Any, op: Op) -> None:
    """Apply one taxonomy edit collected by the Tag Manager."""
    db = session.db
    action = op.payload["action"]
    args = op.payload.get("args", {})

    if action == "rename_category":
        mytags.rename_category(db, args["category_id"], args["name"])
    elif action == "clear_category":
        mytags.clear_category(db, args["category_id"])
    elif action == "reorder_categories":
        mytags.reorder_categories(db, args["ordered_ids"])
    elif action == "create_tag":
        mytags.create_tag(db, args["category_id"], args["name"])
    elif action == "rename_tag":
        mytags.rename_tag(db, args["tag_id"], args["name"])
    elif action == "delete_tag":
        mytags.delete_tag(db, args["tag_id"])
    elif action == "move_tag":
        mytags.move_tag(db, args["tag_id"], args["category_id"])
    elif action == "merge_tags":
        mytags.merge_tags(db, args["source_id"], args["target_id"])
    elif action == "reorder_tags":
        # Resolved because a category can contain tags created in this same batch,
        # which have no database ID until their create op runs.
        mytags.reorder_tags(db, args["category_id"], _resolve_tag_ids(db, args["ordered_ids"]))
    elif action == "rename_colour":
        colours.rename_colour(db, args["colour_id"], args["name"])
    elif action == "apply_taxonomy":
        proposal = [
            taxonomy.ProposedCategory(c["name"], c["tags"]) for c in args["categories"]
        ]
        taxonomy.apply_proposal(db, proposal, clear_existing=args.get("clear_existing", True))
    else:
        raise ValueError(f"Unknown taxonomy action: {action!r}")


def handle_library_manager(session: Any, op: Op) -> None:
    """Apply folder and playlist-tree edits through stable IDs and in-place paths."""
    db = session.db
    action = op.payload["action"]
    args = op.payload.get("args", {})

    if action == "move_folder":
        folders.move_and_reroute(
            db, args["old_path"], args["new_path"], args["music_root"]
        )
        session.add_rollback_action(
            lambda old=args["old_path"], new=args["new_path"]: folders.roll_back_move(
                old, new
            )
        )
    elif action == "create_playlist":
        playlists.create(
            db,
            args["name"],
            parent_id=args.get("parent_id"),
            folder=args.get("folder", False),
        )
    elif action == "rename_playlist":
        playlists.rename(db, args["playlist_id"], args["name"])
    elif action == "move_playlist":
        playlists.move(db, args["playlist_id"], args.get("parent_id"))
    elif action == "delete_playlist":
        playlists.delete(db, args["playlist_id"])
    else:
        raise ValueError(f"Unknown library edit: {action!r}")


HANDLERS = {
    OpKind.ADD_CONTENT: handle_add_content,
    OpKind.RELOCATE: handle_relocate,
    OpKind.ADD_TO_PLAYLIST: handle_add_to_playlist,
    OpKind.SET_MYTAGS: handle_set_mytags,
    OpKind.SET_COLOUR: handle_set_colour,
    OpKind.SET_COMMENT: handle_set_comment,
    OpKind.TAG_MANAGER: handle_tag_manager,
    OpKind.LIBRARY_MANAGER: handle_library_manager,
}


def register_all(queue: Any) -> None:
    queue.register_all(HANDLERS)


def payload_for_track(track: Any) -> dict[str, Any]:
    """Serialise what an import op needs from a Track."""
    info = track.info
    path = str(track.final_path or track.source_path)
    payload: dict[str, Any] = {"path": path}
    if info is not None:
        payload.update(
            codec=info.codec,
            container=info.container,
            bitrate=info.bitrate,
            sample_rate=info.sample_rate,
            bit_depth=info.bit_depth,
            channels=info.channels,
            duration=info.duration,
            file_size=info.file_size,
            tags=dict(info.tags),
        )
    return payload
