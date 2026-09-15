"""A proposed starting taxonomy, mined from the folders and playlists already in use.

Offered on first run and **entirely optional** — accept it, edit it, or start blank.
Nothing reaches rekordbox until it is explicitly applied.

The four slots are freed up by two facts that mean tags need not carry everything:
energy lives on the track **colour**, and the CDJ Track Filter already filters by
**BPM and key** natively. That leaves Genre · Vibe · Situation · Format.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .mytags import MAX_TAGS_PER_CATEGORY

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProposedCategory:
    name: str
    tags: list[str] = field(default_factory=list)

    def capped(self) -> "ProposedCategory":
        return ProposedCategory(self.name, self.tags[:MAX_TAGS_PER_CATEGORY])


#: Curated vocabularies. The Genre list is seeded from the library's own folders at
#: runtime; these three are starting points to edit rather than mined guesses.
VIBE_WORDS = [
    "Hazy", "Dreamy", "Atmospheric", "Moody", "Soulful", "Driving", "Party", "Sexy",
    "Uplifting", "Chill", "Groovy", "Dark", "Bright", "Cheesy", "Jazzy", "Spacey",
    "Tribal", "Balearic", "Dramatic", "Raw", "Hypnotic",
]

SITUATION_WORDS = [
    "Intro", "Warm Up", "Groove Build", "Peak Time", "Late", "Breather", "Build Down",
    "Sunset", "Sunrise", "Bar", "Club", "Afterparty", "Lounge", "Dancefloor",
]

FORMAT_WORDS = [
    "Vocal", "Instrumental", "Acapella", "Edit", "Extended", "Bootleg", "Dub",
    "Intro-less", "Remix", "Original",
]

#: Common DJ shorthand used in playlist names. Expanding these is a guess at intent,
#: so they are proposed rather than assumed — worth confirming before committing. Add
#: your own shorthand here if your playlists use codes of their own.
ABBREVIATIONS: dict[str, str] = {
    "WU": "Warm Up",
    "GB": "Groove Build",
    "PK": "Peak Time",
    "LTE": "Late",
    "DF": "Dancefloor",
    "AP": "Afterparty",
    "IB": "Intro Build",
    "SIT": "Sitting",
    "STD": "Standing",
    "DNC": "Dancing",
    "AMB": "Ambient",
}

#: Playlist names that describe a gig, venue or set rather than a musical quality.
#: Turning these into tags would just duplicate the playlists. These are generic
#: examples — venue- and night-specific names vary per DJ, so add your own if a set
#: playlist keeps showing up as a suggested tag.
_NOT_TAGS = re.compile(
    r"^(cue analysis|podcasts?|club|bar|specials|closing tracks|random)",
    re.IGNORECASE,
)

_STOPWORDS = {"and", "the", "misc", "new", "old", "mix", "pod", "b2b"}


def _titlecase(word: str) -> str:
    return word if word.isupper() and len(word) <= 3 else word.title()


def genres_from_folders(folders: list[str], subfolders: dict[str, list[str]] | None = None) -> list[str]:
    """Turn the on-disk genre tree into genre tags.

    Compound folder names like ``Nu Disco - Italo - 80s Synths`` and
    ``Dub - Reggae - Lovers Rock`` are split, because each part is a genre a track
    might legitimately be tagged with on its own.
    """
    names: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        cleaned = value.strip(" -+")
        if not cleaned or cleaned.casefold() in _STOPWORDS:
            return
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            names.append(cleaned)

    for folder in folders:
        if folder.startswith("_"):  # quarantine and other housekeeping folders
            continue
        for part in re.split(r"\s*[-+]\s*", folder):
            add(part)

    for subs in (subfolders or {}).values():
        for sub in subs:
            for part in re.split(r"\s*[-+,]\s*", sub):
                add(part)

    return names


def _mine_words(playlist_names: list[str], vocabulary: list[str]) -> list[str]:
    """Vocabulary entries that actually appear in the playlist names."""
    haystack = " | ".join(playlist_names).casefold()
    return [word for word in vocabulary if word.casefold() in haystack]


def expand_abbreviations(playlist_names: list[str]) -> list[str]:
    """Expansions for the abbreviations genuinely used in the playlist names."""
    tokens: set[str] = set()
    for name in playlist_names:
        tokens.update(re.findall(r"\b[A-Z]{2,4}\b", name))
    return [ABBREVIATIONS[t] for t in sorted(tokens) if t in ABBREVIATIONS]


def propose(
    genre_folders: list[str],
    playlist_names: list[str] | None = None,
    subfolders: dict[str, list[str]] | None = None,
) -> list[ProposedCategory]:
    """Build the four proposed categories."""
    playlist_names = playlist_names or []

    genres = genres_from_folders(genre_folders, subfolders)

    # Prefer vocabulary the library already demonstrably uses, then fill from the
    # curated list so the starting point is usable rather than sparse.
    def ordered(vocabulary: list[str], extra: list[str] | None = None) -> list[str]:
        used = _mine_words(playlist_names, vocabulary)
        rest = [w for w in vocabulary if w not in used]
        out: list[str] = []
        for word in [*used, *(extra or []), *rest]:
            if word not in out:
                out.append(word)
        return out

    # "Sub-Genre" and "Setting" rather than rekordbox's own "Genre" and "Situation".
    # Partly accuracy — what gets tagged here really is sub-genre, not genre — and
    # partly caution: a category left *unedited* is reported to vanish from USB
    # export, and since nobody knows whether rekordbox detects that by an edit flag
    # or by comparing against the stock name, a distinct name removes the question
    # for free. Any of these can be renamed to whatever you like in the Tag Manager.
    return [
        ProposedCategory("Sub-Genre", genres).capped(),
        ProposedCategory("Vibe", ordered(VIBE_WORDS)).capped(),
        ProposedCategory(
            "Setting", ordered(SITUATION_WORDS, expand_abbreviations(playlist_names))
        ).capped(),
        ProposedCategory("Format", ordered(FORMAT_WORDS)).capped(),
    ]


def apply_proposal(db, proposal: list[ProposedCategory], *, clear_existing: bool = True) -> dict[str, int]:
    """Write a proposal into the four MyTag slots.

    Every slot gets an explicit name, including any left empty — a category still
    carrying a rekordbox default name risks being dropped from USB export.
    """
    from . import mytags

    categories = mytags.categories(db)
    if len(proposal) > len(categories):
        raise mytags.MyTagError(
            f"rekordbox has {len(categories)} category slots; {len(proposal)} were proposed. "
            f"There is no way to add a fifth."
        )

    created = 0
    for slot, proposed in zip(categories, proposal, strict=False):
        if clear_existing:
            mytags.clear_category(db, slot.id, placeholder=proposed.name)
        mytags.rename_category(db, slot.id, proposed.name)
        for tag_name in proposed.tags:
            if mytags.find_tag(db, tag_name, slot.id) is None:
                mytags.create_tag(db, slot.id, tag_name)
                created += 1

    # Any slot the proposal didn't cover still must not keep a default name.
    for index, slot in enumerate(categories[len(proposal):], start=len(proposal) + 1):
        refreshed = mytags.categories_by_id(db)[slot.id]
        if refreshed.is_default_name:
            mytags.rename_category(db, slot.id, f"Unused {index}")

    db.flush()

    # Not blocked — the name is the user's choice — but worth saying out loud.
    for slot in mytags.categories(db):
        if slot.is_default_name:
            logger.warning(
                "Category %r matches a rekordbox stock name. Categories left unedited "
                "have been reported to disappear from USB export; a distinct name "
                "avoids the question.",
                slot.name,
            )

    logger.info("Applied taxonomy: %d tag(s) created", created)
    return {"categories": len(proposal), "tags_created": created}
