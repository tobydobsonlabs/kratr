"""Applying the queued rekordbox changes.

Everything queued goes in together, because rekordbox has to be closed for any of it.
The dialog is the gate: it states what will happen, refuses to proceed while rekordbox
is running, and reports per-operation outcomes rather than a single pass/fail.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..rekordbox import session as session_mod
from ..rekordbox.queue import ApplyResult, OpQueue

logger = logging.getLogger(__name__)


class ApplySignals(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)      # ApplyResult
    failed = Signal(str)


class ApplyTask(QRunnable):
    def __init__(self, queue: OpQueue, settings: config.Settings) -> None:
        super().__init__()
        self.queue = queue
        self.settings = settings
        self.signals = ApplySignals()

    def run(self) -> None:  # noqa: D102
        pending_ids = {op.id for op in self.queue.pending}
        try:
            with session_mod.open_for_write(self.settings) as live:
                def progress(index: int, total: int, op: Any) -> None:
                    self.signals.progress.emit(index, total, op.label or op.kind)

                result: ApplyResult = self.queue.apply(live, progress=progress)
            self.signals.finished.emit(result)
        except session_mod.RekordboxRunningError as exc:
            self.signals.failed.emit(str(exc))
        except session_mod.SchemaBlockedError as exc:
            self.signals.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            # queue.apply() runs before the session context performs its final commit.
            # If that commit fails, none of those “applied” statuses are truthful.
            for op in self.queue.ops:
                if op.id in pending_ids and op.status == "applied":
                    op.status = "pending"
            self.queue.save()
            logger.exception("Apply failed")
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")


class ApplyDialog(QDialog):
    """Pre-flight, progress, and result for a batch apply."""

    def __init__(self, queue: OpQueue, settings: config.Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Apply to rekordbox")
        self.setObjectName("applyDialog")
        self.resize(620, 440)
        self.queue = queue
        self.settings = settings
        self.result_obj: ApplyResult | None = None
        self._task: ApplyTask | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        eyebrow = QLabel("KRATR / WRITE GATE")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)

        title = QLabel("APPLY TO REKORDBOX")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        self.summary = QLabel()
        self.summary.setObjectName("summaryBox")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, stretch=1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        self.buttons.accepted.connect(self.start)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.refresh()

    def refresh(self) -> None:
        pending = self.queue.pending
        kinds = self.queue.pending_by_kind()
        breakdown = ", ".join(f"{count} × {kind}" for kind, count in sorted(kinds.items()))
        self.summary.setText(
            f"<b>{len(pending)} change(s) ready</b><br>{breakdown or 'nothing queued'}"
        )

        status = session_mod.status(self.settings)
        can_apply = bool(pending) and not status["rekordbox_running"]

        if status["rekordbox_running"]:
            self.status.setText(
                "⚠ rekordbox is running. Close it first — it holds the library in memory and "
                "would overwrite anything written underneath it."
            )
        elif not pending:
            self.status.setText("Nothing to apply.")
        else:
            backup = status["latest_backup"] or "none yet"
            self.status.setText(
                f"rekordbox is closed. A timestamped backup of master.db "
                f"(and its write-ahead log) is taken before anything is written. "
                f"Last backup: {backup}."
            )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(can_apply)

    def start(self) -> None:
        from PySide6.QtCore import QThreadPool

        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, max(1, len(self.queue.pending)))
        self.log.append("Backing up and opening the library…")

        self._task = ApplyTask(self.queue, self.settings)
        self._task.signals.progress.connect(self._on_progress)
        self._task.signals.finished.connect(self._on_finished)
        self._task.signals.failed.connect(self._on_failed)
        QThreadPool.globalInstance().start(self._task)

    def _on_progress(self, index: int, total: int, label: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(index)
        self.log.append(f"[{index}/{total}] {label}")

    def _on_finished(self, result: ApplyResult) -> None:
        self._task = None
        self.result_obj = result
        self.progress.setValue(self.progress.maximum())
        self.log.append("")
        self.log.append(result.summary())
        for op in result.failed:
            self.log.append(f"  ✕ {op.label or op.kind}: {op.error}")
        if result.ok:
            self.log.append("Reopen rekordbox to see the changes.")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")

    def _on_failed(self, message: str) -> None:
        self._task = None
        self.progress.setVisible(False)
        self.log.append(f"✕ {message}")
        self.status.setText(message)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
