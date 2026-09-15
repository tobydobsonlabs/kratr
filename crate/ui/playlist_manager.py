"""Playlist tree editor for the standalone Settings flow."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dialogs import ask

ID_ROLE = Qt.ItemDataRole.UserRole
ATTRIBUTE_ROLE = Qt.ItemDataRole.UserRole + 1
PARENT_ROLE = Qt.ItemDataRole.UserRole + 2


class PlaylistManager(QWidget):
    """Stage playlist edits while keeping stable rekordbox playlist IDs."""

    edit_requested = Signal(str, dict, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._roots: list[Any] = []
        self._staged_ids: set[str] = set()
        self._folder_options: list[tuple[str | None, str]] = [(None, "All playlists")]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        note = QLabel(
            "Renaming or moving a playlist keeps its ID, so every track membership "
            "stays connected. Deleting a playlist removes only that playlist and its links."
        )
        note.setObjectName("libraryStatus")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.tree = QTreeWidget()
        self.tree.setObjectName("browserList")
        self.tree.setHeaderLabels(("Playlist", "Type", "Tracks"))
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, stretch=1)

        actions = QHBoxLayout()
        for label, slot in (
            ("New playlist…", lambda: self._create(False)),
            ("New folder…", lambda: self._create(True)),
            ("Rename…", self._rename),
            ("Move…", self._move),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            actions.addWidget(button)
        actions.addStretch(1)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)

    def set_tree(self, roots: list[Any], *, clear_staged: bool = False) -> None:
        self._roots = roots
        if clear_staged:
            self._staged_ids.clear()
        self.tree.clear()
        self._folder_options = [(None, "All playlists")]

        def add(node: Any, parent_item: QTreeWidgetItem | None, path: str) -> None:
            attribute = int(node.attribute)
            kind = "Folder" if attribute == 1 else ("Smart" if attribute == 4 else "Playlist")
            item = QTreeWidgetItem((node.name, kind, str(node.track_count or "")))
            item.setData(0, ID_ROLE, str(node.id))
            item.setData(0, ATTRIBUTE_ROLE, attribute)
            item.setData(0, PARENT_ROLE, parent_item.data(0, ID_ROLE) if parent_item else None)
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            current_path = f"{path} / {node.name}" if path else node.name
            if attribute == 1:
                self._folder_options.append((str(node.id), current_path))
            for child in node.children:
                add(child, item, current_path)

        for root in roots:
            add(root, None, "")
        self.tree.expandToDepth(0)

    def _selected(self) -> QTreeWidgetItem | None:
        return self.tree.currentItem()

    def _target_parent_id(self) -> str | None:
        item = self._selected()
        if item is None:
            return None
        if item.data(0, ATTRIBUTE_ROLE) == 1:
            return item.data(0, ID_ROLE)
        return item.data(0, PARENT_ROLE)

    def _create(self, folder: bool) -> None:
        kind = "playlist folder" if folder else "playlist"
        name, ok = QInputDialog.getText(self, f"New {kind}", "Name:")
        name = name.strip() if ok else ""
        if not name:
            return
        parent_id = self._target_parent_id()
        self.edit_requested.emit(
            "create_playlist",
            {"name": name, "parent_id": parent_id, "folder": folder},
            f"new {kind} {name!r}",
        )

    def _rename(self) -> None:
        item = self._editable_selected("rename")
        if item is None:
            return
        playlist_id = str(item.data(0, ID_ROLE))
        name, ok = QInputDialog.getText(
            self, "Rename playlist", "Name:", text=item.text(0)
        )
        name = name.strip() if ok else ""
        if not name or name == item.text(0):
            return
        self._staged_ids.add(playlist_id)
        self.edit_requested.emit(
            "rename_playlist",
            {"playlist_id": playlist_id, "name": name},
            f"playlist {item.text(0)!r} → {name!r}",
        )

    def _move(self) -> None:
        item = self._editable_selected("move")
        if item is None:
            return
        playlist_id = str(item.data(0, ID_ROLE))
        attribute = item.data(0, ATTRIBUTE_ROLE)
        forbidden = {playlist_id}
        if attribute == 1:
            stack = [item]
            while stack:
                current = stack.pop()
                for index in range(current.childCount()):
                    child = current.child(index)
                    forbidden.add(str(child.data(0, ID_ROLE)))
                    stack.append(child)
        options = [(pid, label) for pid, label in self._folder_options if pid not in forbidden]
        labels = [label for _pid, label in options]
        choice, ok = QInputDialog.getItem(
            self, "Move playlist", f"Move {item.text(0)!r} into:", labels, 0, False
        )
        if not ok:
            return
        parent_id = options[labels.index(choice)][0]
        if parent_id == item.data(0, PARENT_ROLE):
            return
        self._staged_ids.add(playlist_id)
        self.edit_requested.emit(
            "move_playlist",
            {"playlist_id": playlist_id, "parent_id": parent_id},
            f"move playlist {item.text(0)!r} → {choice}",
        )

    def _delete(self) -> None:
        item = self._editable_selected("delete")
        if item is None:
            return
        if item.data(0, ATTRIBUTE_ROLE) == 1 and item.childCount():
            QMessageBox.information(
                self,
                "Folder is not empty",
                "Move or delete the playlists inside this folder first.",
            )
            return
        count = item.text(2) or "0"
        if not ask(
            self,
            "Delete playlist?",
            f"Delete {item.text(0)!r}?\n\n{count} track membership(s) will be removed. "
            "The audio files and collection tracks are untouched.",
        ):
            return
        playlist_id = str(item.data(0, ID_ROLE))
        self._staged_ids.add(playlist_id)
        self.edit_requested.emit(
            "delete_playlist",
            {"playlist_id": playlist_id},
            f"delete playlist {item.text(0)!r}",
        )

    def _editable_selected(self, action: str) -> QTreeWidgetItem | None:
        item = self._selected()
        if item is None:
            QMessageBox.information(self, f"{action.title()} playlist", "Select an item first.")
            return None
        playlist_id = str(item.data(0, ID_ROLE))
        if playlist_id in self._staged_ids:
            QMessageBox.information(
                self,
                "Playlist already staged",
                "Write the pending change before editing this item again.",
            )
            return None
        return item
