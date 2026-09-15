"""Deleting files, recoverably.

Deleting a track is the only thing KRATR does that destroys something outside its own
data, so it goes to the **Recycle Bin** rather than being unlinked. It is still gone
from the library and from the folder, but a mistake at 2am is retrievable.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040          # the bit that makes it recoverable
_FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


@dataclass(slots=True)
class DeleteResult:
    recycled: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        parts = [f"{len(self.recycled)} file(s) moved to the Recycle Bin"]
        if self.failed:
            parts.append(f"{len(self.failed)} could not be deleted")
        return "; ".join(parts)


def _recycle_windows(paths: list[Path]) -> None:
    """Hand the files to the shell so they land in the Recycle Bin."""
    # The API takes a double-null-terminated list of null-separated paths.
    joined = "\0".join(str(p) for p in paths) + "\0\0"

    operation = _SHFILEOPSTRUCTW(
        hwnd=None,
        wFunc=_FO_DELETE,
        pFrom=joined,
        pTo=None,
        fFlags=_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI,
        fAnyOperationsAborted=False,
        hNameMappings=None,
        lpszProgressTitle=None,
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        raise OSError(f"SHFileOperation failed with code {result}")
    if operation.fAnyOperationsAborted:
        raise OSError("the delete was aborted")


def delete(paths: list[Path], *, permanent: bool = False) -> DeleteResult:
    """Delete files, preferring the Recycle Bin so the action can be undone."""
    result = DeleteResult()
    existing = [Path(p) for p in paths if Path(p).is_file()]
    if not existing:
        return result

    if not permanent and sys.platform == "win32":
        try:
            _recycle_windows(existing)
            result.recycled.extend(existing)
            logger.info("Recycled %d file(s)", len(existing))
            return result
        except (OSError, AttributeError) as exc:
            # Never silently fall through to an unrecoverable delete.
            logger.warning("Recycle Bin unavailable (%s); nothing deleted", exc)
            result.failed.extend((p, str(exc)) for p in existing)
            return result

    for path in existing:
        try:
            os.remove(path)
            result.recycled.append(path)
        except OSError as exc:
            result.failed.append((path, str(exc)))
    return result
