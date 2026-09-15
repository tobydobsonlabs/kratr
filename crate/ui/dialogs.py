"""Dialog helpers.

Exists because of one specific trap: ``QMessageBox.question()`` returns a plain
``int`` (16384 for Yes), **not** a ``QMessageBox.StandardButton`` member. So::

    answer is QMessageBox.StandardButton.Yes    # always False, silently
    answer == QMessageBox.StandardButton.Yes    # correct

An ``is`` comparison therefore makes every confirmation read as "cancelled", and the
action never happens — with no error to show for it. Worse, a test that patches
``question`` to return the enum member makes ``is`` pass, so the bug survives a green
suite. Route every confirmation through :func:`confirmed` instead.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QMessageBox, QWidget


def confirmed(answer: Any) -> bool:
    """True when a message-box result means Yes, whatever type Qt handed back."""
    try:
        return int(answer) == int(QMessageBox.StandardButton.Yes.value)
    except (TypeError, ValueError):
        return False


def ask(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    default_yes: bool = False,
) -> bool:
    """Ask a Yes/No question. Returns True only for an explicit Yes."""
    default = (
        QMessageBox.StandardButton.Yes if default_yes else QMessageBox.StandardButton.No
    )
    answer = QMessageBox.question(
        parent,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        default,
    )
    return confirmed(answer)
