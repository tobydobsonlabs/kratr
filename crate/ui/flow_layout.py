"""A layout that wraps its children onto new rows as the window narrows.

Qt has no built-in flow layout. Tag and colour buttons need one so the whole
vocabulary stays visible and reflows instead of clipping or scrolling sideways.
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QWidget, QWidgetItem


class FlowWidget(QWidget):
    """A widget whose height follows its FlowLayout.

    Needed because a plain QWidget reports the layout's ``sizeHint`` — which for a
    wrapping layout is just the largest single item — so a parent vertical layout
    collapses it to nothing and the contents never appear. Propagating
    height-for-width is what actually makes the rows visible.
    """

    def __init__(self, spacing: int = 8, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._flow = FlowLayout(self, spacing=spacing)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._height_cache: dict[int, int] = {}

    def add(self, widget: QWidget) -> None:
        self._flow.addWidget(widget)
        self._height_cache.clear()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        # Memoised: Qt asks repeatedly during layout negotiation, and each answer costs
        # a full pass over every child. With a large taxonomy that adds up fast.
        cached = self._height_cache.get(width)
        if cached is None:
            cached = self._flow.heightForWidth(width)
            self._height_cache[width] = cached
        return cached

    def sizeHint(self) -> QSize:  # noqa: N802
        width = self.width() or 800
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Only renegotiate when the width genuinely changed. Calling updateGeometry()
        # on every resize invalidates the parent layout, which resizes this widget
        # again — an endless relayout loop that hung the Tags step outright once the
        # taxonomy grew large enough for the oscillation not to settle.
        if event.oldSize().width() != event.size().width():
            self._height_cache.clear()
            self.updateGeometry()


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin: int = 0, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self._spacing = spacing
        self.setContentsMargins(QMargins(margin, margin, margin, margin))

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _layout(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height = effective.x(), effective.y(), 0

        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisible():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing

            if next_x - self._spacing > effective.right() and row_height > 0:
                x = effective.x()
                y += row_height + self._spacing
                next_x = x + hint.width() + self._spacing
                row_height = 0

            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            row_height = max(row_height, hint.height())

        return y + row_height - rect.y() + margins.bottom()
