"""Playlist picker over the existing nested tree.

The library has 197 entries across folders like ``Club 4x4``, ``HAZY``, ``Bar``,
``CUBE b2b OUISSAM`` and a ``House`` folder with ~29 sub-playlists, so search matters
more than browsing. Filtering keeps a playlist visible when its own name matches *or*
when any descendant does, otherwise typing would hide the folder you were aiming for.

Folders and smart playlists are shown for context but cannot be ticked — neither can
hold tracks directly.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..rekordbox.reader import PlaylistNode

_ID_ROLE = Qt.ItemDataRole.UserRole
_SELECTABLE_ROLE = Qt.ItemDataRole.UserRole + 1


class PlaylistPicker(QWidget):
    """Multi-select tree of playlists. Tracks legitimately live in several."""

    selection_changed = Signal(list)  # list[str] of playlist IDs

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search playlists…")
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumHeight(120)
        self.tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tree, stretch=1)

        self.summary = QLabel("No playlists selected")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        # Word-wrapped labels report a tiny height hint, so in a squeezed layout the
        # summary ends up drawn over the tree. A floor keeps it in its own space.
        self.summary.setMinimumHeight(32)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.summary, stretch=0)

        self._selected: set[str] = set()

    # ------------------------------------------------------------------ populate
    def set_playlists(self, roots: list[PlaylistNode]) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        for node in roots:
            self.tree.addTopLevelItem(self._build(node))
        self.tree.blockSignals(False)
        self._apply_filter(self.search.text())

    def _build(self, node: PlaylistNode) -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        if node.is_folder:
            label = f"📁  {node.name}"
        elif node.is_smart:
            label = f"⚙  {node.name}  (smart)"
        else:
            label = f"{node.name}   ({node.track_count})"
        item.setText(0, label)
        item.setData(0, _ID_ROLE, node.id)
        item.setData(0, _SELECTABLE_ROLE, node.accepts_tracks)

        if node.accepts_tracks:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if node.id in self._selected
                               else Qt.CheckState.Unchecked)
        else:
            # Visible for context, but not a valid destination.
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            item.setForeground(0, Qt.GlobalColor.gray)

        for child in node.children:
            item.addChild(self._build(child))
        return item

    # ----------------------------------------------------------------- selection
    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if not item.data(0, _SELECTABLE_ROLE):
            return
        playlist_id = item.data(0, _ID_ROLE)
        if item.checkState(0) == Qt.CheckState.Checked:
            self._selected.add(playlist_id)
        else:
            self._selected.discard(playlist_id)
        self._update_summary()
        self.selection_changed.emit(self.selected())

    def selected(self) -> list[str]:
        return sorted(self._selected)

    def set_selected(self, playlist_ids: list[str]) -> None:
        self._selected = set(playlist_ids)
        self._refresh_check_states()
        self._update_summary()

    def clear_selection(self) -> None:
        self.set_selected([])

    def _refresh_check_states(self) -> None:
        self.tree.blockSignals(True)
        for item in self._walk():
            if item.data(0, _SELECTABLE_ROLE):
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if item.data(0, _ID_ROLE) in self._selected
                    else Qt.CheckState.Unchecked,
                )
        self.tree.blockSignals(False)

    def _update_summary(self) -> None:
        count = len(self._selected)
        if not count:
            self.summary.setText("No playlists selected")
            return
        names = [
            item.text(0).split("   (")[0]
            for item in self._walk()
            if item.data(0, _ID_ROLE) in self._selected
        ]
        self.summary.setText(f"{count} selected: " + ", ".join(sorted(names)))

    # -------------------------------------------------------------------- filter
    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for index in range(self.tree.topLevelItemCount()):
            self._filter_item(self.tree.topLevelItem(index), needle)

    def _filter_item(self, item: QTreeWidgetItem, needle: str) -> bool:
        """Show an item when it matches, or when any descendant does."""
        matched = needle in item.text(0).casefold() if needle else True
        child_matched = False
        for i in range(item.childCount()):
            if self._filter_item(item.child(i), needle):
                child_matched = True

        visible = matched or child_matched
        item.setHidden(not visible)
        if needle and child_matched:
            item.setExpanded(True)
        return visible

    def _walk(self, parent: QTreeWidgetItem | None = None):
        if parent is None:
            for i in range(self.tree.topLevelItemCount()):
                yield from self._walk(self.tree.topLevelItem(i))
            return
        yield parent
        for i in range(parent.childCount()):
            yield from self._walk(parent.child(i))
