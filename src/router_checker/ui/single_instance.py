"""Only one copy of the app runs; starting it again brings the window forward."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance(QObject):
    activated = Signal()  # another launch asked us to show the window

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._server: QLocalServer | None = None

    def already_running(self) -> bool:
        """If another copy runs, ask it to show itself and return True."""
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if socket.waitForConnected(500):
            socket.write(b"show\n")
            socket.flush()
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()
            return True
        self._server = QLocalServer(self)
        QLocalServer.removeServer(self._name)
        self._server.listen(self._name)
        self._server.newConnection.connect(self._on_connection)
        return False

    def _on_connection(self) -> None:
        while self._server is not None and self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            connection.disconnected.connect(connection.deleteLater)
            self.activated.emit()
