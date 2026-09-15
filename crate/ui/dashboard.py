"""Home dashboard — collect tracks, listen to them, import one at a time."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import LibraryStatus, QualityRating, Track, TrackState
from ..pipeline import duplicates, trash
from .branding import application_icon
from .dialogs import ask

logger = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".wav", ".aiff", ".aif", ".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg"}
AUDIO_FILTER = "Audio (*.wav *.aiff *.aif *.flac *.mp3 *.m4a *.aac *.ogg);;All files (*)"

_STATE_LABELS = {
    TrackState.PENDING: "Waiting",
    TrackState.ANALYSING: "Analysing…",
    TrackState.READY: "Ready",
    TrackState.CONVERTING: "Working…",
    TrackState.FILED: "Filed",
    TrackState.QUEUED: "Queued for rekordbox",
    TrackState.QUARANTINED: "Quarantined",
    TrackState.FAILED: "Failed",
    TrackState.SKIPPED: "Skipped",
}

_LIBRARY_LABELS = {
    LibraryStatus.NEW: "New",
    LibraryStatus.FILED_CORRECTLY: "In library",
    LibraryStatus.MISFILED: "In library · misfiled",
    LibraryStatus.MISSING_FILE: "In library · file missing",
    LibraryStatus.DUPLICATE: "Possible duplicate",
}

_QUALITY_CLASS = {
    QualityRating.LOSSLESS: "good",
    QualityRating.HIGH: "good",
    QualityRating.MEDIUM: "warn",
    QualityRating.LOW: "bad",
    QualityRating.POOR: "bad",
    QualityRating.UNKNOWN: "muted",
}


class CleanSelectionDelegate(QStyledItemDelegate):
    """Keep keyboard focus without Windows drawing a frame through cell text."""

    def initStyleOption(self, option, index) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_HasFocus


def collect_audio_paths(urls) -> list[Path]:
    """Audio files from dropped URLs, expanding any folders."""
    paths: list[Path] = []
    for url in urls:
        local = url.toLocalFile()
        if not local:
            continue
        path = Path(local)
        if path.is_dir():
            paths.extend(
                p for p in sorted(path.rglob("*"))
                if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
            )
        elif path.suffix.lower() in AUDIO_SUFFIXES:
            paths.append(path)
    return paths


class Dashboard(QWidget):
    """The landing page, and where you return after each import."""

    files_added = Signal(list)
    play_requested = Signal(object)     # Track
    import_requested = Signal(object)   # Track
    delete_requested = Signal(object)   # Track — file already deleted
    settings_requested = Signal()
    selection_changed = Signal(object)

    COLUMNS = ("Track", "Format", "Quality", "Duplicate", "Library", "Status")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tracks: list[Track] = []
        self._groups: list[duplicates.DuplicateGroup] = []
        # The whole page is the drop target — no separate strip to aim at.
        self.setAcceptDrops(True)
        self._build()

    # ------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_dragging(True)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_dragging(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._set_dragging(False)
        paths = collect_audio_paths(event.mimeData().urls())
        if paths:
            self.files_added.emit(paths)
        event.acceptProposedAction()

    def _set_dragging(self, dragging: bool) -> None:
        for target in (self.table, self.empty_panel):
            target.setProperty("dragging", dragging)
            target.style().unpolish(target)
            target.style().polish(target)

    def _build(self) -> None:
        self.setObjectName("dashboardPage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(16)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        eyebrow = QLabel("KRATR / INTAKE QUEUE")
        eyebrow.setObjectName("eyebrow")
        titles.addWidget(eyebrow)
        title = QLabel("TRACK INTAKE")
        title.setObjectName("pageTitle")
        titles.addWidget(title)
        header.addLayout(titles)
        header.addStretch(1)

        self.settings_button = QPushButton("SETTINGS")
        self.settings_button.setToolTip("Manage folders, tags, colours, and library behaviour")
        self.settings_button.clicked.connect(self.settings_requested)
        header.addWidget(self.settings_button)

        self.queue_count = QLabel("00")
        self.queue_count.setObjectName("indexDisplay")
        self.queue_count.setToolTip("Tracks currently in the intake queue")
        header.addWidget(self.queue_count)
        layout.addLayout(header)

        action_rail = QWidget()
        action_rail.setObjectName("actionRail")
        actions = QHBoxLayout(action_rail)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)

        self.add_button = QPushButton("+  ADD FILES")
        self.add_button.clicked.connect(self._browse)
        actions.addWidget(self.add_button)

        actions.addStretch(1)

        self.remove_button = QPushButton("REMOVE")
        self.remove_button.setToolTip("Take it off this list. The file is left alone.")
        self.remove_button.clicked.connect(self._remove_selected)
        actions.addWidget(self.remove_button)

        self.delete_button = QPushButton("DELETE FILE")
        self.delete_button.setObjectName("danger")
        self.delete_button.setToolTip("Delete the track from your computer (Recycle Bin)")
        self.delete_button.clicked.connect(self._delete_selected)
        actions.addWidget(self.delete_button)

        self.remove_duplicates_button = QPushButton("REMOVE DUPLICATES")
        self.remove_duplicates_button.setObjectName("danger")
        self.remove_duplicates_button.clicked.connect(self._remove_duplicates)
        self.remove_duplicates_button.hide()
        actions.addWidget(self.remove_duplicates_button)

        self.clear_button = QPushButton("CLEAR FINISHED")
        self.clear_button.clicked.connect(self._clear_finished)
        actions.addWidget(self.clear_button)
        layout.addWidget(action_rail)

        # Progress lives here rather than in the status bar: when a folder of 135
        # tracks is churning away, the bottom of the window is not where you're looking.
        self.progress_row = QWidget()
        progress_layout = QHBoxLayout(self.progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(12)

        self.progress_label = QLabel()
        self.progress_label.setObjectName("progressLabel")
        self.progress_label.setMinimumWidth(230)
        progress_layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)
        progress_layout.addWidget(self.progress_bar, stretch=1)

        self.progress_row.hide()
        layout.addWidget(self.progress_row)

        self.duplicate_banner = QLabel()
        self.duplicate_banner.setObjectName("warning")
        self.duplicate_banner.setWordWrap(True)
        self.duplicate_banner.hide()
        layout.addWidget(self.duplicate_banner)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setItemDelegate(CleanSelectionDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setMinimumSectionSize(42)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.itemDoubleClicked.connect(self._on_double_click)
        self.table.itemSelectionChanged.connect(self._on_selection)

        headers = self.table.horizontalHeader()
        headers.setMinimumHeight(52)
        headers.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(self.COLUMNS)):
            headers.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, stretch=1)

        self.empty_panel = QWidget()
        self.empty_panel.setObjectName("emptyPanel")
        empty_layout = QVBoxLayout(self.empty_panel)
        empty_layout.setContentsMargins(40, 34, 40, 34)
        empty_layout.setSpacing(12)
        empty_layout.addStretch(1)

        empty_icon = QLabel()
        empty_icon.setObjectName("emptyIcon")
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = application_icon()
        if not icon.isNull():
            empty_icon.setPixmap(icon.pixmap(112, 112))
        empty_layout.addWidget(empty_icon)

        self.empty_hint = QLabel(
            "DROP AUDIO / BUILD THE CRATER\n"
            "FILES OR FOLDERS FROM ANYWHERE\n\n"
            "ADD FILES TO BEGIN"
        )
        self.empty_hint.setObjectName("emptyHint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_hint)
        empty_layout.addStretch(1)
        layout.addWidget(self.empty_panel, stretch=1)

        footer = QHBoxLayout()
        self.hint_label = QLabel("Double-click a track to play it")
        self.hint_label.setObjectName("muted")
        footer.addWidget(self.hint_label)
        footer.addStretch(1)

        self.import_button = QPushButton("Import selected track  →")
        self.import_button.setObjectName("primary")
        self.import_button.setMinimumWidth(240)
        self.import_button.clicked.connect(self._import_selected)
        footer.addWidget(self.import_button)
        layout.addLayout(footer)

        self._update_buttons()

    # ------------------------------------------------------------------ data
    def set_tracks(self, tracks: list[Track]) -> None:
        self._tracks = tracks
        self.queue_count.setText(f"{len(tracks):02d}")
        selected = self.selected_track()
        self._groups = duplicates.find_groups(tracks)

        self.table.blockSignals(True)
        self.table.setRowCount(len(tracks))
        for row, track in enumerate(tracks):
            self._fill_row(row, track)
        self.table.blockSignals(False)
        self._refresh_duplicate_banner()
        self._refresh_empty_state()

        # Restore the previous selection, or pick something usable so the Import
        # button is live as soon as a track is ready — having to click a row first
        # made it look like the button had stopped working.
        restored = False
        if selected is not None:
            for row, track in enumerate(tracks):
                if track is selected:
                    self.table.selectRow(row)
                    restored = True
                    break

        if not restored and tracks:
            first_ready = next(
                (i for i, t in enumerate(tracks) if t.state is TrackState.READY), 0
            )
            self.table.selectRow(first_ready)

        self._update_buttons()

    def _fill_row(self, row: int, track: Track) -> None:
        info = track.info
        quality = track.quality

        name = QTableWidgetItem(track.display_name)
        name.setData(Qt.ItemDataRole.UserRole, id(track))
        name.setToolTip(str(track.source_path))
        self.table.setItem(row, 0, name)

        fmt = "—"
        if info:
            fmt = info.fmt.label if info.fmt else info.codec.upper()
            if info.bitrate_kbps and info.is_lossy:
                fmt += f" {info.bitrate_kbps}k"
        self.table.setItem(row, 1, QTableWidgetItem(fmt))

        if quality is None:
            verdict = "—"
        elif quality.is_suspect:
            verdict = f"⚠ Upscale · {quality.cutoff_khz} kHz"
        else:
            verdict = quality.rating.label
        item = QTableWidgetItem(verdict)
        if quality is not None:
            item.setForeground(
                QColor("#ffffff") if quality.is_suspect
                else (QColor("#777777") if quality.rating is QualityRating.UNKNOWN
                      else QColor("#d8d8d8"))
            )
        self.table.setItem(row, 2, item)

        group = duplicates.duplicates_of(track, self._groups)
        if group is None:
            duplicate_item = QTableWidgetItem("")
        elif group.best is track:
            duplicate_item = QTableWidgetItem(f"★ Keep  (best of {len(group.tracks)})")
            duplicate_item.setForeground(QColor("#ffffff"))
            duplicate_item.setToolTip(group.recommendation())
        else:
            reason = duplicates.describe_advantage(group.best, track)
            duplicate_item = QTableWidgetItem("Duplicate — consider removing")
            duplicate_item.setForeground(QColor("#bcbcbc"))
            duplicate_item.setToolTip(f"{group.best.display_name} is better: {reason}")
            # A muted tint, not a bold fill: a saturated red row reads as *selected*,
            # which made it look like a track was picked when nothing was.
            tint = QColor("#151515")
            for column in range(len(self.COLUMNS)):
                existing = self.table.item(row, column)
                if existing is not None:
                    existing.setBackground(tint)
        self.table.setItem(row, 3, duplicate_item)

        self.table.setItem(row, 4, QTableWidgetItem(_LIBRARY_LABELS.get(track.library_status, "")))
        self.table.setItem(row, 5, QTableWidgetItem(_STATE_LABELS.get(track.state, "")))

    # --------------------------------------------------------------- selection
    def selected_track(self) -> Track | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        marker = item.data(Qt.ItemDataRole.UserRole)
        return next((t for t in self._tracks if id(t) == marker), None)

    def _on_selection(self) -> None:
        self._update_buttons()
        self.selection_changed.emit(self.selected_track())

    def _on_double_click(self, item: QTableWidgetItem) -> None:
        marker = self.table.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)
        track = next((t for t in self._tracks if id(t) == marker), None)
        if track is not None:
            self.play_requested.emit(track)

    def set_analysis_progress(self, done: int, total: int) -> None:
        """Show how far through a batch of analyses we are, or hide when finished."""
        if total <= 0 or done >= total:
            self.progress_row.hide()
            self.progress_bar.setValue(0)
            return

        self.progress_row.show()
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        remaining = total - done
        self.progress_label.setText(
            f"Analysing {done} of {total}"
            + (f"  ·  {remaining} to go" if remaining else "")
        )

    def _refresh_empty_state(self) -> None:
        """With no separate drop strip, the empty table has to say what to do."""
        empty = not self._tracks
        self.empty_hint.setVisible(empty)
        self.empty_panel.setVisible(empty)
        self.table.setVisible(not empty)

    def _update_buttons(self) -> None:
        track = self.selected_track()
        ready = track is not None and track.state in {
            TrackState.READY, TrackState.FILED, TrackState.QUEUED
        }
        self.import_button.setEnabled(ready)
        self.remove_button.setEnabled(track is not None)
        self.delete_button.setEnabled(track is not None)

        if track is None:
            self.hint_label.setText("Double-click a track to play it")
        elif track.state is TrackState.ANALYSING:
            self.hint_label.setText("Analysing — the verdict will appear shortly")
        elif track.state is TrackState.FAILED:
            self.hint_label.setText(f"Failed: {track.error}")
        else:
            self.hint_label.setText("Double-click to play · Import to start filing it")

    # ---------------------------------------------------------------- actions
    def _browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add tracks", "", AUDIO_FILTER)
        if paths:
            self.files_added.emit([Path(p) for p in paths])

    def _import_selected(self) -> None:
        track = self.selected_track()
        if track is not None:
            self.import_requested.emit(track)

    def _drop_track(self, track: Track) -> None:
        """Remove by identity.

        ``list.remove()`` matches by equality, and Track is a dataclass — so two
        duplicate copies of the same recording compare equal and it could remove the
        wrong row. Duplicates are a normal case here, so identity is the only safe test.
        """
        self._tracks[:] = [t for t in self._tracks if t is not track]

    def _remove_selected(self) -> None:
        track = self.selected_track()
        if track is not None:
            self._drop_track(track)
            self.set_tracks(self._tracks)

    def _delete_selected(self) -> None:
        """Delete the file itself — the only thing here that touches your disk."""
        track = self.selected_track()
        if track is None:
            return

        path = track.final_path or track.source_path
        if not path.is_file():
            self._tracks.remove(track)
            self.set_tracks(self._tracks)
            return

        size = path.stat().st_size / 1e6
        if not ask(
            self,
            "Delete this file?",
            f"<b>{track.display_name}</b><br><br>"
            f"{path}<br>{size:.1f} MB<br><br>"
            f"This deletes the file from your computer. It goes to the "
            f"<b>Recycle Bin</b>, so it can be restored if you change your mind.",
        ):
            return

        # The window owns the player, and Windows won't delete a file it still has
        # open, so the deletion itself is done there after the handle is released.
        self.delete_requested.emit(track)

    def confirm_deleted(self, track: Track, path: Path) -> None:
        self._drop_track(track)
        self.set_tracks(self._tracks)
        self.hint_label.setText(f"Deleted {path.name} — it's in your Recycle Bin")

    def report_delete_failure(self, failures: list[tuple[Path, str]]) -> None:
        QMessageBox.warning(
            self, "Could not delete",
            "\n".join(f"{p.name}: {why}" for p, why in failures),
        )

    def _refresh_duplicate_banner(self) -> None:
        if not self._groups:
            self.duplicate_banner.hide()
            return

        lines = [g.recommendation() for g in self._groups[:4]]
        if len(self._groups) > 4:
            lines.append(f"…and {len(self._groups) - 4} more.")
        self.duplicate_banner.setText(
            f"<b>{len(self._groups)} possible duplicate(s).</b> "
            "Different mixes and edits are not counted — only copies that look like the "
            "same recording.<br>" + "<br>".join(lines)
        )
        self.duplicate_banner.show()
        self.remove_duplicates_button.setVisible(True)
        total = sum(len(g.others) for g in self._groups)
        self.remove_duplicates_button.setText(f"Remove {total} lower-quality copy(s)")

    def _remove_duplicates(self) -> None:
        losers = [t for group in self._groups for t in group.others]
        if not losers:
            return
        detail = "\n".join(
            f"• {t.display_name}  —  keeping {duplicates.duplicates_of(t, self._groups).best.display_name}"
            for t in losers[:10]
        )
        if not ask(
            self,
            "Remove duplicate copies?",
            f"Remove {len(losers)} lower-quality copy(s) from this list?\n\n{detail}\n\n"
            f"They are only removed from the list — the files are left exactly where "
            f"they are, and nothing is deleted from your computer.",
        ):
            return
        for track in losers:
            self._drop_track(track)
        self.set_tracks(self._tracks)

    def _clear_finished(self) -> None:
        done = {TrackState.FILED, TrackState.QUEUED, TrackState.QUARANTINED, TrackState.SKIPPED}
        self._tracks[:] = [t for t in self._tracks if t.state not in done]
        self.set_tracks(self._tracks)
