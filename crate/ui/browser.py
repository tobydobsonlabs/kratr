"""A file-explorer-style navigator, shared by the folder and playlist steps.

Both steps browse a tree that is too deep to show flat — 22 genre folders with
sub-folders below, and 197 playlists nested in gig folders. Navigating in and out
with a breadcrumb beats one long indented list for both.

The two differ only in what they list and whether selection is single or multiple,
so the navigation lives here and each step supplies its own contents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_KEY_ROLE = Qt.ItemDataRole.UserRole
_KIND_ROLE = Qt.ItemDataRole.UserRole + 1


@dataclass(slots=True)
class Node:
    """One row in the browser."""

    key: str
    name: str
    #: Can be navigated into.
    is_container: bool = False
    #: Can be chosen as a destination / added to.
    selectable: bool = True
    detail: str = ""
    #: Full path shown in search results, where context is otherwise lost.
    context: str = ""


@dataclass(slots=True)
class Crumb:
    key: str
    name: str


class Navigator(QWidget):
    """Breadcrumb + list + search over a hierarchy."""

    #: A key was chosen (single-select) or toggled (multi-select).
    activated = Signal(str)
    #: Navigated into a container.
    navigated = Signal(list)

    def __init__(
        self,
        list_children: Callable[[str | None], list[Node]],
        *,
        search_all: Callable[[str], list[Node]] | None = None,
        multi_select: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_children = list_children
        self._search_all = search_all
        self._multi = multi_select
        self._trail: list[Crumb] = [Crumb("", "All")]
        self._selected: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.up_button = QPushButton("↑")
        self.up_button.setFixedWidth(38)
        self.up_button.setToolTip("Go up one level")
        self.up_button.clicked.connect(self.go_up)
        bar.addWidget(self.up_button)

        self.breadcrumb = QLabel()
        self.breadcrumb.setObjectName("breadcrumb")
        self.breadcrumb.setTextFormat(Qt.TextFormat.RichText)
        self.breadcrumb.setWordWrap(True)
        bar.addWidget(self.breadcrumb, stretch=1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(280)
        self.search.textChanged.connect(self._on_search)
        bar.addWidget(self.search)
        layout.addLayout(bar)

        self.list = QListWidget()
        self.list.setObjectName("browserList")
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.itemClicked.connect(self._on_click)
        layout.addWidget(self.list, stretch=1)

        self.refresh()

    # ------------------------------------------------------------- navigation
    @property
    def current_key(self) -> str | None:
        return self._trail[-1].key or None

    def refresh(self) -> None:
        if self.search.text().strip():
            self._on_search(self.search.text())
            return

        self.list.clear()
        for node in self._list_children(self.current_key):
            self.list.addItem(self._make_item(node))

        parts = [f"<span style='color:#737373'>{c.name}</span>" for c in self._trail[:-1]]
        parts.append(f"<b>{self._trail[-1].name}</b>")
        self.breadcrumb.setText(" &nbsp;›&nbsp; ".join(parts))
        self.up_button.setEnabled(len(self._trail) > 1)

    def _make_item(self, node: Node, show_context: bool = False) -> QListWidgetItem:
        icon = "📁" if node.is_container else "•"
        text = f"{icon}  {node.name}"
        if node.detail:
            text += f"    {node.detail}"
        if show_context and node.context:
            text += f"      ({node.context})"

        item = QListWidgetItem(text)
        item.setData(_KEY_ROLE, node.key)
        item.setData(_KIND_ROLE, "container" if node.is_container else "leaf")

        if self._multi and node.selectable:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if node.key in self._selected else Qt.CheckState.Unchecked
            )
        elif not node.selectable:
            item.setForeground(Qt.GlobalColor.gray)
        return item

    def enter(self, key: str, name: str) -> None:
        self._trail.append(Crumb(key, name))
        self.search.clear()
        self.refresh()
        self.navigated.emit([c.key for c in self._trail])

    def go_up(self) -> None:
        if len(self._trail) > 1:
            self._trail.pop()
            self.search.clear()
            self.refresh()
            self.navigated.emit([c.key for c in self._trail])

    def go_to_path(self, crumbs: list[Crumb]) -> None:
        """Jump straight to a location — used to reveal where a track already lives."""
        self._trail = [Crumb("", "All"), *crumbs]
        self.search.clear()
        self.refresh()

    # -------------------------------------------------------------- selection
    def selected_keys(self) -> list[str]:
        return sorted(self._selected)

    def set_selected(self, keys: list[str]) -> None:
        self._selected = set(keys)
        self.refresh()

    def _on_click(self, item: QListWidgetItem) -> None:
        key = item.data(_KEY_ROLE)
        if self._multi:
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                checked = item.checkState() == Qt.CheckState.Checked
                # itemClicked fires after the checkbox toggles, so read the new state.
                if checked:
                    self._selected.add(key)
                else:
                    self._selected.discard(key)
                self.activated.emit(key)
        elif item.data(_KIND_ROLE) == "container":
            self._selected = {key}
            self.activated.emit(key)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        if item.data(_KIND_ROLE) == "container":
            name = item.text().split("  ", 1)[-1].split("    ")[0]
            self.enter(item.data(_KEY_ROLE), name)

    # ----------------------------------------------------------------- search
    def _on_search(self, text: str) -> None:
        needle = text.strip()
        if not needle:
            self.refresh()
            return
        if self._search_all is None:
            return

        self.list.clear()
        for node in self._search_all(needle):
            self.list.addItem(self._make_item(node, show_context=True))
        self.breadcrumb.setText(f"<b>Search:</b> {needle}")
