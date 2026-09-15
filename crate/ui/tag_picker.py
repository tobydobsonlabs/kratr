"""Tag and colour pickers — the last two steps of the pipeline.

Tagging should be a few clicks, not a menu-dive, so all four categories are shown at
once as toggleable chips with an inline "+ New" on each.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..rekordbox.colours import Colour
from ..rekordbox.mytags import MAX_TAGS_PER_CATEGORY, Category


class TagChip(QPushButton):
    def __init__(self, tag_id: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(name, parent)
        self.tag_id = tag_id
        self.setCheckable(True)
        self.setObjectName("tagChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)


class TagPicker(QWidget):
    """All four categories at once, with inline tag creation."""

    selection_changed = Signal(list)
    tag_requested = Signal(str, str)  # category_id, new tag name

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected: set[str] = set()
        self._categories: list[Category] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter tags…")
        self.search.textChanged.connect(self._apply_filter)
        outer.addWidget(self.search)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._body = QWidget()
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(self._body)
        outer.addWidget(scroll, stretch=1)

        self._chips: list[TagChip] = []

    def set_categories(self, categories: list[Category]) -> None:
        self._categories = categories
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chips = []

        for category in categories:
            self._layout.addWidget(self._build_category(category))
        self._layout.addStretch(1)
        self._apply_filter(self.search.text())

    def _build_category(self, category: Category) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 4, 0, 8)

        header = QHBoxLayout()
        title = QLabel(category.name.upper())
        title.setObjectName("categoryTitle")
        header.addWidget(title)
        count = QLabel(f"{len(category.tags)}/{MAX_TAGS_PER_CATEGORY}")
        count.setObjectName("muted")
        header.addWidget(count)
        header.addStretch(1)

        add = QPushButton("+ New")
        add.setObjectName("linkButton")
        add.clicked.connect(lambda _=False, c=category: self._create_tag(c))
        add.setEnabled(not category.is_full)
        if category.is_full:
            add.setToolTip(f"{category.name} is at rekordbox's {MAX_TAGS_PER_CATEGORY}-tag limit")
        header.addWidget(add)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setSpacing(4)
        for index, tag in enumerate(category.tags):
            chip = TagChip(tag.id, tag.name)
            chip.setChecked(tag.id in self._selected)
            chip.toggled.connect(lambda checked, t=tag.id: self._on_toggled(t, checked))
            self._chips.append(chip)
            grid.addWidget(chip, index // 4, index % 4)
        layout.addLayout(grid)
        return box

    def _create_tag(self, category: Category) -> None:
        name, ok = QInputDialog.getText(self, f"New tag in {category.name}", "Tag name:")
        if not ok or not name.strip():
            return
        if category.is_full:
            QMessageBox.warning(
                self,
                "Category full",
                f"{category.name} already has {MAX_TAGS_PER_CATEGORY} tags, which is "
                f"rekordbox's limit. Delete one first, or move it to another category.",
            )
            return
        self.tag_requested.emit(category.id, name.strip())

    def _on_toggled(self, tag_id: str, checked: bool) -> None:
        if checked:
            self._selected.add(tag_id)
        else:
            self._selected.discard(tag_id)
        self.selection_changed.emit(self.selected())

    def selected(self) -> list[str]:
        return sorted(self._selected)

    def set_selected(self, tag_ids: list[str]) -> None:
        self._selected = set(tag_ids)
        for chip in self._chips:
            chip.blockSignals(True)
            chip.setChecked(chip.tag_id in self._selected)
            chip.blockSignals(False)

    def clear_selection(self) -> None:
        self.set_selected([])

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for chip in self._chips:
            chip.setVisible(not needle or needle in chip.text().casefold())


class ColourPicker(QWidget):
    """The final step: one colour per track, with labels editable inline.

    Renaming a colour never disturbs the tracks already set to it — they reference the
    colour ID, not its label — so the meaning can change at any time.
    """

    colour_changed = Signal(object)          # colour id or None
    rename_requested = Signal(str, str)      # colour id, new label

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected: str | None = None
        self._buttons: dict[str, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._grid = QGridLayout()
        self._grid.setSpacing(6)
        layout.addLayout(self._grid)

        hint = QLabel("Double-click a colour to rename it. Renaming keeps every track already set to it.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def set_colours(self, colours: list[Colour]) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons = {}

        for index, colour in enumerate(colours):
            button = _ColourButton(colour)
            button.setChecked(colour.id == self._selected)
            button.clicked.connect(lambda _=False, c=colour.id: self._on_clicked(c))
            button.double_clicked.connect(lambda c=colour: self._rename(c))
            self._buttons[colour.id] = button
            self._grid.addWidget(button, index // 4, index % 4)

    def _on_clicked(self, colour_id: str) -> None:
        # Clicking the selected colour again clears it.
        self._selected = None if self._selected == colour_id else colour_id
        for cid, button in self._buttons.items():
            button.setChecked(cid == self._selected)
        self.colour_changed.emit(self._selected)

    def _rename(self, colour: Colour) -> None:
        name, ok = QInputDialog.getText(
            self, "Rename colour", f"Label for this colour:", text=colour.name
        )
        if ok and name.strip():
            self.rename_requested.emit(colour.id, name.strip())

    def selected(self) -> str | None:
        return self._selected

    def set_selected(self, colour_id: str | None) -> None:
        self._selected = colour_id
        for cid, button in self._buttons.items():
            button.setChecked(cid == colour_id)


class _ColourButton(QPushButton):
    double_clicked = Signal()

    def __init__(self, colour: Colour, parent: QWidget | None = None) -> None:
        label = colour.name
        if colour.track_count:
            label += f"  ({colour.track_count})"
        super().__init__(label, parent)
        self.colour_id = colour.id
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(34)
        self.setStyleSheet(
            f"QPushButton {{ border-left: 10px solid {colour.swatch}; text-align: left;"
            f" padding-left: 8px; }}"
            f"QPushButton:checked {{ border: 2px solid {colour.swatch};"
            f" border-left: 10px solid {colour.swatch}; font-weight: 600; }}"
        )

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)
