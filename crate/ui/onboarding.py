"""First-run setup walkthrough.

Shown once, the first time KRATR is launched on a machine that has never been
configured — detected by the absence of ``settings.json``. An existing install (one
that already has settings) never sees this, so it is invisible to anyone upgrading.

Five screens, one decision each:

    Welcome → Dependencies → rekordbox → Format → Library folder

On finish it writes ``settings.json``; the main window then loads exactly those
settings. If the friend closes the walkthrough without finishing, nothing is written
and the app exits cleanly so the next launch starts the walkthrough again.

Nothing here writes to the rekordbox library. The rekordbox screen only ever opens a
disposable *copy* of the database, read-only, and tolerates every kind of failure —
a first run on a friend's machine is exactly where the library might not open yet.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..models import AudioFormat
from .branding import application_icon

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- checks
class DependencyProbe(QThread):
    """Confirm the risky dependencies work, off the GUI thread.

    ffmpeg/ffprobe are looked up exactly the way the running app looks them up, so the
    result here matches what conversion and probing will actually find.
    """

    result = Signal(dict)

    def run(self) -> None:  # noqa: D102 - Qt entry point
        report: dict[str, tuple[bool, str]] = {}

        ffmpeg = config.find_executable("ffmpeg")
        report["ffmpeg"] = (bool(ffmpeg), ffmpeg or "not found")
        ffprobe = config.find_executable("ffprobe")
        report["ffprobe"] = (bool(ffprobe), ffprobe or "not found")

        try:
            import pyrekordbox  # noqa: F401

            report["pyrekordbox"] = (True, f"pyrekordbox {pyrekordbox.__version__}")
        except Exception as exc:  # noqa: BLE001 - this is the diagnostic
            report["pyrekordbox"] = (False, f"{type(exc).__name__}: {exc}")

        self.result.emit(report)


class RekordboxProbe(QThread):
    """Detect the rekordbox library and, if possible, open a read-only copy of it.

    Opening the copy is the reassuring proof that KRATR can read the library — but on a
    first run it can also be slow (pyrekordbox may fetch its key) or fail outright.
    Every outcome is reported as data; nothing raises past this thread.
    """

    result = Signal(dict)

    def run(self) -> None:  # noqa: D102
        out: dict[str, object] = {"present": config.REKORDBOX_DB_PATH.is_file()}
        if not out["present"]:
            self.result.emit(out)
            return

        out["path"] = str(config.REKORDBOX_DB_PATH)
        try:
            from ..rekordbox.sandbox import Sandbox

            with Sandbox.create() as sb:
                db = sb.open()
                out["opened"] = True
                out["tracks"] = db.get_content().count()
                out["playlists"] = db.get_playlist().count()
                out["tags"] = db.get_my_tag().count()
        except Exception as exc:  # noqa: BLE001 - a first run is exactly where this fails
            logger.warning("Onboarding rekordbox probe could not open the library", exc_info=True)
            out["opened"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"
        self.result.emit(out)


# --------------------------------------------------------------------------- screens
def _heading(eyebrow: str, title: str, subtitle: str) -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    tag = QLabel(eyebrow)
    tag.setObjectName("eyebrow")
    layout.addWidget(tag)
    head = QLabel(title)
    head.setObjectName("pageTitle")
    layout.addWidget(head)
    sub = QLabel(subtitle)
    sub.setObjectName("muted")
    sub.setWordWrap(True)
    layout.addWidget(sub)
    return box


def _check_row(ok: bool | None, label: str, detail: str) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    mark = QLabel("…" if ok is None else ("✓" if ok else "✕"))
    mark.setObjectName("primary" if ok else ("muted" if ok is None else "danger"))
    mark.setFixedWidth(18)
    layout.addWidget(mark)
    name = QLabel(f"<b>{label}</b>")
    layout.addWidget(name)
    info = QLabel(detail)
    info.setObjectName("muted")
    info.setWordWrap(True)
    layout.addWidget(info, stretch=1)
    return row


class OnboardingDialog(QDialog):
    """The five-screen first-run walkthrough. Returns a saved Settings on accept."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("importWizard")   # reuse the wizard's visual language
        self.setModal(True)
        self.setWindowTitle("Welcome to KRATR")
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setMinimumSize(660, 520)

        self._settings = config.Settings()
        self._library_ok = False
        self._dep_probe: DependencyProbe | None = None
        self._rb_probe: RekordboxProbe | None = None

        self._build()
        self.stack.setCurrentIndex(0)
        self._update_nav()

    # ------------------------------------------------------------------ chrome
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(16)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._welcome_screen())
        self.stack.addWidget(self._dependency_screen())
        self.stack.addWidget(self._rekordbox_screen())
        self.stack.addWidget(self._format_screen())
        self.stack.addWidget(self._features_screen())
        self.stack.addWidget(self._library_screen())
        layout.addWidget(self.stack, stretch=1)

        footer = QHBoxLayout()
        self.progress_label = QLabel()
        self.progress_label.setObjectName("muted")
        footer.addWidget(self.progress_label)
        footer.addStretch(1)
        self.back_button = QPushButton("←  Back")
        self.back_button.clicked.connect(self._back)
        footer.addWidget(self.back_button)
        self.next_button = QPushButton("Next  →")
        self.next_button.setObjectName("primary")
        self.next_button.setMinimumWidth(180)
        self.next_button.clicked.connect(self._next)
        footer.addWidget(self.next_button)
        layout.addLayout(footer)

    @property
    def _last_index(self) -> int:
        return self.stack.count() - 1

    def _back(self) -> None:
        if self.stack.currentIndex() == 0:
            return
        self.stack.setCurrentIndex(self.stack.currentIndex() - 1)
        self._on_enter(self.stack.currentIndex())
        self._update_nav()

    def _next(self) -> None:
        index = self.stack.currentIndex()
        if index == self._last_index:
            self._finish()
            return
        self.stack.setCurrentIndex(index + 1)
        self._on_enter(self.stack.currentIndex())
        self._update_nav()

    def _on_enter(self, index: int) -> None:
        # Kick off the live checks the first time their screen is shown.
        if index == 1 and self._dep_probe is None:
            self._start_dependency_probe()
        elif index == 2 and self._rb_probe is None:
            self._start_rekordbox_probe()

    def _update_nav(self) -> None:
        index = self.stack.currentIndex()
        self.progress_label.setText(f"Step {index + 1} of {self.stack.count()}")
        self.back_button.setEnabled(index > 0)
        if index == self._last_index:
            self.next_button.setText("Finish  ✓")
            self.next_button.setEnabled(self._library_ok)
        else:
            self.next_button.setText("Next  →")
            self.next_button.setEnabled(True)

    # ------------------------------------------------------------------ 1. welcome
    def _welcome_screen(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(16)
        layout.addWidget(
            _heading(
                "KRATR / FIRST RUN",
                "Let's get you set up",
                "A quick, one-time walkthrough — a couple of checks and two choices, "
                "then you're ready to drop your first track.",
            )
        )

        promise = QFrame()
        promise.setObjectName("summaryBox")
        promise_layout = QVBoxLayout(promise)
        promise_layout.setSpacing(8)
        title = QLabel("KRATR is safe to point at your real library")
        title.setObjectName("panelTitle")
        promise_layout.addWidget(title)
        for line in (
            "It <b>never writes while rekordbox is running</b> — a hard block, not a warning.",
            "It <b>backs up your library before every write</b>, sidecars and all.",
            "Existing tracks are <b>relocated, never re-imported</b>, so ratings, cues and "
            "playlists survive by construction.",
        ):
            bullet = QLabel(f"•  {line}")
            bullet.setWordWrap(True)
            promise_layout.addWidget(bullet)
        layout.addWidget(promise)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ 2. deps
    def _dependency_screen(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)
        layout.addWidget(
            _heading(
                "KRATR / CHECK",
                "Making sure everything works",
                "KRATR needs ffmpeg to read and convert audio, and pyrekordbox to talk to "
                "your library. These ship with KRATR — this is just confirming they run here.",
            )
        )
        self.dep_rows = QVBoxLayout()
        self.dep_rows.setSpacing(10)
        layout.addLayout(self.dep_rows)
        self.dep_note = QLabel()
        self.dep_note.setObjectName("muted")
        self.dep_note.setWordWrap(True)
        layout.addWidget(self.dep_note)
        layout.addStretch(1)
        self._render_dep_rows(None)
        return page

    def _render_dep_rows(self, report: dict | None) -> None:
        while self.dep_rows.count():
            item = self.dep_rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        labels = {
            "ffmpeg": "ffmpeg (audio conversion)",
            "ffprobe": "ffprobe (audio inspection)",
            "pyrekordbox": "pyrekordbox (rekordbox library)",
        }
        for key, label in labels.items():
            if report is None:
                self.dep_rows.addWidget(_check_row(None, label, "checking…"))
            else:
                ok, detail = report[key]
                self.dep_rows.addWidget(_check_row(ok, label, detail))

    def _start_dependency_probe(self) -> None:
        self._render_dep_rows(None)
        self.dep_note.setText("")
        # Parent to the application, not the dialog: a probe must never be a child that
        # gets destroyed while its thread is still running (e.g. a fast Finish click).
        self._dep_probe = DependencyProbe(QApplication.instance())
        self._dep_probe.result.connect(self._on_dependencies)
        self._dep_probe.start()

    def _on_dependencies(self, report: dict) -> None:
        self._render_dep_rows(report)
        if all(ok for ok, _ in report.values()):
            self.dep_note.setText("All good. You can continue.")
        else:
            self.dep_note.setText(
                "Something didn't check out. You can still continue and sort it later — "
                "KRATR will tell you exactly what's missing when it needs it."
            )

    # ------------------------------------------------------------------ 3. rekordbox
    def _rekordbox_screen(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)
        layout.addWidget(
            _heading(
                "KRATR / LIBRARY",
                "Finding your rekordbox library",
                "KRATR reads the same library rekordbox uses. It only ever opens a "
                "disposable copy here — your real database is not touched.",
            )
        )
        self.rb_status = QLabel("Looking for your rekordbox library…")
        self.rb_status.setObjectName("muted")
        self.rb_status.setWordWrap(True)
        layout.addWidget(self.rb_status)
        self.rb_detail = QLabel()
        self.rb_detail.setWordWrap(True)
        layout.addWidget(self.rb_detail)
        layout.addStretch(1)
        return page

    def _start_rekordbox_probe(self) -> None:
        self.rb_status.setText("Looking for your rekordbox library…")
        self.rb_detail.setText("")
        self._rb_probe = RekordboxProbe(QApplication.instance())
        self._rb_probe.result.connect(self._on_rekordbox)
        self._rb_probe.start()

    def _on_rekordbox(self, out: dict) -> None:
        if not out.get("present"):
            self.rb_status.setText("No rekordbox library found yet.")
            self.rb_detail.setText(
                "That's fine — install and run rekordbox at least once, then KRATR will "
                "pick it up automatically. You can still set up the rest now."
            )
            return
        if out.get("opened"):
            self.rb_status.setText("✓  Found your rekordbox library, and KRATR can read it.")
            self.rb_detail.setText(
                f"<b>{out.get('tracks', 0)}</b> tracks · "
                f"<b>{out.get('playlists', 0)}</b> playlists · "
                f"<b>{out.get('tags', 0)}</b> MyTags"
            )
        else:
            self.rb_status.setText("Found your rekordbox library.")
            self.rb_detail.setText(
                "KRATR couldn't fully open it just yet — that can sort itself out on the "
                "next launch, and KRATR always checks before it writes. You can continue."
            )

    # ------------------------------------------------------------------ 4. format
    def _format_screen(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)
        layout.addWidget(
            _heading(
                "KRATR / FORMAT",
                "Your preferred format",
                "When you bring in a lossless track, what should KRATR convert it to? "
                "MP3s are always left as they are. You can change this any time in Settings.",
            )
        )
        self.format_group = QButtonGroup(self)
        options = [
            ("wav", "WAV", "Uncompressed, universally CDJ-safe. The usual choice."),
            ("aiff", "AIFF", "Uncompressed with richer tag support; larger metadata."),
            ("keep", "Keep original", "Don't convert — file lossless tracks as they arrive."),
        ]
        for value, label, blurb in options:
            radio = QRadioButton(f"  {label}")
            radio.setProperty("format_value", value)
            if value == "wav":
                radio.setChecked(True)
            self.format_group.addButton(radio)
            layout.addWidget(radio)
            hint = QLabel(f"      {blurb}")
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        layout.addStretch(1)
        return page

    def _chosen_format(self) -> str:
        button = self.format_group.checkedButton()
        return button.property("format_value") if button else "wav"

    # ------------------------------------------------------------------ 5. features
    def _features_screen(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)
        layout.addWidget(
            _heading(
                "KRATR / WHAT TO INCLUDE",
                "Tags and colours — your call",
                "These two are personal, so they're entirely optional. Pick what you want "
                "now; you can change either one any time in Settings.",
            )
        )

        self.tags_checkbox = QCheckBox("  Tag my tracks during import")
        self.tags_checkbox.setChecked(True)
        layout.addWidget(self.tags_checkbox)
        tags_hint = QLabel(
            "      KRATR suggests a starter set of tags — genres pulled from your own "
            "folders, plus vibe / setting / format words to edit. Rename them, replace "
            "them, or ignore them. Turn this off to skip tagging completely."
        )
        tags_hint.setObjectName("muted")
        tags_hint.setWordWrap(True)
        layout.addWidget(tags_hint)

        layout.addSpacing(6)

        self.colour_checkbox = QCheckBox("  Colour-code my tracks")
        self.colour_checkbox.setChecked(False)
        layout.addWidget(self.colour_checkbox)
        colour_hint = QLabel(
            "      Colours let you read a track's energy — or anything you choose — at a "
            "glance on a CDJ. Plenty of DJs never use them, so this is off unless you "
            "want it. It adds a colour page to each import."
        )
        colour_hint.setObjectName("muted")
        colour_hint.setWordWrap(True)
        layout.addWidget(colour_hint)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ 6. library
    def _library_screen(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)
        layout.addWidget(
            _heading(
                "KRATR / DESTINATION",
                "Where your music lives",
                "Choose the top folder of your genre library — the one that holds your "
                "House, Techno, Disco… folders. Filed tracks go into it, sorted by genre.",
            )
        )
        row = QHBoxLayout()
        self.library_field = QLineEdit()
        self.library_field.setPlaceholderText("e.g. D:\\Music")
        self.library_field.textChanged.connect(self._on_library_changed)
        row.addWidget(self.library_field, stretch=1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_library)
        row.addWidget(browse)
        layout.addLayout(row)
        self.library_note = QLabel("Pick a folder to finish.")
        self.library_note.setObjectName("muted")
        self.library_note.setWordWrap(True)
        layout.addWidget(self.library_note)
        layout.addStretch(1)
        return page

    def _browse_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose your music library folder")
        if folder:
            self.library_field.setText(folder)

    def _on_library_changed(self, text: str) -> None:
        from pathlib import Path

        candidate = text.strip()
        self._library_ok = bool(candidate) and Path(candidate).is_dir()
        if not candidate:
            self.library_note.setText("Pick a folder to finish.")
        elif self._library_ok:
            self.library_note.setText("✓  That folder exists — you're ready to finish.")
        else:
            self.library_note.setText("That folder doesn't exist yet. Pick one that does.")
        self._update_nav()

    # ------------------------------------------------------------------ finish
    def _apply_format_choice(self) -> None:
        choice = self._chosen_format()
        if choice == "keep":
            self._settings.default_target = None
            self._settings.conversion_rules = {fmt.value: None for fmt in AudioFormat}
            return
        target = AudioFormat.WAV if choice == "wav" else AudioFormat.AIFF
        self._settings.default_target = target.value
        # Normalise every lossless source to the chosen container; never touch MP3, and
        # never pointlessly re-encode something already in the target format.
        rules: dict[str, str | None] = {}
        for fmt in AudioFormat:
            if fmt is AudioFormat.MP3 or fmt is target:
                rules[fmt.value] = None
            elif fmt.is_lossless:
                rules[fmt.value] = target.value
            else:
                rules[fmt.value] = None
        self._settings.conversion_rules = rules

    def _finish(self) -> None:
        self._settings.music_root = self.library_field.text().strip()
        self._settings.use_tags = self.tags_checkbox.isChecked()
        self._settings.use_colours = self.colour_checkbox.isChecked()
        self._apply_format_choice()
        try:
            self._settings.save()
        except OSError:
            logger.exception("Could not write settings during onboarding")
        self.accept()

    @property
    def settings(self) -> config.Settings:
        return self._settings


def run_first_run(app: QApplication) -> bool:
    """Show the walkthrough. Returns True if the user completed and settings were saved.

    The app is expected to already carry KRATR's stylesheet and icon so the dialog
    matches the rest of the UI.
    """
    dialog = OnboardingDialog()
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return accepted
