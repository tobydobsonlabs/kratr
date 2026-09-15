"""The media player bar, pinned above everything.

You cannot decide a track's genre, vibe or energy without hearing it, so the player
is present on the dashboard and on every wizard step — the same instance throughout,
so moving between steps never interrupts playback.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..models import Track
from ..pipeline import ffmpeg
from .branding import application_icon, wordmark_pixmap
from .waveform import WaveformTask, WaveformView

logger = logging.getLogger(__name__)

#: QtMultimedia on Windows has no AIFF decoder, and AIFF is one of the conversion
#: targets KRATR offers — so those files are transcoded to a temp WAV to play.
_NEEDS_TRANSCODE = {".aiff", ".aif"}


class TransportButton(QPushButton):
    """Play / pause / stop, drawn rather than typed.

    The obvious ``▶`` and ``■`` characters are missing or badly sized in the default
    Windows UI font, so the buttons came out blank. Painting the shapes removes the
    dependency on whatever glyphs happen to be installed.
    """

    def __init__(self, shape: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.shape_name = shape
        self.setFixedSize(42, 42)
        self.setObjectName("transport")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_shape(self, shape: str) -> None:
        if shape != self.shape_name:
            self.shape_name = shape
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e6e7ea"))

        centre = self.rect().center()
        size = 13

        if self.shape_name == "play":
            triangle = QPolygonF([
                QPointF(centre.x() - size * 0.4, centre.y() - size * 0.62),
                QPointF(centre.x() - size * 0.4, centre.y() + size * 0.62),
                QPointF(centre.x() + size * 0.66, centre.y()),
            ])
            painter.drawPolygon(triangle)
        elif self.shape_name == "pause":
            bar, gap = size * 0.28, size * 0.22
            for offset in (-gap - bar, gap):
                painter.drawRoundedRect(
                    QRectF(centre.x() + offset, centre.y() - size * 0.6, bar, size * 1.2), 1, 1
                )
        else:  # stop
            painter.drawRoundedRect(
                QRectF(centre.x() - size * 0.5, centre.y() - size * 0.5, size, size), 2, 2
            )
        painter.end()


def _format_time(ms: int) -> str:
    if ms <= 0:
        return "0:00"
    seconds = ms // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


class PlayerBar(QWidget):
    """Transport, track name, waveform and volume."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("playerBar")
        self._track: Track | None = None
        self._source: Path | None = None
        self._temp_files: list[Path] = []
        self._pool = QThreadPool.globalInstance()

        self._audio = QAudioOutput()
        self._audio.setVolume(0.8)
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio)

        # Qt binds to whichever output was default when the player was created and
        # then stays there, so unplugging a Bluetooth speaker or plugging in a USB
        # interface leaves playback pointed at a device that is no longer there.
        # Watching for device changes and re-binding keeps it on the laptop's current
        # default, which is what you'd expect from any other player.
        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._on_devices_changed)
        self._current_device_id = self._default_device_id()
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.errorOccurred.connect(self._on_error)

        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 10, 16, 10)
        outer.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)

        brand_lockup = QWidget()
        brand_lockup.setObjectName("brandLockup")
        brand_layout = QHBoxLayout(brand_lockup)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(0)

        self.brand_icon_label = QLabel()
        self.brand_icon_label.setObjectName("brandIcon")
        self.brand_icon_label.setAccessibleName("KRATR crater record icon")
        self.brand_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_icon_label.setFixedSize(46, 46)
        icon = application_icon()
        if not icon.isNull():
            self.brand_icon_label.setPixmap(icon.pixmap(42, 42))
        brand_layout.addWidget(self.brand_icon_label)

        self.brand_label = QLabel()
        self.brand_label.setObjectName("brandWordmark")
        self.brand_label.setAccessibleName("KRATR")
        self.brand_label.setToolTip("KRATR — DJ track intake for rekordbox")
        self.brand_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_label.setFixedSize(150, 46)
        wordmark = wordmark_pixmap()
        if wordmark.isNull():
            self.brand_label.setText("KRATR")
        else:
            self.brand_label.setPixmap(
                wordmark.scaled(
                    136,
                    34,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        brand_layout.addWidget(self.brand_label)
        top.addWidget(brand_lockup)

        divider = QFrame()
        divider.setObjectName("mastheadDivider")
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFixedHeight(46)
        top.addWidget(divider)

        brand_meta = QLabel("AUDIO INTAKE\n// REKORDBOX")
        brand_meta.setObjectName("brandMeta")
        brand_meta.setFixedWidth(96)
        top.addWidget(brand_meta)

        self.play_button = TransportButton("play")
        self.play_button.clicked.connect(self.toggle)
        self.play_button.setToolTip("Play / pause  (Space)")
        top.addWidget(self.play_button)

        self.stop_button = TransportButton("stop")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setToolTip("Stop")
        top.addWidget(self.stop_button)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title_label = QLabel("Nothing loaded")
        self.title_label.setObjectName("playerTitle")
        titles.addWidget(self.title_label)
        self.detail_label = QLabel("Double-click a track to play it")
        self.detail_label.setObjectName("playerDetail")
        titles.addWidget(self.detail_label)
        top.addLayout(titles, stretch=1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("playerTime")
        top.addWidget(self.time_label)

        volume_icon = QLabel("🔊")
        volume_icon.setObjectName("playerDetail")
        top.addWidget(volume_icon)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(110)
        self.volume.valueChanged.connect(lambda v: self._audio.setVolume(v / 100))
        top.addWidget(self.volume)
        outer.addLayout(top)

        self.waveform = WaveformView()
        self.waveform.seek_requested.connect(self.seek_fraction)
        outer.addWidget(self.waveform, stretch=1)

    # ------------------------------------------------------------------ loading
    def load(self, track: Track, autoplay: bool = True) -> None:
        """Load a track, transcoding first if QtMultimedia can't decode it."""
        path = track.final_path or track.source_path
        if not path.is_file():
            self.detail_label.setText(f"File not found: {path}")
            return

        self._track = track
        self._source = path

        self.title_label.setText(track.display_name)
        info = track.info
        if info:
            bits = f"{info.bit_depth}-bit " if info.bit_depth else ""
            rate = f"{info.sample_rate / 1000:g} kHz" if info.sample_rate else ""
            self.detail_label.setText(
                f"{info.fmt.label if info.fmt else info.codec} · {bits}{rate}"
            )
        else:
            self.detail_label.setText(path.name)

        self.waveform.clear("Reading waveform…")
        task = WaveformTask(path)
        task.signals.ready.connect(self._on_peaks)
        task.signals.failed.connect(lambda _p, _m: self.waveform.clear("Waveform unavailable"))
        self._pool.start(task)

        self._player.setSource(QUrl.fromLocalFile(str(self._playable(path))))
        if autoplay:
            self._player.play()

    def _playable(self, path: Path) -> Path:
        if path.suffix.lower() not in _NEEDS_TRANSCODE:
            return path
        try:
            temp = Path(tempfile.mkdtemp(prefix="crate-play-")) / (path.stem + ".wav")
            ffmpeg.run([
                ffmpeg.ffmpeg_path(), "-v", "error", "-y",
                "-i", str(path), "-c:a", "pcm_s16le", str(temp),
            ], timeout=300)
            self._temp_files.append(temp)
            logger.debug("Transcoded %s for playback", path.name)
            return temp
        except Exception:  # noqa: BLE001 - fall through and let Qt report it
            logger.warning("Could not transcode %s for playback", path.name, exc_info=True)
            return path

    def _on_peaks(self, path: Path, peaks: list[float]) -> None:
        # A slow waveform must not overwrite a track loaded since.
        if self._source is not None and Path(path) == self._source:
            self.waveform.set_peaks(peaks)

    # -------------------------------------------------------------- transport
    def toggle(self) -> None:
        if self._player.playbackState() is QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        elif self._source is not None:
            self._player.play()

    def stop(self) -> None:
        self._player.stop()
        self.waveform.set_position(0.0)

    # -------------------------------------------------------- audio devices
    @staticmethod
    def _default_device_id() -> bytes:
        device = QMediaDevices.defaultAudioOutput()
        return bytes(device.id()) if device is not None and not device.isNull() else b""

    def _on_devices_changed(self) -> None:
        """Follow the system default when audio hardware comes or goes."""
        new_id = self._default_device_id()
        if new_id == self._current_device_id:
            return  # the list changed but the default we're using did not

        device = QMediaDevices.defaultAudioOutput()
        if device is None or device.isNull():
            logger.warning("No audio output device available")
            return

        was_playing = self._player.playbackState() is QMediaPlayer.PlaybackState.PlayingState
        position = self._player.position()

        self._audio.setDevice(device)
        self._current_device_id = new_id
        logger.info("Switched audio output to %s", device.description())

        # Some backends drop the stream when the device changes underneath them, so
        # playback is re-established explicitly rather than assumed to continue.
        if was_playing:
            self._player.setPosition(position)
            self._player.play()

        self.detail_label.setText(f"Playing through {device.description()}")

    def release(self, path: Path | None = None) -> bool:
        """Let go of the file so it can be moved or deleted.

        Windows refuses to move or unlink a file another process has open, and
        QMediaPlayer keeps a handle on whatever it has loaded. Since the workflow is
        *listen to it, then file it*, the player has to release the track before the
        file moves — otherwise filing the track you are auditioning always fails.

        Returns True if it was actually holding that file.
        """
        if self._source is None:
            return False
        if path is not None and Path(path).resolve() != self._source.resolve():
            return False

        self._player.stop()
        self._player.setSource(QUrl())
        self.waveform.set_position(0.0)
        logger.debug("Released %s for filing", self._source.name)
        return True

    def reload_at(self, track: Track) -> None:
        """Point the player at a track's new location after it has moved."""
        self.load(track, autoplay=False)

    def seek_fraction(self, fraction: float) -> None:
        duration = self._player.duration()
        if duration > 0:
            self._player.setPosition(int(fraction * duration))

    @property
    def current_track(self) -> Track | None:
        return self._track

    # ---------------------------------------------------------------- signals
    def _on_position(self, position: int) -> None:
        duration = self._player.duration()
        if duration > 0:
            self.waveform.set_position(position / duration)
        self.time_label.setText(f"{_format_time(position)} / {_format_time(duration)}")

    def _on_duration(self, duration: int) -> None:
        self.time_label.setText(f"{_format_time(self._player.position())} / {_format_time(duration)}")

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state is QMediaPlayer.PlaybackState.PlayingState
        self.play_button.set_shape("pause" if playing else "play")

    def _on_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error is not QMediaPlayer.Error.NoError:
            logger.warning("Playback error: %s (%s)", message, error)
            self.detail_label.setText(f"Cannot play this file — {message}")

    def cleanup(self) -> None:
        self._player.stop()
        for temp in self._temp_files:
            try:
                temp.unlink(missing_ok=True)
                temp.parent.rmdir()
            except OSError:
                pass
        self._temp_files = []
