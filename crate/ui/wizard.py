"""The import wizard: one decision per screen, with the player pinned above it."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..models import KeepOriginal, Track
from ..pipeline.filing import MusicLibrary
from .dialogs import ask
from .steps import ColourStep, FolderStep, PlaylistStep, QualityStep, Step, TagsStep

logger = logging.getLogger(__name__)


class StepIndicator(QWidget):
    """Where you are in the five steps — and a way to jump straight to any of them."""

    step_clicked = Signal(int)

    def __init__(self, titles: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._pills: list[QPushButton] = []

        for index, title in enumerate(titles):
            if index:
                arrow = QLabel("/")
                arrow.setObjectName("stepArrow")
                layout.addWidget(arrow)
            pill = QPushButton(f"0{index + 1} / {title.upper()}")
            pill.setObjectName("stepPill")
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            pill.setToolTip(f"Go to {title}")
            pill.clicked.connect(lambda _=False, i=index: self.step_clicked.emit(i))
            self._pills.append(pill)
            layout.addWidget(pill)
        layout.addStretch(1)

    def set_current(self, index: int) -> None:
        for position, pill in enumerate(self._pills):
            state = "current" if position == index else ("done" if position < index else "todo")
            pill.setProperty("state", state)
            pill.style().unpolish(pill)
            pill.style().polish(pill)

    def set_navigation_enabled(self, enabled: bool) -> None:
        for pill in self._pills:
            pill.setEnabled(enabled)


class ImportWizard(QWidget):
    """Quality → Folder → Playlists → Tags → Colour."""

    cancelled = Signal()
    completed = Signal(object)              # Track — ready for the full commit
    quarantine_requested = Signal(object)
    discard_requested = Signal(object)
    taxonomy_edit = Signal(str, dict, str)

    def __init__(
        self,
        library: MusicLibrary,
        use_tags: bool = True,
        use_colours: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.track: Track | None = None
        self._busy = False
        self._commit_retry = False

        # Every step is always constructed, so main_window can keep configuring
        # ``tags_step``/``colour_step`` (categories, colours, pending edits) whether or
        # not they're shown. Only the enabled ones join the visible flow below.
        self.quality_step = QualityStep()
        self.folder_step = FolderStep(library)
        self.playlist_step = PlaylistStep()
        self.tags_step = TagsStep()
        self.colour_step = ColourStep()

        self.steps: list[Step] = [self.quality_step, self.folder_step, self.playlist_step]
        if use_tags:
            self.steps.append(self.tags_step)
        if use_colours:
            self.steps.append(self.colour_step)

        for step in self.steps:
            step.finished_early.connect(self._on_finished_early)
            step.validity_changed.connect(lambda _v: self._update_nav())
        # Connect the taxonomy-edit signals even for a step that isn't shown: harmless
        # when hidden, and correct the moment it is enabled.
        self.tags_step.edit_requested.connect(self.taxonomy_edit)
        self.colour_step.edit_requested.connect(self.taxonomy_edit)

        self._build()

    def _build(self) -> None:
        self.setObjectName("importWizard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(1)
        eyebrow = QLabel("KRATR / TRACK PROCESS")
        eyebrow.setObjectName("eyebrow")
        titles.addWidget(eyebrow)
        self.title = QLabel()
        self.title.setObjectName("pageTitle")
        titles.addWidget(self.title)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("muted")
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch(1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel)
        header.addWidget(self.cancel_button)
        layout.addLayout(header)

        self.indicator = StepIndicator([s.title for s in self.steps])
        self.indicator.step_clicked.connect(self.goto)
        layout.addWidget(self.indicator)

        self.stack = QStackedWidget()
        for step in self.steps:
            self.stack.addWidget(step)
        layout.addWidget(self.stack, stretch=1)

        footer = QHBoxLayout()
        self.back_button = QPushButton("←  Back")
        self.back_button.clicked.connect(self.back)
        footer.addWidget(self.back_button)
        footer.addStretch(1)

        self.commit_progress = QProgressBar()
        self.commit_progress.setObjectName("commitProgress")
        self.commit_progress.setTextVisible(False)
        self.commit_progress.setFixedSize(260, 10)
        self.commit_progress.hide()
        footer.addWidget(self.commit_progress)

        self.next_button = QPushButton("Next  →")
        self.next_button.setObjectName("primary")
        self.next_button.setMinimumWidth(200)
        self.next_button.clicked.connect(self.next)
        footer.addWidget(self.next_button)
        layout.addLayout(footer)

    # ------------------------------------------------------------------ flow
    def start(self, track: Track) -> None:
        self.track = track
        self._busy = False
        self._commit_retry = False
        self.commit_progress.hide()
        self.indicator.set_navigation_enabled(True)
        self.cancel_button.setEnabled(True)
        self.back_button.setEnabled(True)
        for step in self.steps:
            step.load(track)
        self.stack.setCurrentIndex(0)
        self._update_nav()

    @property
    def index(self) -> int:
        return self.stack.currentIndex()

    def current_step(self) -> Step:
        return self.steps[self.index]

    def goto(self, index: int) -> None:
        """Jump to any step, keeping every choice made so far.

        Choices are saved onto the in-memory track as you leave a step, so moving
        around freely never loses them — and never writes anything either. Nothing
        reaches rekordbox until Import.
        """
        if self.track is None or index == self.index or self._busy or self._commit_retry:
            return
        index = max(0, min(index, len(self.steps) - 1))

        # validate=False: navigating is not the moment to argue about a conversion
        # choice. That check belongs on the final Import.
        self.current_step().commit(validate=False)
        self.stack.setCurrentIndex(index)
        self.current_step().load(self.track)
        self._update_nav()

    def next(self) -> None:
        if self._busy or self.track is None:
            return
        if self._commit_retry:
            self.completed.emit(self.track)
            return

        step = self.current_step()
        if not step.commit():
            return

        if self.index == len(self.steps) - 1:
            self._finish()
            return

        self.stack.setCurrentIndex(self.index + 1)
        # Re-load so a later step reflects choices just made.
        if self.track is not None:
            self.current_step().load(self.track)
        self._update_nav()

    def back(self) -> None:
        if self._busy or self._commit_retry:
            return
        if self.index == 0:
            self._on_cancel()
            return
        self.goto(self.index - 1)

    def _update_nav(self) -> None:
        if self._busy:
            return
        if self._commit_retry:
            self.title.setText("WRITE PENDING")
            self.next_button.setText("Retry rekordbox write  →")
            self.next_button.setEnabled(True)
            return

        step = self.current_step()
        self.title.setText(step.title.upper())
        self.subtitle.setText(step.subtitle)
        self.indicator.set_current(self.index)
        self.back_button.setText("←  Back" if self.index else "←  Dashboard")
        last = self.index == len(self.steps) - 1
        self.next_button.setText("Import this track" if last else "Next  →")
        self.next_button.setEnabled(step.is_valid())

    @property
    def commit_retry(self) -> bool:
        return self._commit_retry

    def set_busy(self, message: str) -> None:
        """Lock navigation while filing or applying, keeping progress in context."""
        self._busy = True
        self._commit_retry = False
        self.title.setText("COMMITTING TRACK")
        self.subtitle.setText(message)
        self.indicator.set_navigation_enabled(False)
        self.cancel_button.setEnabled(False)
        self.back_button.setEnabled(False)
        self.next_button.setText("WORKING…")
        self.next_button.setEnabled(False)
        self.commit_progress.setRange(0, 0)
        self.commit_progress.show()

    def set_commit_progress(self, done: int, total: int, message: str) -> None:
        self.subtitle.setText(message)
        self.commit_progress.setRange(0, max(1, total))
        self.commit_progress.setValue(done)

    def set_commit_failed(self, message: str) -> None:
        """Leave the filed track in place and expose a write-only retry action."""
        self._busy = False
        self._commit_retry = True
        self.title.setText("WRITE PENDING")
        self.subtitle.setText(message)
        self.indicator.set_navigation_enabled(False)
        self.cancel_button.setEnabled(False)
        self.back_button.setEnabled(False)
        self.next_button.setText("Retry rekordbox write  →")
        self.next_button.setEnabled(True)
        self.commit_progress.hide()

    def _finish(self) -> None:
        if self.track is None:
            return
        track = self.track

        # Free navigation means the folder step can be skipped past, so check here
        # rather than relying on having walked the steps in order.
        if not track.genre_folder:
            QMessageBox.information(
                self, "Choose a folder first",
                "This track needs a folder before it can be imported.",
            )
            self.goto(1)
            return
        folder = "/".join(p for p in (track.genre_folder, track.subgenre_folder) if p)
        if track.target is None or track.target is KeepOriginal:
            target = "Keep the original format"
        else:
            target = f"Convert to {track.target.label}"

        lines = [
            f"• {target}",
            f"• File into <b>{folder or '—'}</b>",
            f"• Add to <b>{len(track.playlist_ids)}</b> playlist(s)",
        ]
        if self.tags_step in self.steps:
            lines.append(f"• Apply <b>{len(track.my_tag_ids)}</b> tag(s)")
        if self.colour_step in self.steps:
            lines.append(f"• Colour: <b>{'set' if track.colour_id else 'none'}</b>")

        if ask(
            self,
            "Import this track?",
            f"<b>{track.display_name}</b><br><br>"
            + "<br>".join(lines)
            + "<br><br>"
            f"KRATR will file the audio, create a timestamped library backup, and write "
            f"these changes to rekordbox now. rekordbox must remain closed until it finishes.",
            default_yes=True,
        ):
            self.completed.emit(track)

    def _on_cancel(self) -> None:
        if self._busy or self._commit_retry:
            return
        self.cancelled.emit()

    def _on_finished_early(self, action: str) -> None:
        if self.track is None:
            return
        if action == "quarantine":
            self.quarantine_requested.emit(self.track)
        elif action == "discard":
            self.discard_requested.emit(self.track)
