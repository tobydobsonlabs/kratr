"""KRATR brand assets shared by the live UI and packaged application."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QIcon, QPixmap


RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
APP_ICON_PATH = RESOURCES_DIR / "kratr-icon-inverted-v1.png"
WORDMARK_PATH = RESOURCES_DIR / "kratr-wordmark-fractured-v1.png"


def application_icon() -> QIcon:
    """Return the crater/record application icon, or a null icon if unavailable."""
    return QIcon(str(APP_ICON_PATH)) if APP_ICON_PATH.is_file() else QIcon()


def wordmark_pixmap() -> QPixmap:
    """Load the wordmark and trim its generated white canvas to the black ink.

    Keeping the full-resolution source in resources makes it useful outside the UI,
    while the small threshold scan here gives the player bar a compact logo plate.
    """
    source = QPixmap(str(WORDMARK_PATH))
    if source.isNull():
        return source

    preview = source.toImage().scaled(
        480,
        220,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    dark: list[tuple[int, int]] = []
    for y in range(preview.height()):
        for x in range(preview.width()):
            colour = preview.pixelColor(x, y)
            if colour.red() < 96 and colour.green() < 96 and colour.blue() < 96:
                dark.append((x, y))

    if not dark:
        return source

    left = min(x for x, _ in dark)
    right = max(x for x, _ in dark)
    top = min(y for _, y in dark)
    bottom = max(y for _, y in dark)
    scale_x = source.width() / preview.width()
    scale_y = source.height() / preview.height()
    padding_x = max(12, int(source.width() * 0.018))
    padding_y = max(12, int(source.height() * 0.035))
    rect = QRect(
        max(0, int(left * scale_x) - padding_x),
        max(0, int(top * scale_y) - padding_y),
        min(source.width(), int((right + 1) * scale_x) + padding_x)
        - max(0, int(left * scale_x) - padding_x),
        min(source.height(), int((bottom + 1) * scale_y) + padding_y)
        - max(0, int(top * scale_y) - padding_y),
    )
    return source.copy(rect)
