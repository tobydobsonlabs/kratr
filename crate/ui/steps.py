"""The five import steps: Quality → Folder → Playlists → Tags → Colour.

One decision per screen. Each step reads and writes the shared :class:`Track`, so
going back and forward preserves everything already chosen.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..models import AudioFormat, KeepOriginal, LibraryStatus, Track
from ..pipeline import convert as convert_mod
from ..pipeline import quality as quality_mod
from ..pipeline.filing import MusicLibrary, sanitise_folder_name
from ..rekordbox import colours as colours_mod
from ..rekordbox import mytags
from .browser import Crumb, Navigator, Node
from .dialogs import ask
from .flow_layout import FlowWidget
from .layout_utils import clear_layout
from .spectrogram import SpectrogramView

logger = logging.getLogger(__name__)


class Step(QWidget):
    """Base for a wizard step."""

    #: Emitted when the step wants the wizard to leave early (quarantine / discard).
    finished_early = Signal(str)   # "quarantine" | "discard"
    #: Emitted when validity changes, so the wizard can enable Next.
    validity_changed = Signal(bool)

    title = ""
    subtitle = ""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.track: Track | None = None

    def load(self, track: Track) -> None:
        self.track = track

    def commit(self, validate: bool = True) -> bool:
        """Save this step's choices onto the track.

        Writes to the in-memory track only — nothing reaches rekordbox until Import.
        ``validate=False`` is used when simply navigating between steps, so moving
        around never triggers a confirmation dialog. Return False to block moving on.
        """
        return True

    def is_valid(self) -> bool:
        return True


# ============================================================== 1. Quality
class QualityStep(Step):
    title = "Quality"
    subtitle = "What this file actually is, and whether it's worth keeping"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(10)
        self.spectrogram = SpectrogramView()
        self.spectrogram.setMinimumHeight(260)
        left.addWidget(self.spectrogram, stretch=1)
        self.facts = QLabel()
        self.facts.setObjectName("muted")
        self.facts.setWordWrap(True)
        left.addWidget(self.facts)
        layout.addLayout(left, stretch=3)

        right = QVBoxLayout()
        right.setSpacing(12)

        self.headline = QLabel()
        self.headline.setObjectName("verdictHeadline")
        self.headline.setWordWrap(True)
        right.addWidget(self.headline)

        self.explanation = QLabel()
        self.explanation.setObjectName("verdictBody")
        self.explanation.setWordWrap(True)
        self.explanation.setAlignment(Qt.AlignmentFlag.AlignTop)
        right.addWidget(self.explanation)
        right.addStretch(1)

        # Each control is captioned, and the "same as the file" options say which
        # property they mean — two unlabelled "Match source" dropdowns side by side
        # gave no clue which was which.
        convert_box = QVBoxLayout()
        convert_box.setSpacing(6)
        heading = QLabel("Convert to")
        heading.setObjectName("panelTitle")
        convert_box.addWidget(heading)

        row = QHBoxLayout()
        row.setSpacing(10)
        for caption, builder, stretch in (
            ("Format", self._build_target, 2),
            ("Bit depth", self._build_depth, 1),
            ("Sample rate", self._build_rate, 1),
        ):
            column = QVBoxLayout()
            column.setSpacing(3)
            label = QLabel(caption)
            label.setObjectName("fieldLabel")
            column.addWidget(label)
            column.addWidget(builder())
            row.addLayout(column, stretch=stretch)
        convert_box.addLayout(row)

        self.warning = QLabel()
        self.warning.setObjectName("warning")
        self.warning.setWordWrap(True)
        self.warning.hide()
        convert_box.addWidget(self.warning)
        right.addLayout(convert_box)

        actions = QHBoxLayout()
        self.discard_button = QPushButton("Discard")
        self.discard_button.setToolTip("Remove from the list. The file is left where it is.")
        self.discard_button.clicked.connect(lambda: self.finished_early.emit("discard"))
        actions.addWidget(self.discard_button)

        self.quarantine_button = QPushButton("Move to _Re-source")
        self.quarantine_button.setObjectName("danger")
        self.quarantine_button.clicked.connect(lambda: self.finished_early.emit("quarantine"))
        actions.addWidget(self.quarantine_button)
        right.addLayout(actions)

        layout.addLayout(right, stretch=2)

    def _build_target(self) -> QComboBox:
        self.target = QComboBox()
        self.target.addItem("Keep original", None)
        for fmt in AudioFormat:
            self.target.addItem(fmt.label, fmt.value)
        self.target.currentIndexChanged.connect(self._on_target_changed)
        return self.target

    def _build_depth(self) -> QComboBox:
        self.depth = QComboBox()
        self.depth.addItem("Same as file", None)
        for value in (16, 24, 32):
            self.depth.addItem(f"{value}-bit", value)
        return self.depth

    def _build_rate(self) -> QComboBox:
        self.rate = QComboBox()
        self.rate.addItem("Same as file", None)
        for value in (44_100, 48_000, 96_000):
            self.rate.addItem(f"{value / 1000:g} kHz", value)
        return self.rate

    def load(self, track: Track) -> None:
        super().load(track)
        info, report = track.info, track.quality
        if info is None or report is None:
            self.headline.setText("Still analysing…")
            return

        self.spectrogram.show_report(report, info.sample_rate)

        bits = f"{info.bit_depth}-bit · " if info.bit_depth else ""
        rate = f"{info.sample_rate / 1000:g} kHz" if info.sample_rate else "?"
        kbps = f" · {info.bitrate_kbps} kbps" if info.bitrate_kbps else ""
        duration = f" · {int(info.duration // 60)}:{int(info.duration % 60):02d}" if info.duration else ""
        self.facts.setText(
            f"{info.fmt.label if info.fmt else info.codec} · {bits}{rate}{kbps}"
            f" · {info.file_size / 1e6:.1f} MB{duration}\n{info.path}"
        )

        advice = quality_mod.recommend(info, report)
        self.headline.setText(advice.headline)
        self.headline.setProperty("severity", advice.severity)
        self.headline.style().unpolish(self.headline)
        self.headline.style().polish(self.headline)

        measurement = "\n".join(report.notes)
        self.explanation.setText(f"{advice.explanation}\n\n— — —\n{measurement}")

        # Keep a format the user already picked; only fall back to the default rule
        # the first time this track reaches the step.
        if track.target is not None:
            chosen = track.target
        else:
            chosen = convert_mod.plan_from_settings(
                info, config.Settings.load(), is_suspect=report.is_suspect
            ).target
        value = None if chosen is KeepOriginal else chosen.value
        self.target.setCurrentIndex(max(0, self.target.findData(value)))
        # A flagged upscale defaults to MP3 320 — honest about the quality and playable
        # on all gear. Left editable so quarantining, or overriding, is still one click.
        self.target.setEnabled(True)
        self.target.setToolTip(
            "Flagged as a likely upscale — defaulting to MP3 320 so it plays on all gear "
            "without pretending to be lossless. Re-sourcing a real copy is still better."
            if report.is_suspect else ""
        )
        # Always offered on a flagged track, but only styled as the danger action when
        # it is actually the recommendation. A 320 kbps source is transparent on a club
        # system, so leading with "Move to _Re-source" there costs a playable track to
        # fix what is really a labelling problem.
        self.quarantine_button.setVisible(report.is_suspect)
        recommended = advice.action is quality_mod.RecommendedAction.QUARANTINE
        self.quarantine_button.setObjectName("danger" if recommended else "")
        self.quarantine_button.setToolTip(
            "Recommended: move it out of the library and re-source it."
            if recommended else
            "Optional — only worth it if you can re-source the track now."
        )
        self.quarantine_button.style().unpolish(self.quarantine_button)
        self.quarantine_button.style().polish(self.quarantine_button)
        self._on_target_changed()

    def current_plan(self) -> convert_mod.ConversionPlan | None:
        if self.track is None or self.track.info is None:
            return None
        value = self.target.currentData()
        target = KeepOriginal if value is None else AudioFormat(value)
        return convert_mod.plan(
            self.track.info, target,
            bit_depth=self.depth.currentData(),
            sample_rate=self.rate.currentData(),
            is_analysed=self.track.is_in_library,
            is_suspect=bool(self.track.quality and self.track.quality.is_suspect),
        )

    def _on_target_changed(self) -> None:
        plan = self.current_plan()
        if plan is None or not plan.warnings:
            self.warning.hide()
            return
        self.warning.setText("\n\n".join(w.message for w in plan.warnings))
        self.warning.show()

    def commit(self, validate: bool = True) -> bool:
        plan = self.current_plan()
        if plan is None or self.track is None:
            return False
        severe = [w for w in plan.warnings if w.severe] if validate else []
        if severe and not ask(
            self, "Are you sure?",
            "\n\n".join(w.message for w in severe) + "\n\nContinue anyway?",
        ):
            return False
        self.track.target = plan.target
        self.track.target_bit_depth = plan.bit_depth
        self.track.target_sample_rate = plan.sample_rate
        return True


# =============================================================== 2. Folder
class FolderStep(Step):
    title = "Folder"
    subtitle = "Where this track lives on disk"
    edit_requested = Signal(str, dict, str)

    def __init__(
        self,
        library: MusicLibrary,
        parent: QWidget | None = None,
        *,
        management_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.library = library
        self.management_mode = management_mode
        self._chosen: Path | None = None
        self._staged_paths: set[Path] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.current_banner = QLabel()
        self.current_banner.setObjectName("libraryStatus")
        self.current_banner.setWordWrap(True)
        if management_mode:
            self.current_banner.setText(
                "Folder renames and moves are staged with matching rekordbox path updates. "
                "Track IDs stay unchanged, preserving playlists, cues, tags and play counts."
            )
        else:
            self.current_banner.hide()
        layout.addWidget(self.current_banner)

        self.navigator = Navigator(
            self._children, search_all=self._search, multi_select=False
        )
        self.navigator.activated.connect(self._on_chosen)
        self.navigator.navigated.connect(lambda _t: self._on_navigated())
        layout.addWidget(self.navigator, stretch=1)

        footer = QHBoxLayout()
        self.new_folder_button = QPushButton("New folder here…")
        self.new_folder_button.clicked.connect(self._new_folder)
        footer.addWidget(self.new_folder_button)

        self.use_here_button = QPushButton("Use this folder")
        self.use_here_button.clicked.connect(self._use_current)
        if management_mode:
            self.use_here_button.hide()
            self.rename_folder_button = QPushButton("Rename selected…")
            self.rename_folder_button.clicked.connect(self._rename_folder)
            footer.addWidget(self.rename_folder_button)
            self.move_folder_button = QPushButton("Move selected…")
            self.move_folder_button.clicked.connect(self._move_folder)
            footer.addWidget(self.move_folder_button)
            self.delete_folder_button = QPushButton("Delete selected")
            self.delete_folder_button.setObjectName("danger")
            self.delete_folder_button.clicked.connect(self._delete_folder)
            footer.addWidget(self.delete_folder_button)
        else:
            footer.addWidget(self.use_here_button)
        footer.addStretch(1)

        self.destination = QLabel("No folder chosen")
        self.destination.setObjectName("destination")
        self.destination.setWordWrap(True)
        footer.addWidget(self.destination, stretch=1)
        layout.addLayout(footer)

    # -------------------------------------------------------------- contents
    def _folder_for(self, key: str | None) -> Path:
        return self.library.root / key if key else self.library.root

    def _children(self, key: str | None) -> list[Node]:
        folder = self._folder_for(key)
        if not folder.is_dir():
            return []
        nodes = []
        for child in sorted(
            (p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name.casefold(),
        ):
            relative = child.relative_to(self.library.root).as_posix()
            tracks = sum(1 for p in child.iterdir() if p.is_file())
            subfolders = sum(1 for p in child.iterdir() if p.is_dir())
            detail = f"{tracks} tracks" if tracks else ""
            if subfolders:
                detail += f"   {subfolders} folders" if detail else f"{subfolders} folders"
            nodes.append(Node(key=relative, name=child.name, is_container=True, detail=detail))
        return nodes

    def _search(self, needle: str) -> list[Node]:
        needle = needle.casefold()
        results = []
        for path in sorted(self.library.root.rglob("*")):
            if not path.is_dir() or path.name.startswith("."):
                continue
            if needle in path.name.casefold():
                relative = path.relative_to(self.library.root).as_posix()
                results.append(
                    Node(key=relative, name=path.name, is_container=True,
                         context=str(Path(relative).parent) if "/" in relative else "")
                )
        return results[:200]

    # --------------------------------------------------------------- actions
    def load(self, track: Track) -> None:
        super().load(track)

        # Restore a folder already chosen for this track. Without this, revisiting the
        # step — which free navigation makes easy — silently cleared the choice.
        self._chosen = None
        if track.genre_folder:
            parts = [track.genre_folder]
            if track.subgenre_folder:
                parts.extend(track.subgenre_folder.split("/"))
            self._chosen = self.library.root.joinpath(*parts)
            self._reveal(self._chosen)

        existing = track.existing
        if existing and track.library_status in {
            LibraryStatus.FILED_CORRECTLY, LibraryStatus.MISFILED, LibraryStatus.MISSING_FILE
        }:
            current = Path(existing.folder_path).parent
            where = self.library.classify(Path(existing.folder_path))
            if where:
                self.current_banner.setText(
                    f"Already in your collection, filed under "
                    f"<b>{' / '.join(p for p in where if p)}</b>.<br>"
                    f"Leave it there, or pick a new folder — rekordbox is updated in place, "
                    f"keeping playlists, rating, play count and cues."
                )
                if self._chosen is None:      # don't override a choice already made
                    self._chosen = current
                    self._reveal(current)
            else:
                self.current_banner.setText(
                    f"In your collection but outside the music folder "
                    f"(<b>{existing.folder_path}</b>). Choose where it belongs."
                )
            self.current_banner.show()
        else:
            self.current_banner.hide()

        self._update_destination()

    def _reveal(self, folder: Path) -> None:
        try:
            relative = folder.relative_to(self.library.root)
        except ValueError:
            return
        crumbs, accumulated = [], []
        for part in relative.parts:
            accumulated.append(part)
            crumbs.append(Crumb("/".join(accumulated), part))
        if crumbs:
            self.navigator.go_to_path(crumbs[:-1])

    def _on_navigated(self) -> None:
        self._update_destination()

    def _on_chosen(self, key: str) -> None:
        self._chosen = self._folder_for(key)
        self._update_destination()

    def _use_current(self) -> None:
        self._chosen = self._folder_for(self.navigator.current_key)
        self._update_destination()

    def _new_folder(self) -> None:
        parent = self._folder_for(self.navigator.current_key)
        name, ok = QInputDialog.getText(self, "New folder", f"New folder inside {parent.name}:")
        if not ok or not name.strip():
            return
        try:
            folder = parent / sanitise_folder_name(name)
            if folder.exists():
                raise FileExistsError(f"A folder named {folder.name!r} already exists here")
            folder.mkdir(parents=True)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not create folder", str(exc))
            return
        self.navigator.refresh()
        self._chosen = folder
        self._update_destination()

    def _rename_folder(self) -> None:
        folder = self._selected_managed_folder("rename")
        if folder is None:
            return
        name, ok = QInputDialog.getText(
            self, "Rename folder", "Folder name:", text=folder.name
        )
        if not ok or not name.strip() or name.strip() == folder.name:
            return
        try:
            target = folder.with_name(sanitise_folder_name(name))
            if target.exists():
                raise FileExistsError(f"A folder named {target.name!r} already exists here")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not rename folder", str(exc))
            return
        self._stage_folder_move(folder, target)

    def _move_folder(self) -> None:
        folder = self._selected_managed_folder("move")
        if folder is None:
            return
        candidates = [self.library.root]
        candidates.extend(
            path for path in sorted(self.library.root.rglob("*"))
            if path.is_dir() and path != folder and folder not in path.parents
        )
        labels = [
            "Music root" if path == self.library.root
            else path.relative_to(self.library.root).as_posix()
            for path in candidates
        ]
        choice, ok = QInputDialog.getItem(
            self, "Move folder", f"Move {folder.name!r} into:", labels, 0, False
        )
        if not ok:
            return
        parent = candidates[labels.index(choice)]
        target = parent / folder.name
        if target == folder:
            return
        if target.exists():
            QMessageBox.warning(
                self, "Could not move folder", f"A folder named {folder.name!r} already exists there"
            )
            return
        self._stage_folder_move(folder, target)

    def _selected_managed_folder(self, action: str) -> Path | None:
        folder = self._chosen
        if folder is None or folder == self.library.root:
            QMessageBox.information(self, f"{action.title()} folder", "Select a folder first.")
            return None
        if folder in self._staged_paths:
            QMessageBox.information(
                self, "Folder already staged", "Write the pending change before editing it again."
            )
            return None
        if not folder.is_dir():
            QMessageBox.warning(self, f"Could not {action} folder", f"Folder not found: {folder}")
            return None
        return folder

    def _stage_folder_move(self, folder: Path, target: Path) -> None:
        self._staged_paths.add(folder)
        self.edit_requested.emit(
            "move_folder",
            {
                "old_path": str(folder),
                "new_path": str(target),
                "music_root": str(self.library.root),
            },
            f"folder {folder.relative_to(self.library.root)} → "
            f"{target.relative_to(self.library.root)}",
        )

    def _delete_folder(self) -> None:
        folder = self._editable_empty_folder("delete")
        if folder is None:
            return
        if not ask(
            self,
            "Delete empty folder?",
            f"Delete {folder.name!r}?\n\nOnly the empty folder will be removed.",
        ):
            return
        try:
            folder.rmdir()
        except OSError as exc:
            QMessageBox.warning(self, "Could not delete folder", str(exc))
            return
        self._chosen = None
        self.navigator.refresh()
        self._update_destination()

    def _editable_empty_folder(self, action: str) -> Path | None:
        folder = self._chosen
        if folder is None or folder == self.library.root:
            QMessageBox.information(
                self, f"{action.title()} folder", "Select a folder first."
            )
            return None
        if folder in self._staged_paths:
            QMessageBox.information(
                self, "Folder already staged", "Write the pending change before deleting it."
            )
            return None
        try:
            is_empty = folder.is_dir() and next(folder.iterdir(), None) is None
        except OSError as exc:
            QMessageBox.warning(self, f"Could not {action} folder", str(exc))
            return None
        if not is_empty:
            QMessageBox.information(
                self,
                f"Cannot {action} this folder",
                "KRATR only renames or deletes empty folders. A folder containing tracks "
                "or subfolders may already be referenced by rekordbox.",
            )
            return None
        return folder

    def refresh_management(self, *, clear_staged: bool = False) -> None:
        if clear_staged:
            self._staged_paths.clear()
            self._chosen = None
        self.navigator.refresh()
        self._update_destination()

    def _update_destination(self) -> None:
        if self._chosen is None:
            self.destination.setText(
                "Select a folder to manage" if self.management_mode
                else "Select a folder, or open one and press “Use this folder”"
            )
            self.destination.setProperty("chosen", False)
        else:
            prefix = "SELECTED" if self.management_mode else "→"
            self.destination.setText(f"{prefix}  {self._chosen}")
            self.destination.setProperty("chosen", True)
        self.destination.style().unpolish(self.destination)
        self.destination.style().polish(self.destination)
        self.validity_changed.emit(self.is_valid())

    def is_valid(self) -> bool:
        return self._chosen is not None

    def commit(self, validate: bool = True) -> bool:
        if self.track is None or self._chosen is None:
            return False
        relative = self._chosen.relative_to(self.library.root).parts
        self.track.genre_folder = relative[0] if relative else None
        self.track.subgenre_folder = "/".join(relative[1:]) if len(relative) > 1 else None
        return True


# ============================================================ 3. Playlists
class PlaylistStep(Step):
    title = "Playlists"
    subtitle = "Which sets this track belongs in — it can be in as many as you like"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nodes: dict[str, object] = {}
        self._names: dict[str, str] = {}
        self._children_map: dict[str | None, list] = {}
        self._suggested: set[str] = set()
        self._already_in: set[str] = set()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(10)
        self.banner = QLabel()
        self.banner.setObjectName("libraryStatus")
        self.banner.setWordWrap(True)
        self.banner.hide()
        left.addWidget(self.banner)

        self.navigator = Navigator(
            self._children, search_all=self._search, multi_select=True
        )
        self.navigator.activated.connect(lambda _k: self._refresh_selected())
        left.addWidget(self.navigator, stretch=1)
        layout.addLayout(left, stretch=3)

        right = QVBoxLayout()
        right.setSpacing(8)
        self.selected_title = QLabel("Selected")
        self.selected_title.setObjectName("panelTitle")
        right.addWidget(self.selected_title)

        self.selected_list = QListWidget()
        self.selected_list.setObjectName("browserList")
        self.selected_list.itemDoubleClicked.connect(self._remove_selected)
        right.addWidget(self.selected_list, stretch=1)

        hint = QLabel(
            "Double-click to remove.\n"
            "Removing one marked “already in” only skips re-adding it — "
            "the track stays in that playlist."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        right.addWidget(hint)
        layout.addLayout(right, stretch=1)

    def set_tree(self, roots: list) -> None:
        """Index the playlist tree once, so navigation is instant."""
        self._children_map = {}
        self._names = {}

        def walk(nodes, parent_key):
            self._children_map[parent_key] = nodes
            for node in nodes:
                self._names[node.id] = node.name
                walk(node.children, node.id)

        walk(roots, None)
        self.navigator.refresh()

    def _children(self, key: str | None) -> list[Node]:
        return [
            Node(
                key=node.id,
                name=node.name,
                is_container=bool(node.children),
                selectable=node.accepts_tracks,
                detail=(f"{node.track_count} tracks" if node.accepts_tracks else
                        ("smart" if node.is_smart else "")),
            )
            for node in self._children_map.get(key, [])
        ]

    def _search(self, needle: str) -> list[Node]:
        needle = needle.casefold()
        results = []
        for parent_key, nodes in self._children_map.items():
            for node in nodes:
                if needle in node.name.casefold():
                    results.append(
                        Node(
                            key=node.id, name=node.name,
                            is_container=bool(node.children),
                            selectable=node.accepts_tracks,
                            detail=f"{node.track_count} tracks" if node.accepts_tracks else "",
                            context=self._names.get(parent_key, "") if parent_key else "",
                        )
                    )
        return results[:200]

    @staticmethod
    def _normalise(name: str) -> str:
        """Fold a name for matching: 'Garage-House' and 'Garage House' are the same."""
        return "".join(ch for ch in name.casefold() if ch.isalnum())

    def suggest_for_folder(self, genre: str | None, subgenre: str | None) -> list[str]:
        """Playlists whose names match the folder the track is being filed into.

        The playlist tree mirrors the folder tree — ``D:\\Music\\House\\Soulful House``
        has a ``Soulful House`` playlist — so once a folder is chosen, the matching
        playlist is nearly always wanted. Suggested, not forced: it is pre-ticked and
        can be unticked.
        """
        wanted = []
        if subgenre:
            wanted.append(subgenre.split("/")[-1])
        if genre:
            wanted.append(genre)
        if not wanted:
            return []

        by_name: dict[str, str] = {}
        for nodes in self._children_map.values():
            for node in nodes:
                if node.accepts_tracks:
                    # First match wins, so a leaf beats a same-named list elsewhere.
                    by_name.setdefault(self._normalise(node.name), node.id)

        matches = []
        for candidate in wanted:
            playlist_id = by_name.get(self._normalise(candidate))
            if playlist_id and playlist_id not in matches:
                matches.append(playlist_id)
        return matches

    def load(self, track: Track) -> None:
        super().load(track)

        existing_ids = list(track.existing.playlist_ids) if track.existing else []
        suggested = self.suggest_for_folder(track.genre_folder, track.subgenre_folder)
        # Union, never a replacement: a suggestion must not drop an earlier choice,
        # and the playlists the track is already in must show as selected rather than
        # only being described in the banner.
        selection = list(dict.fromkeys([*track.playlist_ids, *existing_ids, *suggested]))
        self._already_in = set(existing_ids)
        self._suggested = set(suggested) - set(track.playlist_ids) - self._already_in

        messages = []
        existing = track.existing
        if existing and existing.playlist_names:
            messages.append(
                f"Already in <b>{len(existing.playlist_names)}</b> playlist(s): "
                f"{', '.join(existing.playlist_names[:8])}"
                f"{'…' if len(existing.playlist_names) > 8 else ''} — all ticked below. "
                f"Adding is additive: unticking one won't remove the track from it."
            )
        if self._suggested:
            names = [self._names.get(p, p) for p in self._suggested]
            messages.append(
                f"Pre-selected <b>{', '.join(names)}</b> to match the folder you chose. "
                f"Untick if that's not what you want."
            )

        if messages:
            self.banner.setText("<br>".join(messages))
            self.banner.show()
        else:
            self.banner.hide()

        self.navigator.set_selected(selection)
        self._refresh_selected()

    def _refresh_selected(self) -> None:
        keys = self.navigator.selected_keys()
        self.selected_list.clear()
        for key in keys:
            name = self._names.get(key, key)
            if key in self._already_in:
                name = f"{name}   · already in"
            item = QListWidgetItem(name)
            # The key rides on the item rather than being reverse-looked-up from the
            # label: the label is decorated, and two playlists can share a name.
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.selected_list.addItem(item)
        self.selected_title.setText(f"Selected — {len(keys)}")

    def _remove_selected(self, item) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        if key is None:
            return
        self.navigator.set_selected(
            [k for k in self.navigator.selected_keys() if k != key]
        )
        self._refresh_selected()

    def commit(self, validate: bool = True) -> bool:
        if self.track is not None:
            self.track.playlist_ids = self.navigator.selected_keys()
        return True


# ================================================================= 4. Tags
#: Mime type used when dragging a chip to a new position.
TAG_MIME = "application/x-crate-tag"


class TagButton(QPushButton):
    """A tag chip. Click to apply it; drag it to change the order."""

    def __init__(self, tag_id: str, name: str, count: int, parent: QWidget | None = None) -> None:
        # Just the name — a running track tally on every chip is noise when you're
        # scanning for the right tag. The count still appears where it matters, in
        # the delete confirmation.
        super().__init__(name, parent)
        self.tag_id = tag_id
        self.tag_name = name
        self.track_count = count
        self.setCheckable(True)
        self.setObjectName("tagChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._press_at: QPoint | None = None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.LeftButton:
            self._press_at = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # Only past the system drag threshold, so an ordinary click still toggles.
        if self._press_at is not None and (
            (event.position().toPoint() - self._press_at).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._start_drag()
            return
        super().mouseMoveEvent(event)

    def _start_drag(self) -> None:
        self._press_at = None
        self.setDown(False)

        payload = QMimeData()
        payload.setData(TAG_MIME, self.tag_id.encode("utf-8"))

        drag = QDrag(self)
        drag.setMimeData(payload)
        drag.setPixmap(self.grab())
        drag.setHotSpot(QPoint(self.width() // 2, self.height() // 2))
        drag.exec(Qt.DropAction.MoveAction)


class TagFlow(FlowWidget):
    """Holds one category's chips and accepts them being dragged into a new order."""

    reordered = Signal(str, str, int)   # category_id, moved tag_id, new index

    def __init__(self, category_id: str, spacing: int = 6, parent: QWidget | None = None) -> None:
        super().__init__(spacing=spacing, parent=parent)
        self.category_id = category_id
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(TAG_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(TAG_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(TAG_MIME):
            return
        tag_id = bytes(event.mimeData().data(TAG_MIME)).decode("utf-8")
        self.reordered.emit(self.category_id, tag_id, self._index_at(event.position().toPoint()))
        event.acceptProposedAction()

    def _index_at(self, point: QPoint) -> int:
        """Where in the order the chip was dropped."""
        chips = [c for c in self.findChildren(TagButton)]
        chips.sort(key=lambda c: (c.y(), c.x()))
        for index, chip in enumerate(chips):
            centre = chip.geometry().center()
            # Same row and left of centre, or on an earlier row entirely.
            if point.y() < chip.geometry().bottom() and point.x() < centre.x():
                return index
        return len(chips)


class TagsStep(Step):
    title = "Tags"
    subtitle = "How you'll find this track on a CDJ mid-set"

    #: Taxonomy edits to queue: (action, args, description)
    edit_requested = Signal(str, dict, str)

    def __init__(
        self, parent: QWidget | None = None, *, management_mode: bool = False
    ) -> None:
        super().__init__(parent)
        self.management_mode = management_mode
        self._categories: list[mytags.Category] = []
        self._selected: set[str] = set()
        self._buttons: list[TagButton] = []
        self._pending: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.banner = QLabel()
        self.banner.setObjectName("libraryStatus")
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)

        controls = QHBoxLayout()
        self.edit_toggle = QPushButton("✎  Edit tags")
        self.edit_toggle.setCheckable(True)
        self.edit_toggle.setToolTip(
            "Show rename and delete controls on every tag, and let you rename categories"
        )
        self.edit_toggle.toggled.connect(self._on_edit_toggled)
        controls.addWidget(self.edit_toggle)

        if management_mode:
            self.edit_toggle.blockSignals(True)
            self.edit_toggle.setChecked(True)
            self.edit_toggle.blockSignals(False)
            self.edit_toggle.hide()
        self.edit_hint = QLabel(
            "Add, rename, delete, or drag tags to reorder them"
            if management_mode else "Click a tag to apply it to this track"
        )
        self.edit_hint.setObjectName("muted")
        controls.addWidget(self.edit_hint)
        controls.addStretch(1)
        layout.addLayout(controls)

        # Taxonomy changes can't be written while rekordbox may be open, so they queue.
        # Without saying so, editing a tag looks like it silently did nothing.
        self.pending_banner = QLabel()
        self.pending_banner.setObjectName("warning")
        self.pending_banner.setWordWrap(True)
        self.pending_banner.hide()
        layout.addWidget(self.pending_banner)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 8, 0)
        self._body_layout.setSpacing(18)
        scroll.setWidget(self._body)
        layout.addWidget(scroll, stretch=1)

        self.summary = QLabel("No tags selected")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        if management_mode:
            self.summary.hide()
        layout.addWidget(self.summary)

    # ------------------------------------------------------------------ edits
    def _on_edit_toggled(self, editing: bool) -> None:
        self.edit_hint.setText(
            "Rename or delete tags below · click “+ Tag” to add one" if editing
            else "Click a tag to apply it to this track"
        )
        self.set_categories(self._categories)

    def set_pending(self, descriptions: list[str]) -> None:
        """Show what is genuinely still queued.

        Driven by the op queue rather than a local tally, because a local list never
        learns that an apply happened — it kept claiming changes were unsaved long
        after they had been written, which is exactly the wrong thing to be wrong about.
        """
        self._pending = list(descriptions)
        if not self._pending:
            self.pending_banner.hide()
            return

        shown = "; ".join(self._pending[-6:])
        more = f" (and {len(self._pending) - 6} more)" if len(self._pending) > 6 else ""
        self.pending_banner.setText(
            f"<b>{len(self._pending)} tag change(s) waiting to be written to rekordbox:</b> "
            f"{shown}{more}"
            + (
                "<br>Use Write changes below after closing rekordbox."
                if self.management_mode
                else "<br>They will be written automatically when this track is imported. "
                     "New tags can be used on the track straight away."
            )
        )
        self.pending_banner.show()

    def _record_pending(self, description: str) -> None:
        """Optimistic local echo; corrected by :meth:`set_pending` from the queue."""
        self.set_pending([*self._pending, description])

    def set_categories(self, categories: list[mytags.Category]) -> None:
        self._categories = categories
        clear_layout(self._body_layout)
        self._buttons = []

        for category in categories:
            self._body_layout.addWidget(self._build_category(category))
        self._body_layout.addStretch(1)
        self._refresh_summary()

    def _build_category(self, category: mytags.Category) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel(category.name.upper())
        title.setObjectName("categoryTitle")
        header.addWidget(title)

        count = QLabel(f"{len(category.tags)}/{mytags.MAX_TAGS_PER_CATEGORY}")
        count.setObjectName("muted")
        header.addWidget(count)
        header.addStretch(1)

        for label, slot in (
            ("+ Tag", lambda _=False, c=category: self._add_tag(c)),
            ("Rename category", lambda _=False, c=category: self._rename_category(c)),
        ):
            button = QPushButton(label)
            button.setObjectName("linkButton")
            button.clicked.connect(slot)
            header.addWidget(button)
        layout.addLayout(header)

        chips = TagFlow(category.id, spacing=6)
        chips.reordered.connect(self._reorder_tag)
        editing = self.management_mode or self.edit_toggle.isChecked()
        for tag in category.tags:
            button = TagButton(tag.id, tag.name, tag.track_count)
            if self.management_mode:
                button.setCheckable(False)
            else:
                button.setChecked(tag.id in self._selected)
                button.toggled.connect(lambda checked, t=tag.id: self._on_toggled(t, checked))
            self._buttons.append(button)

            if not editing:
                chips.add(button)
                continue

            # In edit mode each tag carries its own visible rename and delete controls,
            # rather than hiding them behind a right-click nobody would think to try.
            group = QWidget()
            row = QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(3)
            row.addWidget(button)
            for glyph, name, tip, slot in (
                ("✎", "chipAction", "Rename", lambda _=False, t=tag: self._rename_tag(t)),
                ("✕", "chipDelete", "Delete", lambda _=False, t=tag: self._delete_tag(t)),
            ):
                small = QPushButton(glyph)
                small.setObjectName(name)
                small.setFixedSize(26, 26)
                small.setCursor(Qt.CursorShape.PointingHandCursor)
                small.setToolTip(f"{tip} “{tag.name}”")
                small.clicked.connect(slot)
                row.addWidget(small)
            chips.add(group)

        if not category.tags:
            empty = QLabel("No tags yet — use “+ Tag” to add one")
            empty.setObjectName("muted")
            chips.add(empty)
        layout.addWidget(chips)
        return box

    # ---------------------------------------------------------------- editing
    # Every edit does two things: change what's on screen straight away, and queue the
    # real change for rekordbox. Only queueing made editing look broken.
    def _add_tag(self, category: mytags.Category) -> None:
        if len(category.tags) >= mytags.MAX_TAGS_PER_CATEGORY:
            QMessageBox.warning(
                self, "Category full",
                f"{category.name} already has {mytags.MAX_TAGS_PER_CATEGORY} tags — "
                f"rekordbox's limit per category. Delete one first.",
            )
            return
        name, ok = QInputDialog.getText(self, "New tag", f"New tag in {category.name}:")
        name = name.strip() if ok else ""
        if not name:
            return
        if any(t.name.casefold() == name.casefold() for t in category.tags):
            QMessageBox.warning(self, "Already exists",
                                f"{category.name} already has a tag called {name!r}.")
            return

        # A real ID only exists once the create is applied, so the tag carries a
        # provisional one. `handlers.handle_set_mytags` resolves it by name at apply
        # time — the create op always runs first — so it can be used on this track now.
        category.tags.append(
            mytags.Tag(
                id=f"pending:{category.id}:{name}", name=name,
                seq=len(category.tags) + 1, category_id=category.id,
            )
        )
        self.edit_requested.emit(
            "create_tag", {"category_id": category.id, "name": name}, f"new tag {name!r}"
        )
        self._record_pending(f"+{name}")
        self.set_categories(self._categories)

    def _rename_category(self, category: mytags.Category) -> None:
        name, ok = QInputDialog.getText(
            self, "Rename category", "Category name:", text=category.name
        )
        name = name.strip() if ok else ""
        if not name or name == category.name:
            return

        self.edit_requested.emit(
            "rename_category", {"category_id": category.id, "name": name},
            f"category → {name!r}",
        )
        self._record_pending(f"{category.name} → {name}")
        category.name = name
        self.set_categories(self._categories)

    def _rename_tag(self, tag: mytags.Tag) -> None:
        name, ok = QInputDialog.getText(self, "Rename tag", "Tag name:", text=tag.name)
        name = name.strip() if ok else ""
        if not name or name == tag.name:
            return

        if not tag.id.startswith("pending:"):
            self.edit_requested.emit(
                "rename_tag", {"tag_id": tag.id, "name": name}, f"{tag.name!r} → {name!r}"
            )
            self._record_pending(f"{tag.name} → {name}")
        else:
            # Not created yet — rewrite the queued create instead of renaming after.
            self.edit_requested.emit(
                "create_tag", {"category_id": tag.category_id, "name": name},
                f"new tag {name!r}",
            )
            self._record_pending(f"+{name}")
            tag.id = f"pending:{tag.category_id}:{name}"

        tag.name = name
        self.set_categories(self._categories)

    def _reorder_tag(self, category_id: str, tag_id: str, new_index: int) -> None:
        """Move a dragged chip to a new position within its category."""
        category = next((c for c in self._categories if c.id == category_id), None)
        if category is None:
            return

        moving = next((t for t in category.tags if t.id == tag_id), None)
        if moving is None:
            return  # dragged in from another category — order is per-category

        current = category.tags.index(moving)
        # Dropping after its own slot shifts the target left by one once removed.
        if new_index > current:
            new_index -= 1
        new_index = max(0, min(new_index, len(category.tags) - 1))
        if new_index == current:
            return

        category.tags.pop(current)
        category.tags.insert(new_index, moving)
        for position, tag in enumerate(category.tags, start=1):
            tag.seq = position

        self.edit_requested.emit(
            "reorder_tags",
            {"category_id": category_id, "ordered_ids": [t.id for t in category.tags]},
            f"reordered {category.name}",
        )
        self._record_pending(f"↕{category.name}")
        self.set_categories(self._categories)

    def _delete_tag(self, tag: mytags.Tag) -> None:
        affected = (
            f"\n\nIt will be removed from {tag.track_count} track(s)."
            if tag.track_count else ""
        )
        if not ask(self, "Delete tag?", f"Delete {tag.name!r}?{affected}"):
            return

        if not tag.id.startswith("pending:"):
            self.edit_requested.emit("delete_tag", {"tag_id": tag.id}, f"− {tag.name!r}")
            self._record_pending(f"−{tag.name}")

        self._selected.discard(tag.id)
        for category in self._categories:
            category.tags = [t for t in category.tags if t.id != tag.id]
        self.set_categories(self._categories)

    # -------------------------------------------------------------- selection
    def _on_toggled(self, tag_id: str, checked: bool) -> None:
        if checked:
            self._selected.add(tag_id)
        else:
            self._selected.discard(tag_id)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        names = [b.tag_name for b in self._buttons if b.tag_id in self._selected]
        self.summary.setText(
            f"{len(names)} selected — {', '.join(sorted(names))}" if names else "No tags selected"
        )

    def load(self, track: Track) -> None:
        super().load(track)
        self._selected = set(track.my_tag_ids)
        for button in self._buttons:
            button.blockSignals(True)
            button.setChecked(button.tag_id in self._selected)
            button.blockSignals(False)

        existing = track.existing
        if existing and existing.my_tag_names:
            self.banner.setText(
                f"Already tagged: <b>{', '.join(existing.my_tag_names)}</b>. "
                f"These are pre-selected — unticking one removes it from the track."
            )
            self.banner.show()
        else:
            self.banner.hide()
        self._refresh_summary()

    def commit(self, validate: bool = True) -> bool:
        if self.track is not None:
            self.track.my_tag_ids = sorted(self._selected)
        return True


# =============================================================== 5. Colour
class ColourStep(Step):
    title = "Colour"
    subtitle = "The one thing you can read at a glance on a CDJ"

    edit_requested = Signal(str, dict, str)
    import_requested = Signal()

    def __init__(
        self, parent: QWidget | None = None, *, management_mode: bool = False
    ) -> None:
        super().__init__(parent)
        self.management_mode = management_mode
        self._selected: str | None = None
        self._buttons: dict[str, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.banner = QLabel()
        self.banner.setObjectName("libraryStatus")
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(10)
        layout.addWidget(self.grid_host)

        self.hint = QLabel(
            (
                "Click a colour to rename it. "
                if management_mode else "Right-click a colour to rename it. "
            )
            + "Renaming keeps every track already set to that colour — they follow "
              "the colour, not its label."
        )
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        layout.addStretch(1)

        self.summary = QLabel()
        self.summary.setObjectName("summaryBox")
        self.summary.setWordWrap(True)
        if management_mode:
            self.summary.hide()
        layout.addWidget(self.summary)

    def set_colours(self, colours: list[colours_mod.Colour]) -> None:
        clear_layout(self.grid)
        self._buttons = {}

        for index, colour in enumerate(colours):
            button = QPushButton(colour.name + (f"\n{colour.track_count} tracks" if colour.track_count else ""))
            button.setCheckable(not self.management_mode)
            if not self.management_mode:
                button.setChecked(colour.id == self._selected)
            button.setMinimumHeight(64)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{ border-left: 12px solid {colour.swatch}; text-align: left;"
                f" padding-left: 12px; font-size: 14px; }}"
                f"QPushButton:checked {{ background: {colour.swatch}33;"
                f" border: 2px solid {colour.swatch}; border-left: 12px solid {colour.swatch};"
                f" font-weight: 600; }}"
            )
            if self.management_mode:
                button.clicked.connect(lambda _=False, c=colour: self._rename(c))
            else:
                button.clicked.connect(lambda _=False, c=colour.id: self._choose(c))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(lambda _p, c=colour: self._rename(c))
            self.grid.addWidget(button, index // 4, index % 4)
            self._buttons[colour.id] = button

    def _choose(self, colour_id: str) -> None:
        self._selected = None if self._selected == colour_id else colour_id
        for cid, button in self._buttons.items():
            button.setChecked(cid == self._selected)
        self._refresh_summary()

    def _rename(self, colour: colours_mod.Colour) -> None:
        name, ok = QInputDialog.getText(
            self, "Rename colour", "Label for this colour:", text=colour.name
        )
        if ok and name.strip():
            colour.name = name.strip()
            button = self._buttons.get(colour.id)
            if button is not None:
                count = f"\n{colour.track_count} tracks" if colour.track_count else ""
                button.setText(colour.name + count)
            self.edit_requested.emit(
                "rename_colour", {"colour_id": colour.id, "name": name.strip()},
                f"colour → {name.strip()!r}",
            )

    def load(self, track: Track) -> None:
        super().load(track)
        self._selected = track.colour_id
        for cid, button in self._buttons.items():
            button.setChecked(cid == self._selected)

        if track.existing and track.existing.colour_id:
            self.banner.setText("This track already has a colour — shown selected below.")
            self.banner.show()
        else:
            self.banner.hide()
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        track = self.track
        if track is None:
            return
        target = "Keep original" if track.target in (None, KeepOriginal) else track.target.label
        folder = "/".join(p for p in (track.genre_folder, track.subgenre_folder) if p)

        # .get(), not [] — an unrecognised colour ID must never take down the whole
        # wizard. It did: rekordbox stores "0" for "no colour", which is a truthy
        # string, so the lookup raised KeyError and the Import button silently died.
        button = self._buttons.get(self._selected) if self._selected else None
        colour = button.text().split("\n")[0] if button is not None else "None"
        self.summary.setText(
            f"<b>Ready to import</b><br>"
            f"Format: {target} &nbsp;·&nbsp; Folder: {folder or '—'}<br>"
            f"Playlists: {len(track.playlist_ids)} &nbsp;·&nbsp; "
            f"Tags: {len(track.my_tag_ids)} &nbsp;·&nbsp; Colour: {colour}"
        )

    def commit(self, validate: bool = True) -> bool:
        if self.track is not None:
            self.track.colour_id = self._selected
        return True

