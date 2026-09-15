"""Layout helpers."""

from __future__ import annotations

from PySide6.QtWidgets import QLayout


def clear_layout(layout: QLayout) -> None:
    """Empty a layout, removing its widgets from the screen immediately.

    ``takeAt()`` detaches a widget from the *layout* but leaves it parented to the
    container, so it carries on painting at its old geometry until the deferred
    ``deleteLater()`` finally runs. Rebuilding on top of that shows the stale widgets
    sitting exactly where they were — which looks precisely like nothing happened.
    Reparenting to ``None`` first is what actually removes them.
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            clear_layout(child)
