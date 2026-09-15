"""Main window: player on top, dashboard and import wizard below.

Structure is deliberately flat — the player bar is a sibling of a two-page stack, so
playback continues untouched while the wizard swaps steps beneath it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..models import QualityReport, SourceInfo, Track, TrackState
from ..pipeline import convert as convert_mod
from ..pipeline import filing, trash
from ..rekordbox import colours as colours_mod
from ..rekordbox import handlers, importer, mytags, session as session_mod, unsorted
from ..rekordbox.queue import ApplyResult, OpKind, OpQueue
from ..rekordbox.reader import LibraryReader
from .apply_dialog import ApplyDialog, ApplyTask
from .branding import application_icon
from .dashboard import Dashboard
from .dialogs import ask
from .player import PlayerBar
from .settings import SettingsPage
from .wizard import ImportWizard
from .workers import AnalysisTask, FilingTask, QuarantineTask

logger = logging.getLogger(__name__)


class _UpdateCheckThread(QThread):
    """Run the GitHub version check off the GUI thread; emit only if a newer one exists."""

    found = Signal(object)   # update.UpdateInfo

    def run(self) -> None:  # noqa: D102 - Qt entry point
        from .. import update

        info = update.check()
        if info is not None:
            self.found.emit(info)


class MainWindow(QMainWindow):
    def __init__(self, settings: config.Settings, *, commit_on_import: bool = True) -> None:
        super().__init__()
        self.settings = settings
        self.commit_on_import = commit_on_import
        self.library = filing.MusicLibrary(settings.music_root)
        self.pool = QThreadPool.globalInstance()
        # Each analysis spawns ffmpeg several times; letting one per core loose at
        # once starves the GUI thread when a whole folder arrives.
        self.pool.setMaxThreadCount(max(2, min(4, (os.cpu_count() or 4) // 2)))
        self.tracks: list[Track] = []
        self._pending_analysis = 0
        self._analysis_total = 0
        self._dashboard_dirty = False

        self.reader = LibraryReader()
        self.processed = unsorted.ProcessedLog()
        self.op_queue = OpQueue().load()
        handlers.register_all(self.op_queue)

        self.setWindowTitle(config.APP_NAME)
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(1360, 900)
        self.setMinimumSize(1040, 720)
        self._build()
        self._load_library()
        self._refresh_dashboard()
        self._start_update_check()

    # ------------------------------------------------------------ update check
    def _start_update_check(self) -> None:
        """Ask GitHub (once, in the background) whether a newer KRATR exists."""
        if not getattr(self.settings, "check_for_updates", True):
            return
        # Parented to the application, never the window, so the short-lived thread is
        # never torn down mid-request. A failure inside it is swallowed (notify-only).
        self._update_thread = _UpdateCheckThread(QApplication.instance())
        self._update_thread.found.connect(self._on_update_available)
        self._update_thread.start()

    def _on_update_available(self, info: object) -> None:
        from .. import __version__

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Update available")
        box.setText(f"KRATR {info.version} is available — you're on {__version__}.")
        box.setInformativeText("Open the download page in your browser?")
        download = box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is download:
            QDesktopServices.openUrl(QUrl(info.url))

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.player = PlayerBar()
        layout.addWidget(self.player)

        self.stack = QStackedWidget()
        self.dashboard = Dashboard()
        self.dashboard.files_added.connect(self.add_files)
        self.dashboard.play_requested.connect(self.play_track)
        self.dashboard.import_requested.connect(self.start_import)
        self.dashboard.delete_requested.connect(self._delete_file)
        self.dashboard.settings_requested.connect(self.show_settings)
        self.stack.addWidget(self.dashboard)

        self.wizard = ImportWizard(
            self.library,
            use_tags=self.settings.use_tags,
            use_colours=self.settings.use_colours,
        )
        self.wizard.cancelled.connect(self.show_dashboard)
        self.wizard.completed.connect(self._on_wizard_complete)
        self.wizard.quarantine_requested.connect(self._quarantine)
        self.wizard.discard_requested.connect(self._discard)
        self.wizard.taxonomy_edit.connect(self._queue_taxonomy_edit)
        self.stack.addWidget(self.wizard)

        self.settings_page = SettingsPage(self.library, self.settings)
        self.settings_page.close_requested.connect(self.show_dashboard)
        self.settings_page.taxonomy_edit.connect(self._queue_taxonomy_edit)
        self.settings_page.library_edit.connect(self._queue_library_edit)
        self.settings_page.preferences_saved.connect(self._on_preferences_saved)
        self.settings_page.apply_requested.connect(self._apply_from_settings)
        self.settings_page.suggest_taxonomy.connect(self._suggest_starter_tags)
        self.stack.addWidget(self.settings_page)
        layout.addWidget(self.stack, stretch=1)

        self.setCentralWidget(central)

        status = QStatusBar()
        self.library_label = QLabel()
        self.library_label.setObjectName("muted")
        status.addWidget(self.library_label)

        self.setStatusBar(status)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.player.toggle)

    # -------------------------------------------------------------- library
    def _load_library(self, *, clear_staged: bool = False) -> None:
        if not self.reader.refresh():
            self.library_label.setText(f"rekordbox unavailable — {self.reader.error}")
            self._refresh_pending()
            return

        counts = self.reader.counts()
        self.library_label.setText(
            f"{counts['tracks']} tracks · {counts['playlists']} playlists · "
            f"{counts['my_tags']} MyTags"
        )
        playlist_tree = self.reader.playlist_tree()
        self.wizard.playlist_step.set_tree(playlist_tree)
        self.settings_page.set_playlist_tree(playlist_tree, clear_staged=clear_staged)
        self._categories = mytags.categories(self.reader.db)
        self._colours = colours_mod.colours(self.reader.db)
        self.wizard.tags_step.set_categories(self._categories)
        self.settings_page.set_categories(self._categories)
        self.wizard.colour_step.set_colours(self._colours)
        self.settings_page.set_colours(self._colours)
        self.settings_page.refresh_library_views(clear_staged=clear_staged)
        self._refresh_pending()

    def _refresh_pending(self) -> None:
        pending = self.op_queue.pending
        tag_descriptions = [op.label for op in pending if op.kind == OpKind.TAG_MANAGER]
        # The queue is the single source of truth for what is still unwritten, so the
        # tags banner is rebuilt from it every time rather than kept in step by hand.
        self.wizard.tags_step.set_pending(tag_descriptions)
        self.settings_page.set_pending(
            [
                op.label for op in pending
                if op.kind in {OpKind.TAG_MANAGER, OpKind.LIBRARY_MANAGER}
            ],
            tag_descriptions,
            folder_edit_pending=any(
                op.kind == OpKind.LIBRARY_MANAGER
                and op.payload.get("action") == "move_folder"
                for op in pending
            ),
        )

    # --------------------------------------------------------------- tracks
    def add_files(self, paths: list[Path]) -> None:
        known = {t.source_path for t in self.tracks}
        added = 0
        for path in paths:
            if path in known:
                continue
            known.add(path)
            track = Track(source_path=path, state=TrackState.ANALYSING)
            self.tracks.append(track)
            added += 1

            task = AnalysisTask(track, self.settings)
            task.signals.finished.connect(self._on_analysed)
            task.signals.failed.connect(self._on_failed)
            self.pool.start(task)

        if added:
            self._pending_analysis += added
            self._analysis_total += added
            self._refresh_progress()
            self.statusBar().showMessage(f"Analysing {added} track(s)…")
        self._refresh_dashboard()

    def _refresh_dashboard(self, immediate: bool = True) -> None:
        self._dashboard_dirty = False
        self.dashboard.set_tracks(self.tracks)

    def _schedule_dashboard_refresh(self) -> None:
        """Coalesce refreshes while a batch is being analysed.

        Rebuilding the table on every completed analysis is O(n²) — dropping a
        135-track folder meant ~18,000 row builds plus a duplicate-detection pass per
        track, all on the GUI thread, which locked the app solid. One redraw every
        250 ms keeps it responsive and looks identical.
        """
        if self._dashboard_dirty:
            return
        self._dashboard_dirty = True
        QTimer.singleShot(250, self._flush_dashboard_refresh)

    def _flush_dashboard_refresh(self) -> None:
        if self._dashboard_dirty:
            self._refresh_dashboard()

    def play_track(self, track: Track) -> None:
        self.player.load(track, autoplay=True)

    def _on_analysed(self, track: Track, info: SourceInfo, report: QualityReport) -> None:
        track.info = info
        track.quality = report
        track.state = TrackState.READY

        if self.reader.available:
            try:
                track.library_status, track.existing = self.reader.classify(info, self.library)
            except Exception:  # noqa: BLE001 - a lookup must never block intake
                logger.warning("Library lookup failed for %s", info.path.name, exc_info=True)

        if track.existing:
            # Pre-select what the track already has. Tags matter most: applying them
            # REPLACES a track's set, so without this, re-importing would strip them.
            track.colour_id = track.existing.colour_id
            track.my_tag_ids = list(track.existing.my_tag_ids)
            track.playlist_ids = list(track.existing.playlist_ids)

        plan = convert_mod.plan_from_settings(info, self.settings, is_suspect=report.is_suspect)
        track.target = plan.target
        self._analysis_finished()

    def _on_failed(self, track: Track, message: str) -> None:
        track.state = TrackState.FAILED
        track.error = message
        self._analysis_finished()
        self.statusBar().showMessage(message, 8000)

    def _refresh_progress(self) -> None:
        done = self._analysis_total - self._pending_analysis
        self.dashboard.set_analysis_progress(done, self._analysis_total)

    def _analysis_finished(self) -> None:
        """One track done: update progress, and redraw at most a few times a second."""
        self._pending_analysis = max(0, self._pending_analysis - 1)

        if self._pending_analysis:
            done = self._analysis_total - self._pending_analysis
            self.statusBar().showMessage(f"Analysing… {done} of {self._analysis_total} done")
        else:
            # Batch finished — reset so the next drop starts its own count.
            self.statusBar().showMessage(
                f"Analysed {self._analysis_total} track(s)", 5000
            )
            self._analysis_total = 0

        self._refresh_progress()
        self._schedule_dashboard_refresh()

    # ---------------------------------------------------------------- wizard
    def start_import(self, track: Track) -> None:
        if track.info is None:
            return
        # Load it into the player too — you're about to decide what it is.
        if self.player.current_track is not track:
            self.player.load(track, autoplay=False)
        self.wizard.start(track)
        self.stack.setCurrentWidget(self.wizard)

    def show_dashboard(self) -> None:
        self.stack.setCurrentWidget(self.dashboard)
        self._refresh_dashboard()

    def bring_to_front(self) -> None:
        """Restore and focus the existing window after a second shortcut launch."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def show_settings(self) -> None:
        self.settings_page.general_page.load()
        self.stack.setCurrentWidget(self.settings_page)

    def _apply_from_settings(self) -> None:
        """Write staged taxonomy edits without requiring a track import."""
        dialog = ApplyDialog(self.op_queue, self.settings, self)
        dialog.exec()
        if dialog.result_obj is None:
            return

        self.op_queue.clear_applied()
        self._load_library(clear_staged=dialog.result_obj.ok)
        self._refresh_pending()
        self.statusBar().showMessage(dialog.result_obj.summary(), 8000)

    def _on_wizard_complete(self, track: Track) -> None:
        if self.commit_on_import and not self._commit_preflight():
            return

        # A failed database write is retried directly. The audio was already filed,
        # so running the conversion/move stage again would duplicate the track.
        if self.wizard.commit_retry:
            self._start_apply(track)
            return

        plan = self.wizard.quality_step.current_plan()
        if plan is None or not track.genre_folder:
            return

        # The player has the file open, and Windows won't let an open file be moved.
        # Remember whether it was loaded so it can be re-pointed at the new location.
        self._reload_player_for = track if self.player.release(track.source_path) else None

        track.state = TrackState.CONVERTING
        self._refresh_dashboard()
        if self.commit_on_import:
            self.wizard.set_busy(f"FILING AUDIO / {track.display_name}")
        else:
            self.show_dashboard()
        self.statusBar().showMessage(f"Filing {track.display_name}…")

        task = FilingTask(track, plan, self.library, self.settings)
        task.signals.finished.connect(self._on_filed)
        task.signals.failed.connect(self._on_filing_failed)
        self.pool.start(task)

    def _on_filing_failed(self, track: Track, message: str) -> None:
        track.state = TrackState.FAILED
        track.error = message
        self._refresh_dashboard()
        self.show_dashboard()
        self.statusBar().showMessage(message, 8000)
        QMessageBox.warning(
            self,
            "Could not file this track",
            message + "\n\nThe rekordbox library was not changed.",
        )

    def _on_filed(self, track: Track, final: Path) -> None:
        track.state = TrackState.FILED
        track.final_path = final
        if getattr(self, "_reload_player_for", None) is track:
            self.player.reload_at(track)
            self._reload_player_for = None
        queued = self._queue_rekordbox_ops(track)
        if queued:
            track.state = TrackState.QUEUED
        self._refresh_dashboard()
        self._refresh_pending()

        if self.commit_on_import and queued:
            self._start_apply(track)
            return

        self.statusBar().showMessage(
            f"Filed → {final}" + (f"   ({queued} change(s) staged)" if queued else ""), 8000
        )

    def _commit_preflight(self) -> bool:
        """Require a closed rekordbox and an available library before moving audio."""
        if session_mod.is_rekordbox_running():
            QMessageBox.information(
                self,
                "Close rekordbox first",
                "Close rekordbox, then click Import this track again.\n\n"
                "KRATR now files the audio and writes the rekordbox changes as one "
                "continuous operation.",
            )
            return False
        if not config.REKORDBOX_DB_PATH.is_file():
            QMessageBox.warning(
                self,
                "rekordbox library unavailable",
                f"KRATR cannot find the rekordbox library at:\n{config.REKORDBOX_DB_PATH}",
            )
            return False
        return True

    def _start_apply(self, track: Track) -> None:
        """Apply all staged work immediately, as the final phase of this import."""
        self.wizard.set_busy("BACKING UP LIBRARY / PREPARING REKORDBOX WRITE")
        self.statusBar().showMessage("Backing up and writing changes to rekordbox…")

        task = ApplyTask(self.op_queue, self.settings)
        task.signals.progress.connect(self._on_apply_progress)
        task.signals.finished.connect(
            lambda result, current=track: self._on_apply_finished(current, result)
        )
        task.signals.failed.connect(
            lambda message, current=track: self._on_apply_failed(current, message)
        )
        # Keep the Python wrapper alive while QRunnable is owned by the C++ pool.
        # Without this, a fast filing→apply handoff can lose the signal object before
        # the worker reports completion, leaving the wizard stuck on "COMMITTING".
        self._apply_task = task
        self.pool.start(task)

    def _on_apply_progress(self, index: int, total: int, label: str) -> None:
        self.wizard.set_commit_progress(index, total, f"REKORDBOX WRITE {index}/{total} / {label}")

    def _on_apply_finished(self, track: Track, result: ApplyResult) -> None:
        self._apply_task = None
        self.op_queue.clear_applied()
        self._refresh_pending()

        if not result.ok:
            track.state = TrackState.QUEUED
            detail = result.summary()
            track.error = detail
            self.wizard.set_commit_failed(
                "AUDIO FILED SAFELY / REKORDBOX WRITE NEEDS RETRY"
            )
            self.statusBar().showMessage(detail)
            return

        self._finished_this_apply = []
        cleared = self._clear_applied_tracks()
        self._load_library()
        self._record_processed(self._finished_this_apply)
        self._refresh_dashboard()
        self.show_dashboard()
        self.statusBar().showMessage(
            f"Import complete — {cleared or 1} track(s) filed and written to rekordbox.",
            8000,
        )

    def _on_apply_failed(self, track: Track, message: str) -> None:
        self._apply_task = None
        track.state = TrackState.QUEUED
        track.error = message
        self._refresh_dashboard()
        self.wizard.set_commit_failed("AUDIO FILED SAFELY / REKORDBOX WRITE NEEDS RETRY")
        self.statusBar().showMessage(message)

    def _quarantine(self, track: Track) -> None:
        if not ask(
            self, "Move to _Re-source?",
            f"{track.display_name}\n\nIt will be moved to "
            f"{self.library.root / self.settings.quarantine_folder} — out of the library — "
            f"and added to your re-source list.\n\nContinue?",
            default_yes=True,
        ):
            return

        self.player.release(track.source_path)   # same open-file constraint as filing
        self.show_dashboard()
        task = QuarantineTask(track, self.library, self.settings)
        task.signals.finished.connect(self._on_quarantined)
        task.signals.failed.connect(self._on_failed)
        self.pool.start(task)

    def _on_quarantined(self, track: Track, final: Path) -> None:
        track.state = TrackState.QUARANTINED
        track.final_path = final
        self._refresh_dashboard()
        self.statusBar().showMessage(f"Quarantined → {final}  (added to the re-source list)", 8000)

    def _delete_file(self, track: Track) -> None:
        """Delete a track's file from disk, after letting go of it.

        The dashboard has already confirmed. Windows refuses to delete a file the
        player still has open, so the handle is released first.
        """
        path = track.final_path or track.source_path
        self.player.release(path)

        result = trash.delete([path])
        if result.ok:
            self.tracks[:] = [t for t in self.tracks if t is not track]
            self.dashboard.confirm_deleted(track, path)
            self.statusBar().showMessage(
                f"Deleted {path.name} — recoverable from the Recycle Bin", 8000
            )
        else:
            self.dashboard.report_delete_failure(result.failed)

    def _discard(self, track: Track) -> None:
        if ask(
            self, "Discard this track?",
            f"Remove {track.display_name} from the list?\n\n"
            f"The file itself is left exactly where it is — nothing is deleted.",
            default_yes=True,
        ):
            if track in self.tracks:
                self.tracks.remove(track)
            self.show_dashboard()

    # --------------------------------------------------------------- queueing
    def _queue_library_edit(self, action: str, args: dict, description: str) -> None:
        self.op_queue.add(
            OpKind.LIBRARY_MANAGER, {"action": action, "args": args}, description
        )
        self._refresh_pending()
        self.statusBar().showMessage(
            f"Staged: {description} — close rekordbox, then write it from Settings.",
            7000,
        )

    def _queue_taxonomy_edit(self, action: str, args: dict, description: str) -> None:
        self.op_queue.add(
            OpKind.TAG_MANAGER, {"action": action, "args": args}, description
        )
        # Both contexts share the same in-memory models. Rebuilding the hidden one
        # keeps Settings edits immediately available in the track wizard.
        if hasattr(self, "_categories"):
            self.wizard.tags_step.set_categories(self._categories)
            self.settings_page.set_categories(self._categories)
        if hasattr(self, "_colours"):
            self.wizard.colour_step.set_colours(self._colours)
            self.settings_page.set_colours(self._colours)
        QTimer.singleShot(0, self._sync_taxonomy_views)
        self._refresh_pending()
        self.statusBar().showMessage(
            f"Staged: {description} — write it from Settings or with the next import.", 6000
        )

    def _sync_taxonomy_views(self) -> None:
        """Reflect optimistic edits after the originating widget finishes its slot."""
        if hasattr(self, "_categories"):
            self.wizard.tags_step.set_categories(self._categories)
            self.settings_page.set_categories(self._categories)
        if hasattr(self, "_colours"):
            self.wizard.colour_step.set_colours(self._colours)
            self.settings_page.set_colours(self._colours)

    def _suggest_starter_tags(self) -> None:
        """Offer a generic starter taxonomy — built entirely locally, fully editable.

        Genres are read from the user's own folder tree; the vibe / setting / format
        words are a generic curated starting point. No network, no API. Nothing is
        written until they stage and apply it, and every tag can be renamed, deleted or
        cleared afterwards like any other.
        """
        from PySide6.QtWidgets import (
            QCheckBox,
            QDialog,
            QDialogButtonBox,
            QScrollArea,
        )

        from ..rekordbox import taxonomy

        genres: list[str] = []
        subfolders: dict[str, list[str]] = {}
        if self.library.exists():
            genres = self.library.genres()
            subfolders = {g: self.library.subgenres(g) for g in genres}

        playlist_names: list[str] = []
        if self.reader.available:
            def walk(nodes: list) -> None:
                for node in nodes:
                    playlist_names.append(node.name)
                    walk(node.children)
            walk(self.reader.playlist_tree())

        proposal = taxonomy.propose(genres, playlist_names, subfolders)

        dialog = QDialog(self)
        dialog.setWindowTitle("Suggest a starter set of tags")
        dialog.resize(560, 560)
        layout = QVBoxLayout(dialog)

        blurb = QLabel(
            "A generic starting point, worked out entirely on your machine — no "
            "internet. The <b>genres</b> come from your own folders; the rest are common "
            "DJ words. Nothing is written yet, and you can rename, delete or clear any "
            "of it afterwards."
        )
        blurb.setObjectName("muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        for category in proposal:
            head = QLabel(f"<b>{category.name}</b>  ({len(category.tags)})")
            inner_layout.addWidget(head)
            body = QLabel(", ".join(category.tags) if category.tags else "— none found yet —")
            body.setObjectName("muted")
            body.setWordWrap(True)
            inner_layout.addWidget(body)
        inner_layout.addStretch(1)
        scroll.setWidget(inner)
        layout.addWidget(scroll, stretch=1)

        replace = QCheckBox("Replace the current tags in these four slots")
        replace.setChecked(True)
        replace.setToolTip(
            "On a fresh setup there's nothing to lose. Untick to keep any tags you've "
            "already made and just add these alongside them."
        )
        layout.addWidget(replace)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Stage these tags")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        categories = [{"name": c.name, "tags": list(c.tags)} for c in proposal]
        self._queue_taxonomy_edit(
            "apply_taxonomy",
            {"categories": categories, "clear_existing": replace.isChecked()},
            "apply suggested starter tags",
        )

    def _on_preferences_saved(self) -> None:
        """Make path/default changes live without restarting KRATR."""
        self.library.root = Path(self.settings.music_root)
        self.wizard.folder_step.navigator.refresh()
        self.settings_page.folder_page.refresh_management()
        self.statusBar().showMessage("Preferences saved.", 5000)

    def _queue_rekordbox_ops(self, track: Track) -> int:
        if not track.final_path:
            return 0
        payload = handlers.payload_for_track(track)
        name = track.display_name
        queued = 0

        if track.is_in_library and track.existing:
            self.op_queue.add(
                OpKind.RELOCATE,
                {**payload, "content_id": track.existing.content_id},
                f"relocate {name}",
            )
        else:
            self.op_queue.add(OpKind.ADD_CONTENT, payload, f"import {name}")
        queued += 1

        if track.playlist_ids:
            self.op_queue.add(
                OpKind.ADD_TO_PLAYLIST,
                {"path": payload["path"], "playlist_ids": track.playlist_ids},
                f"{name} → {len(track.playlist_ids)} playlist(s)",
            )
            queued += 1

        if track.my_tag_ids:
            self.op_queue.add(
                OpKind.SET_MYTAGS,
                {"path": payload["path"], "tag_ids": track.my_tag_ids},
                f"tag {name}",
            )
            queued += 1

        if track.colour_id:
            self.op_queue.add(
                OpKind.SET_COLOUR,
                {"path": payload["path"], "colour_id": track.colour_id},
                f"colour {name}",
            )
            queued += 1

        if self.settings.mirror_tags_to_comments and (track.my_tag_ids or track.flags):
            self.op_queue.add(
                OpKind.SET_COMMENT,
                {
                    "path": payload["path"],
                    "tag_names": self._tag_names(track.my_tag_ids),
                    "extra_tokens": list(track.flags),
                    "prefix": self.settings.comment_tag_prefix,
                },
                f"comment {name}",
            )
            queued += 1

        return queued

    def _tag_names(self, tag_ids: list[str]) -> list[str]:
        if not self.reader.available:
            return []
        lookup = {t.id: t.name for t in mytags.all_tags(self.reader.db)}
        return [lookup[t] for t in tag_ids if t in lookup]

    def _paths_still_queued(self) -> set[str]:
        """Files that still have unapplied work against them."""
        return {
            importer.normalise_path(op.payload["path"])
            for op in self.op_queue.pending
            if op.payload.get("path")
        }

    def _clear_applied_tracks(self) -> int:
        """Drop tracks whose rekordbox changes have landed.

        Once a track is in rekordbox it is finished, and leaving it on the list just
        buries the tracks still waiting. A track is only removed when *nothing* for it
        is still queued — if one of its operations failed, it stays put so the failure
        stays visible.
        """
        still_queued = self._paths_still_queued()
        finished = []

        for track in self.tracks:
            if track.state not in (TrackState.QUEUED, TrackState.FILED):
                continue
            if track.final_path is None:
                continue
            if importer.normalise_path(track.final_path) in still_queued:
                continue
            track.state = TrackState.APPLIED
            finished.append(track)

        # Identity, not equality: Track is a dataclass, so two similar tracks compare
        # equal and `remove()` could take out the wrong one.
        self.tracks[:] = [t for t in self.tracks if not any(t is f for f in finished)]
        self._finished_this_apply = finished
        return len(finished)

    def _record_processed(self, tracks: list[Track]) -> None:
        """Remember what has been through KRATR, so 'Surprise me' won't offer it again."""
        if not tracks:
            return
        for track in tracks:
            content_id = None
            if self.reader.available and track.final_path:
                try:
                    content = importer.find_by_path(self.reader.db, track.final_path)
                    content_id = str(content.ID) if content is not None else None
                except Exception:  # noqa: BLE001 - the log is a convenience, not critical
                    logger.debug("Could not resolve content ID", exc_info=True)
            self.processed.record(content_id, track.final_path)
        self.processed.save()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.player.cleanup()
        self.reader.close()
        super().closeEvent(event)
