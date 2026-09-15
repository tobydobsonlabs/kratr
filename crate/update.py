"""Check GitHub for a newer KRATR release.

Notify-only: this never downloads or installs anything. It asks GitHub's public
Releases API "what's the newest version?", compares it to the running version, and
that's all — the UI decides whether to tell the user. Every failure (offline, rate
limited, bad response) is swallowed and treated as "no update", so a launch is never
blocked or broken by the network.

The only time KRATR touches the internet on its own is this one small GET.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from urllib.error import URLError

from . import __version__

logger = logging.getLogger(__name__)

GITHUB_REPO = "tobydobsonlabs/kratr"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


def _parse_version(tag: str) -> tuple[int, ...]:
    """Turn a tag like ``v1.2.3`` into ``(1, 2, 3)`` for comparison.

    Deliberately forgiving: a missing or non-numeric part becomes 0 rather than raising,
    so a malformed tag can never crash the check.
    """
    cleaned = tag.strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


@dataclass(frozen=True)
class UpdateInfo:
    version: str   # e.g. "0.2.0"
    tag: str       # e.g. "v0.2.0"
    url: str       # the release page to send the user to


def check(current: str = __version__, timeout: float = 6.0) -> UpdateInfo | None:
    """Return info about a newer release, or ``None`` (up to date / offline / any error)."""
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "User-Agent": f"KRATR/{current}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("Update check skipped: %s", exc)
        return None

    tag = (data.get("tag_name") or "").strip()
    # A draft/prerelease or a missing tag is treated as "nothing to offer".
    if not tag or data.get("draft") or data.get("prerelease"):
        return None
    if not is_newer(tag, current):
        return None
    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        url=data.get("html_url") or RELEASES_PAGE,
    )
