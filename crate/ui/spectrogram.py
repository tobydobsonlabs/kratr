"""Spectrogram view — the Spek replacement, with the measured cutoff drawn on.

Showing the verdict *on* the image is the point: the number and the picture agree,
so the automatic reading is checkable at a glance rather than taken on trust.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

from ..models import QualityReport

#: showspectrumpic draws its plot inside a margin; these offsets place the cutoff
#: line on the plot area rather than the legend.
_LEFT_MARGIN = 55
_RIGHT_MARGIN = 50
_TOP_MARGIN = 20
_BOTTOM_MARGIN = 40


class SpectrogramView(QLabel):
    """Displays a rendered spectrogram, scaled to fit, with a cutoff marker."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setObjectName("spectrogram")
        self._source: QPixmap | None = None
        self.clear_view("Drop a track to see its spectrum")

    def clear_view(self, message: str = "") -> None:
        self._source = None
        self.setText(message)

    def show_report(self, report: QualityReport, sample_rate: int | None) -> None:
        path = report.spectrogram_path
        if not path or not Path(path).is_file():
            self.clear_view("No spectrogram available")
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.clear_view("Could not load spectrogram")
            return

        if report.cutoff_hz and sample_rate:
            pixmap = self._draw_cutoff(pixmap, report.cutoff_hz, sample_rate / 2.0)

        self._source = pixmap
        self._rescale()

    def _draw_cutoff(self, pixmap: QPixmap, cutoff_hz: float, nyquist: float) -> QPixmap:
        """Overlay a line at the detected cutoff frequency."""
        if nyquist <= 0:
            return pixmap

        annotated = QPixmap(pixmap)
        painter = QPainter(annotated)
        try:
            plot_top = _TOP_MARGIN
            plot_bottom = annotated.height() - _BOTTOM_MARGIN
            plot_height = plot_bottom - plot_top
            if plot_height <= 0:
                return pixmap

            # Frequency runs bottom (0 Hz) to top (Nyquist).
            ratio = min(1.0, cutoff_hz / nyquist)
            y = plot_bottom - int(ratio * plot_height)

            pen = QPen(QColor(255, 255, 255))
            pen.setWidth(2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(_LEFT_MARGIN, y, annotated.width() - _RIGHT_MARGIN, y)

            painter.setPen(QColor(255, 255, 255))
            painter.drawText(_LEFT_MARGIN + 6, max(plot_top + 12, y - 5),
                             f"{cutoff_hz / 1000:.1f} kHz")
        finally:
            painter.end()
        return annotated

    def _rescale(self) -> None:
        if self._source is None:
            return
        self.setPixmap(
            self._source.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._rescale()
