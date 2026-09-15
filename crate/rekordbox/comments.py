"""Mirror tags into the comment field — the one genuinely unlimited tag channel.

MyTag caps at four categories. ``DjmdContent.Commnt`` is free text, and **CDJ search
covers the comment field**, so writing ``/house /hazy /sunset /vocal`` gives an
unbounded, searchable set of tags on the player.

The ``/`` prefix earns its keep: searching ``hazy`` would also match track titles,
whereas ``/hazy`` only matches a tag.

This is the **only destructive operation in KRATR** — 837 tracks already have
comments, overwhelmingly ripper and promo junk (``DEEPDJ.ORG``,
``ExactAudioCopy v0.99pb4``, ``=-TechnoRulez-=``), but a handful are genuine label
notes. So every original is written to an undo log before being overwritten, and
:func:`restore_all` puts them back byte-for-byte.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import config

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "/"


@dataclass(slots=True)
class CommentBackup:
    content_id: str
    original: str
    path: str
    timestamp: str


def slugify(tag_name: str) -> str:
    """``Deep House`` → ``deep-house``, so a token is one searchable word."""
    slug = re.sub(r"[^\w\s-]", "", tag_name.strip().casefold())
    return re.sub(r"[\s_]+", "-", slug).strip("-")


def build_comment(
    tokens: list[str], *, prefix: str = DEFAULT_PREFIX, keep: str | None = None
) -> str:
    """Render tag tokens as a comment string, optionally preserving existing text."""
    # Deduplicate on the *slug*, so "Hazy", "hazy" and "HAZY" collapse to one token.
    slugs = [s for s in (slugify(t) for t in tokens) if s]
    rendered = " ".join(f"{prefix}{s}" for s in dict.fromkeys(slugs))
    if keep:
        kept = strip_tokens(keep, prefix=prefix).strip()
        if kept:
            return f"{kept} {rendered}".strip()
    return rendered


def strip_tokens(comment: str, *, prefix: str = DEFAULT_PREFIX) -> str:
    """Remove KRATR's tokens, leaving whatever text was there before."""
    pattern = rf"(?:(?<=\s)|^){re.escape(prefix)}[\w-]+"
    return re.sub(r"\s{2,}", " ", re.sub(pattern, "", comment or "")).strip()


def parse_tokens(comment: str, *, prefix: str = DEFAULT_PREFIX) -> list[str]:
    """The tokens currently written into a comment."""
    return re.findall(rf"(?:(?<=\s)|^){re.escape(prefix)}([\w-]+)", comment or "")


# --------------------------------------------------------------------- undo log
def log_original(content_id: str, original: str, path: str, log: Path | None = None) -> None:
    """Record a comment before it is overwritten. Never skipped."""
    config.ensure_dirs()
    log = log or config.COMMENT_BACKUP_PATH
    entry = CommentBackup(
        content_id=str(content_id),
        original=original or "",
        path=path or "",
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    with log.open("a", encoding="utf-8") as fh:
        # asdict(), not __dict__ — slots=True dataclasses have no instance dict.
        fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def read_log(log: Path | None = None) -> list[CommentBackup]:
    log = log or config.COMMENT_BACKUP_PATH
    if not log.is_file():
        return []
    entries = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(CommentBackup(**json.loads(line)))
    return entries


def original_for(content_id: str, log: Path | None = None) -> str | None:
    """The *first* recorded comment for a track — the true original."""
    for entry in read_log(log):
        if entry.content_id == str(content_id):
            return entry.original
    return None


def restore_all(db: Any, log: Path | None = None) -> int:
    """Put every logged comment back. The escape hatch for the one destructive op."""
    from pyrekordbox.db6 import tables

    restored = 0
    seen: set[str] = set()
    for entry in read_log(log):
        if entry.content_id in seen:
            continue  # only the first entry is the true original
        seen.add(entry.content_id)
        content = db.query(tables.DjmdContent).filter_by(ID=entry.content_id).first()
        if content is not None:
            content.Commnt = entry.original
            restored += 1
    db.flush()
    logger.warning("Restored %d original comment(s)", restored)
    return restored


# ----------------------------------------------------------------------- writing
def set_track_comment(
    db: Any,
    content_id: str,
    tokens: list[str],
    *,
    prefix: str = DEFAULT_PREFIX,
    preserve_existing: bool = False,
    log: Path | None = None,
) -> str:
    """Write the tag mirror onto a track, backing up whatever was there first."""
    from pyrekordbox.db6 import tables

    content = db.query(tables.DjmdContent).filter_by(ID=str(content_id)).first()
    if content is None:
        raise ValueError(f"No track with ID {content_id}")

    existing = content.Commnt or ""
    # Only log the first time we touch a track, so the log holds true originals.
    if existing and original_for(content_id, log) is None:
        log_original(content_id, existing, content.FolderPath or "", log)

    new_comment = build_comment(
        tokens, prefix=prefix, keep=existing if preserve_existing else None
    )
    content.Commnt = new_comment
    db.flush()
    return new_comment


def sync_from_tags(
    db: Any,
    content_id: str,
    tag_names: list[str],
    extra_tokens: list[str] | None = None,
    *,
    prefix: str = DEFAULT_PREFIX,
    preserve_existing: bool = False,
    log: Path | None = None,
) -> str:
    """Regenerate a track's comment from its tags, so the mirror never drifts.

    ``extra_tokens`` carries non-tag markers such as ``lowq`` for a track filed
    despite being flagged as an upscale.
    """
    tokens = [*tag_names, *(extra_tokens or [])]
    return set_track_comment(
        db, content_id, tokens, prefix=prefix, preserve_existing=preserve_existing, log=log
    )
