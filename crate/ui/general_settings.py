"""Editable KRATR preferences and safety diagnostics."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..models import AudioFormat
from ..rekordbox import session as session_mod


class GeneralSettingsPage(QWidget):
    """The first settings section: paths, import defaults, tags and safety."""

    saved = Signal()

    TARGETS = (
        ("Keep original", None),
        ("WAV", AudioFormat.WAV.value),
        ("AIFF", AudioFormat.AIFF.value),
        ("FLAC", AudioFormat.FLAC.value),
        ("ALAC", AudioFormat.ALAC.value),
        ("MP3", AudioFormat.MP3.value),
    )

    def __init__(self, settings: config.Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.rule_inputs: dict[str, QComboBox] = {}
        self._build()
        self.load()

    def _target_combo(self) -> QComboBox:
        combo = QComboBox()
        for label, value in self.TARGETS:
            combo.addItem(label, value)
        return combo

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(16)

        layout.addWidget(self._heading("LIBRARY PATHS"))
        paths = QFormLayout()
        music_row = QHBoxLayout()
        self.music_root = QLineEdit()
        music_row.addWidget(self.music_root, stretch=1)
        self.browse_music = QPushButton("Browse…")
        self.browse_music.clicked.connect(self._browse_music)
        music_row.addWidget(self.browse_music)
        paths.addRow("Music root", music_row)
        self.quarantine_folder = QLineEdit()
        paths.addRow("Re-source folder", self.quarantine_folder)
        layout.addLayout(paths)

        layout.addWidget(self._heading("IMPORT DEFAULTS"))
        conversion = QFormLayout()
        self.default_target = self._target_combo()
        conversion.addRow("Fallback target", self.default_target)
        for source in AudioFormat:
            combo = self._target_combo()
            self.rule_inputs[source.value] = combo
            conversion.addRow(f"{source.label} source", combo)
        self.bit_depth = QComboBox()
        self.bit_depth.addItem("Keep source / format default", None)
        for value in (16, 24, 32):
            self.bit_depth.addItem(f"{value}-bit", value)
        conversion.addRow("Target bit depth", self.bit_depth)
        self.sample_rate = QComboBox()
        self.sample_rate.addItem("Keep source", None)
        for value in (44_100, 48_000, 88_200, 96_000):
            self.sample_rate.addItem(f"{value:,} Hz", value)
        conversion.addRow("Target sample rate", self.sample_rate)
        layout.addLayout(conversion)

        layout.addWidget(self._heading("ANALYSIS & TAGGING"))
        behaviour = QFormLayout()
        self.analysis_window = QSpinBox()
        self.analysis_window.setRange(5, 180)
        self.analysis_window.setSuffix(" seconds")
        behaviour.addRow("Analysis window", self.analysis_window)
        self.cutoff_threshold = QSpinBox()
        self.cutoff_threshold.setRange(20, 100)
        self.cutoff_threshold.setSuffix(" dB")
        behaviour.addRow("Cutoff threshold", self.cutoff_threshold)
        self.mirror_comments = QCheckBox("Mirror MyTags into rekordbox comments for CDJ search")
        behaviour.addRow("Comment mirror", self.mirror_comments)
        self.comment_prefix = QLineEdit()
        self.comment_prefix.setMaximumWidth(100)
        behaviour.addRow("Tag prefix", self.comment_prefix)
        layout.addLayout(behaviour)

        layout.addWidget(self._heading("IMPORT STEPS"))
        steps_box = QVBoxLayout()
        self.use_tags = QCheckBox("Tag tracks during import (KRATR suggests a starter set)")
        steps_box.addWidget(self.use_tags)
        self.use_colours = QCheckBox("Colour-code tracks during import")
        steps_box.addWidget(self.use_colours)
        steps_note = QLabel(
            "Both are optional and personal. Turning one off removes its page from the "
            "import wizard. Changes here take effect the next time you start KRATR."
        )
        steps_note.setObjectName("muted")
        steps_note.setWordWrap(True)
        steps_box.addWidget(steps_note)
        layout.addLayout(steps_box)

        layout.addWidget(self._heading("SAFETY & TOOLS"))
        safety = QFormLayout()
        self.backup_retention = QSpinBox()
        self.backup_retention.setRange(1, 200)
        self.backup_retention.setSuffix(" backups")
        safety.addRow("Backup retention", self.backup_retention)
        self.ffmpeg_path = QLineEdit()
        self.ffmpeg_path.setPlaceholderText("Use PATH")
        safety.addRow("ffmpeg", self.ffmpeg_path)
        self.ffprobe_path = QLineEdit()
        self.ffprobe_path.setPlaceholderText("Use PATH")
        safety.addRow("ffprobe", self.ffprobe_path)
        layout.addLayout(safety)

        self.status = QLabel()
        self.status.setObjectName("summaryBox")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh status")
        self.refresh_button.clicked.connect(self.refresh_status)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        self.save_button = QPushButton("SAVE PREFERENCES")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        layout.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll)

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("categoryTitle")
        return label

    @staticmethod
    def _set_combo(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(max(0, index))

    def load(self) -> None:
        settings = self.settings
        self.music_root.setText(settings.music_root)
        self.quarantine_folder.setText(settings.quarantine_folder)
        self._set_combo(self.default_target, settings.default_target)
        for source, combo in self.rule_inputs.items():
            self._set_combo(combo, settings.conversion_rules.get(source))
        self._set_combo(self.bit_depth, settings.default_bit_depth)
        self._set_combo(self.sample_rate, settings.default_sample_rate)
        self.analysis_window.setValue(round(settings.analysis_window_s))
        self.cutoff_threshold.setValue(round(settings.cutoff_threshold_db))
        self.mirror_comments.setChecked(settings.mirror_tags_to_comments)
        self.comment_prefix.setText(settings.comment_tag_prefix)
        self.use_tags.setChecked(settings.use_tags)
        self.use_colours.setChecked(settings.use_colours)
        self.backup_retention.setValue(settings.backup_retention)
        self.ffmpeg_path.setText(settings.ffmpeg_path or "")
        self.ffprobe_path.setText(settings.ffprobe_path or "")
        self.refresh_status()

    def save(self) -> None:
        root = self.music_root.text().strip()
        quarantine = self.quarantine_folder.text().strip()
        prefix = self.comment_prefix.text().strip()
        if not root or not quarantine:
            QMessageBox.warning(self, "Settings incomplete", "Library paths cannot be empty.")
            return
        if self.mirror_comments.isChecked() and not prefix:
            QMessageBox.warning(self, "Settings incomplete", "The comment tag prefix cannot be empty.")
            return

        settings = self.settings
        settings.music_root = str(Path(root))
        settings.quarantine_folder = quarantine
        settings.default_target = self.default_target.currentData()
        settings.conversion_rules = {
            source: combo.currentData() for source, combo in self.rule_inputs.items()
        }
        settings.default_bit_depth = self.bit_depth.currentData()
        settings.default_sample_rate = self.sample_rate.currentData()
        settings.analysis_window_s = float(self.analysis_window.value())
        settings.cutoff_threshold_db = float(self.cutoff_threshold.value())
        settings.mirror_tags_to_comments = self.mirror_comments.isChecked()
        settings.comment_tag_prefix = prefix or "/"
        settings.use_tags = self.use_tags.isChecked()
        settings.use_colours = self.use_colours.isChecked()
        settings.backup_retention = self.backup_retention.value()
        settings.ffmpeg_path = self.ffmpeg_path.text().strip() or None
        settings.ffprobe_path = self.ffprobe_path.text().strip() or None
        settings.save()
        self.saved.emit()

    def refresh_status(self) -> None:
        status = session_mod.status(self.settings)
        running = "RUNNING — close it before writes" if status["rekordbox_running"] else "closed"
        database = "found" if status["database_present"] else "not found"
        backup = status["latest_backup"] or "none yet"
        self.status.setText(
            f"<b>rekordbox:</b> {running} &nbsp;·&nbsp; <b>database:</b> {database}<br>"
            f"<b>Backups:</b> {status['backup_count']} &nbsp;·&nbsp; latest: {backup}<br>"
            f"Every rekordbox write requires the app to be closed, verifies the schema, "
            f"and creates a timestamped database + playlist XML backup first."
        )

    def set_music_root_locked(self, locked: bool) -> None:
        """Keep queued folder operations bound to the root they were validated against."""
        self.music_root.setEnabled(not locked)
        self.browse_music.setEnabled(not locked)
        if locked:
            self.music_root.setText(self.settings.music_root)
            self.music_root.setToolTip(
                "Write or clear the pending folder edit before changing the music root."
            )
        else:
            self.music_root.setToolTip("")

    def _browse_music(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose music root", self.music_root.text() or str(Path.home())
        )
        if chosen:
            self.music_root.setText(chosen)
