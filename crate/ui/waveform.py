"""Waveform peaks and the clickable waveform view.

Peaks are computed by decoding the file to mono PCM and reducing it to min/max pairs
per horizontal pixel — the standard approach, and cheap enough to cache per file.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import config
from ..pipeline import ffmpeg

logger = logging.getLogger(__name__)

#: Peak buckets per file. Enough detail for a wide window without being wasteful.
PEAK_COUNT = 2000
#: Peaks are computed from a downsampled decode; 8 kHz is plenty for an envelope.
PEAK_SAMPLE_RATE = 8_000


def cache_path(path: Path) -> Path:
    stat = path.stat()
    key = f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{PEAK_COUNT}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return config.CACHE_DIR / "waveforms" / f"{digest}.json"


def compute_peaks(path: Path, buckets: int = PEAK_COUNT) -> list[float]:
    """Amplitude envelope, one value per bucket, normalised to 0..1."""
    raw = ffmpeg.decode_pcm(path, sample_rate=PEAK_SAMPLE_RATE, channels=1)
    samples = np.frombuffer(raw, dtype=np.float32)
    if samples.size == 0:
        return []

    # Trim to a whole number of buckets so the reshape is exact.
    per_bucket = max(1, samples.size // buckets)
    usable = per_bucket * min(buckets, samples.size // per_bucket)
    if usable == 0:
        return []
    blocks = samples[:usable].reshape(-1, per_bucket)

    # Peak amplitude per block reads better than RMS for finding drops and builds.
    peaks = np.max(np.abs(blocks), axis=1)
    ceiling = float(np.max(peaks)) or 1.0
    return (peaks / ceiling).astype(float).tolist()


def load_or_compute(path: Path) -> list[float]:
    cached = cache_path(path)
    if cached.is_file():
        try:
            return json.loads(cached.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.debug("Waveform cache unreadable for %s", path.name)

    peaks = compute_peaks(path)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(peaks), encoding="utf-8")
    return peaks


class WaveformSignals(QObject):
    ready = Signal(object, object)   # Path, list[float]
    failed = Signal(object, str)


class WaveformTask(QRunnable):
    """Decoding a long track takes seconds, so it never runs on the GUI thread."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.signals = WaveformSignals()

    def run(self) -> None:  # noqa: D102
        try:
            self.signals.ready.emit(self.path, load_or_compute(self.path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Waveform failed for %s: %s", self.path.name, exc)
            self.signals.failed.emit(self.path, str(exc))


class WaveformView(QWidget):
    """Draws the envelope, the playhead, and turns clicks into seeks."""

    seek_requested = Signal(float)   # 0..1 position

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("waveform")

        self._peaks: list[float] = []
        self._position = 0.0
        self._hover: float | None = None
        self._message = "No track loaded"
        self.setMouseTracking(True)

    # ------------------------------------------------------------------ state
    def set_peaks(self, peaks: list[float]) -> None:
        self._peaks = peaks
        self._message = "" if peaks else "Waveform unavailable"
        self.update()

    def clear(self, message: str = "No track loaded") -> None:
        self._peaks = []
        self._position = 0.0
        self._message = message
        self.update()

    def set_position(self, fraction: float) -> None:
        fraction = min(1.0, max(0.0, fraction))
        # Only repaint when the playhead actually moves a pixel.
        if abs(fraction - self._position) * max(1, self.width()) >= 1.0:
            self._position = fraction
            self.update()

    # ----------------------------------------------------------------- events
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.LeftButton and self._peaks:
            self.seek_requested.emit(self._fraction_at(event.position().x()))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._hover = self._fraction_at(event.position().x()) if self._peaks else None
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def _fraction_at(self, x: float) -> float:
        return min(1.0, max(0.0, x / max(1.0, self.width())))

    # ---------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        width, height = self.width(), self.height()
        painter.fillRect(self.rect(), QColor("#050505"))

        if not self._peaks:
            painter.setPen(QColor("#5a5a5a"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            painter.end()
            return

        middle = height / 2
        played_to = int(self._position * width)

        played = QLinearGradient(0, 0, 0, height)
        played.setColorAt(0.0, QColor("#b8b8b8"))
        played.setColorAt(0.5, QColor("#ffffff"))
        played.setColorAt(1.0, QColor("#b8b8b8"))

        count = len(self._peaks)
        for x in range(width):
            # Map pixel → peak bucket, taking the loudest in range so nothing is lost
            # when the widget is narrower than the bucket count.
            start = int(x * count / width)
            end = max(start + 1, int((x + 1) * count / width))
            amplitude = max(self._peaks[start:end], default=0.0)

            bar = max(1.0, amplitude * (height - 6) / 2)
            if x <= played_to:
                painter.setPen(QPen(played, 1))
            else:
                painter.setPen(QPen(QColor("#303030"), 1))
            painter.drawLine(x, int(middle - bar), x, int(middle + bar))

        if self._hover is not None:
            painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
            hover_x = int(self._hover * width)
            painter.drawLine(hover_x, 0, hover_x, height)

        painter.setPen(QPen(QColor("#ffffff"), 2))
        painter.drawLine(played_to, 0, played_to, height)
        painter.end()
