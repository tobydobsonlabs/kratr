"""Tag Manager — full control over the four category slots and every tag in them.

A dialog reachable at any time, not a one-time wizard. Changes are queued like every
other rekordbox write and applied when rekordbox is closed.

The four-slot ceiling is rekordbox's, and the dialog says so plainly at the point it
matters rather than failing silently when you look for a fifth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..rekordbox.mytags import MAX_CATEGORIES, MAX_TAGS_PER_CATEGORY, Category
from .dialogs import ask

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TagEdit:
    """One queued taxonomy change."""

    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    description: str = ""


class TagManagerDialog(QDialog):
    """Edit the taxonomy. Collects changes; the caller queues and applies them."""

    def __init__(self, categories: list[Category], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tag Manager")
        self.resize(760, 560)
        self._categories = categories
        self.edits: list[TagEdit] = []

        layout = QVBoxLayout(self)

        note = QLabel(
            f"rekordbox has exactly {MAX_CATEGORIES} category slots and no way to add a fifth — "
            f"it's the most-requested MyTag feature on Pioneer's forums. Each holds up to "
            f"{MAX_TAGS_PER_CATEGORY} tags. Rename a slot to repurpose it; clearing one empties "
            f"it rather than removing it."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        columns = QHBoxLayout()

        # --- categories -------------------------------------------------
        left = QVBoxLayout()
        left.addWidget(QLabel("Categories"))
        self.category_list = QListWidget()
        self.category_list.currentRowChanged.connect(self._on_category_selected)
        left.addWidget(self.category_list, stretch=1)

        for label, slot in (
            ("Rename…", self._rename_category),
            ("Clear (empty this slot)", self._clear_category),
            ("Move up", lambda: self._move_category(-1)),
            ("Move down", lambda: self._move_category(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            left.addWidget(button)
        columns.addLayout(left, stretch=1)

        # --- tags -------------------------------------------------------
        right = QVBoxLayout()
        self.tag_header = QLabel("Tags")
        right.addWidget(self.tag_header)
        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        right.addWidget(self.tag_list, stretch=1)

        for label, slot in (
            ("New tag…", self._create_tag),
            ("Rename…", self._rename_tag),
            ("Move to another category…", self._move_tag),
            ("Merge into…", self._merge_tag),
            ("Delete", self._delete_tag),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            right.addWidget(button)
        columns.addLayout(right, stretch=2)
        layout.addLayout(columns)

        self.pending_label = QLabel("No changes")
        self.pending_label.setObjectName("muted")
        self.pending_label.setWordWrap(True)
        layout.addWidget(self.pending_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._reload()

    # ------------------------------------------------------------------ helpers
    def _reload(self) -> None:
        self.category_list.blockSignals(True)
        self.category_list.clear()
        for category in self._categories:
            item = QListWidgetItem(f"{category.name}   ({len(category.tags)})")
            item.setData(Qt.ItemDataRole.UserRole, category.id)
            if category.is_default_name:
                item.setToolTip(
                    "This is a rekordbox stock name. Categories left unedited have been "
                    "reported to disappear from USB export — renaming it avoids the risk."
                )
            self.category_list.addItem(item)
        self.category_list.blockSignals(False)
        if self.category_list.count() and self.category_list.currentRow() < 0:
            self.category_list.setCurrentRow(0)
        self._refresh_tags()

    def current_category(self) -> Category | None:
        row = self.category_list.currentRow()
        if 0 <= row < len(self._categories):
            return self._categories[row]
        return None

    def _refresh_tags(self) -> None:
        category = self.current_category()
        self.tag_list.clear()
        if category is None:
            self.tag_header.setText("Tags")
            return
        self.tag_header.setText(
            f"Tags in {category.name} — {len(category.tags)}/{MAX_TAGS_PER_CATEGORY}"
        )
        for tag in category.tags:
            suffix = f"   ({tag.track_count} track{'s' if tag.track_count != 1 else ''})"
            item = QListWidgetItem(tag.name + (suffix if tag.track_count else ""))
            item.setData(Qt.ItemDataRole.UserRole, tag.id)
            self.tag_list.addItem(item)

    def _on_category_selected(self, _row: int) -> None:
        self._refresh_tags()

    def _record(self, action: str, description: str, **payload: Any) -> None:
        self.edits.append(TagEdit(action, payload, description))
        self.pending_label.setText(
            f"{len(self.edits)} pending change(s): " + "; ".join(e.description for e in self.edits[-4:])
        )

    def _selected_tags(self) -> list[tuple[str, str]]:
        return [
            (item.data(Qt.ItemDataRole.UserRole), item.text().split("   (")[0])
            for item in self.tag_list.selectedItems()
        ]

    # --------------------------------------------------------------- categories
    def _rename_category(self) -> None:
        category = self.current_category()
        if category is None:
            return
        name, ok = QInputDialog.getText(self, "Rename category", "Name:", text=category.name)
        if ok and name.strip():
            category.name = name.strip()
            self._record("rename_category", f"category → {name.strip()!r}",
                         category_id=category.id, name=name.strip())
            self._reload()

    def _clear_category(self) -> None:
        category = self.current_category()
        if category is None:
            return
        affected = sum(t.track_count for t in category.tags)
        if not ask(
            self,
            "Clear this category?",
            f"This deletes all {len(category.tags)} tag(s) in {category.name!r} and removes "
            f"them from {affected} track(s).\n\n"
            f"The slot itself stays — rekordbox always has {MAX_CATEGORIES} and there is no "
            f"way to remove one.\n\nContinue?",
        ):
            return
        self._record("clear_category", f"cleared {category.name!r}", category_id=category.id)
        category.tags = []
        self._reload()

    def _move_category(self, delta: int) -> None:
        row = self.category_list.currentRow()
        target = row + delta
        if not (0 <= row < len(self._categories) and 0 <= target < len(self._categories)):
            return
        self._categories[row], self._categories[target] = (
            self._categories[target], self._categories[row],
        )
        self._record(
            "reorder_categories",
            "reordered categories",
            ordered_ids=[c.id for c in self._categories],
        )
        self._reload()
        self.category_list.setCurrentRow(target)

    # --------------------------------------------------------------------- tags
    def _create_tag(self) -> None:
        category = self.current_category()
        if category is None:
            return
        if len(category.tags) >= MAX_TAGS_PER_CATEGORY:
            QMessageBox.warning(
                self, "Category full",
                f"{category.name!r} already has {MAX_TAGS_PER_CATEGORY} tags — rekordbox's limit.",
            )
            return
        name, ok = QInputDialog.getText(self, "New tag", f"Tag name in {category.name}:")
        if ok and name.strip():
            self._record("create_tag", f"+ {name.strip()!r}",
                         category_id=category.id, name=name.strip())
            from ..rekordbox.mytags import Tag

            category.tags.append(
                Tag(id=f"pending:{name.strip()}", name=name.strip(),
                    seq=len(category.tags) + 1, category_id=category.id)
            )
            self._reload()

    def _rename_tag(self) -> None:
        selected = self._selected_tags()
        if len(selected) != 1:
            QMessageBox.information(self, "Rename tag", "Select exactly one tag to rename.")
            return
        tag_id, current = selected[0]
        name, ok = QInputDialog.getText(self, "Rename tag", "Name:", text=current)
        if ok and name.strip():
            self._record("rename_tag", f"{current!r} → {name.strip()!r}",
                         tag_id=tag_id, name=name.strip())
            category = self.current_category()
            if category:
                for tag in category.tags:
                    if tag.id == tag_id:
                        tag.name = name.strip()
            self._reload()

    def _move_tag(self) -> None:
        selected = self._selected_tags()
        if not selected:
            return
        others = [c for c in self._categories if c is not self.current_category()]
        names = [c.name for c in others]
        choice, ok = QInputDialog.getItem(self, "Move tag", "Move to:", names, 0, False)
        if not ok:
            return
        target = others[names.index(choice)]
        for tag_id, name in selected:
            self._record("move_tag", f"{name!r} → {target.name}",
                         tag_id=tag_id, category_id=target.id)
        QMessageBox.information(
            self, "Tracks kept",
            "Moving a tag keeps every track already tagged with it — only which category "
            "it sits in changes.",
        )

    def _merge_tag(self) -> None:
        selected = self._selected_tags()
        if len(selected) != 1:
            QMessageBox.information(self, "Merge", "Select exactly one tag to merge from.")
            return
        source_id, source_name = selected[0]
        category = self.current_category()
        candidates = [t for t in (category.tags if category else []) if t.id != source_id]
        if not candidates:
            return
        names = [t.name for t in candidates]
        choice, ok = QInputDialog.getItem(
            self, "Merge tag", f"Merge {source_name!r} into:", names, 0, False
        )
        if ok:
            target = candidates[names.index(choice)]
            self._record("merge_tags", f"merge {source_name!r} → {target.name!r}",
                         source_id=source_id, target_id=target.id)

    def _delete_tag(self) -> None:
        selected = self._selected_tags()
        if not selected:
            return
        category = self.current_category()
        affected = sum(
            t.track_count for t in (category.tags if category else []) if t.id in {s[0] for s in selected}
        )
        if not ask(
            self,
            "Delete tag(s)?",
            f"Deleting {len(selected)} tag(s) removes them from {affected} track(s).\n\nContinue?",
        ):
            return
        for tag_id, name in selected:
            self._record("delete_tag", f"− {name!r}", tag_id=tag_id)
        if category:
            ids = {s[0] for s in selected}
            category.tags = [t for t in category.tags if t.id not in ids]
        self._reload()
