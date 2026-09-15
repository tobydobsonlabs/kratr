"""Application bootstrap."""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import config
from .branding import application_icon
from .main_window import MainWindow

logger = logging.getLogger(__name__)

STYLE_PATH = Path(__file__).resolve().parent.parent / "resources" / "style.qss"
SINGLE_INSTANCE_NAME = "KRATR-desktop-single-instance"


class SingleInstance(QObject):
    """One GUI process per desktop session, with activation forwarding."""

    activation_requested = Signal()

    def __init__(self, name: str = SINGLE_INSTANCE_NAME) -> None:
        super().__init__()
        self.name = name
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._receive_activation)
        self.is_primary = False

    @staticmethod
    def _notify_existing(name: str) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(name)
        if not socket.waitForConnected(350):
            return False
        socket.write(b"activate")
        socket.flush()
        socket.waitForBytesWritten(350)
        socket.disconnectFromServer()
        return True

    def acquire(self) -> bool:
        if self._notify_existing(self.name):
            return False

        # A crash can leave a stale local-server name behind. Only remove it after
        # proving there is no process accepting connections.
        QLocalServer.removeServer(self.name)
        if self.server.listen(self.name):
            self.is_primary = True
            return True

        # Resolve the rare race where two launches both checked before either listened.
        if self._notify_existing(self.name):
            return False
        logger.error("Could not acquire the single-instance server: %s", self.server.errorString())
        return False

    def _receive_activation(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is not None:
                socket.waitForReadyRead(100)
                socket.readAll()
                socket.disconnectFromServer()
        self.activation_requested.emit()

    def close(self) -> None:
        if self.server.isListening():
            self.server.close()
        if self.is_primary:
            QLocalServer.removeServer(self.name)
        self.is_primary = False


def install_error_reporting(app: QApplication) -> None:
    """Make unexpected failures visible instead of silent.

    A packaged GUI has no console, so an exception raised inside a slot printed its
    traceback into the void — a button would simply do nothing, with no clue why.
    That is exactly how a KeyError on a colour ID looked like "Import is broken".
    Now it is logged and shown.
    """
    def handle(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return

        logger.critical("Unhandled error", exc_info=(exc_type, exc, tb))
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Something went wrong")
            box.setText(
                f"<b>{exc_type.__name__}:</b> {exc}<br><br>"
                f"The action didn't complete. Nothing has been written to rekordbox."
            )
            box.setInformativeText(
                f"Details are in {config.LOG_DIR / config.LOG_FILE_NAME}"
            )
            box.setDetailedText(details)
            box.exec()
        except Exception:  # noqa: BLE001 - reporting must never cause a second failure
            logger.exception("Could not display the error dialog")

    sys.excepthook = handle


def apply_appearance(app: QApplication) -> None:
    """Name, icon and stylesheet — everything a KRATR-styled window (or the first-run
    walkthrough shown before the main window) needs. Idempotent."""
    app.setApplicationName(config.APP_NAME)
    app.setOrganizationName(config.APP_NAME)
    icon = application_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    if STYLE_PATH.is_file():
        app.setStyleSheet(STYLE_PATH.read_text(encoding="utf-8"))


def build_app(argv: list[str] | None = None) -> tuple[QApplication, MainWindow]:
    app = QApplication.instance() or QApplication(argv or sys.argv)
    apply_appearance(app)
    icon = application_icon()

    config.ensure_dirs()
    install_error_reporting(app)
    window = MainWindow(config.Settings.load())
    if not icon.isNull():
        window.setWindowIcon(icon)
    return app, window  # type: ignore[return-value]


def run() -> int:
    # QLocalSocket needs a Qt application object, but the second process exits before
    # constructing MainWindow, reading rekordbox, or flashing another window.
    app = QApplication.instance() or QApplication(sys.argv)
    instance = SingleInstance()
    if not instance.acquire():
        return 0

    # First run on this machine: no settings yet. Walk the user through setup before
    # the main window reads the library. If they close it without finishing, nothing is
    # written and we exit cleanly, so the next launch offers the walkthrough again.
    if not config.SETTINGS_PATH.is_file():
        apply_appearance(app)
        from .onboarding import run_first_run

        if not run_first_run(app):
            return 0

    app, window = build_app()
    instance.activation_requested.connect(window.bring_to_front)
    app.aboutToQuit.connect(instance.close)
    window.show()
    return app.exec()
