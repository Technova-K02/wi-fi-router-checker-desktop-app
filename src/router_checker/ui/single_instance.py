"""Only one copy of the app runs; starting it again brings the window forward.

``RouterChecker.exe --exit`` asks the running copy to close (the installer and
uninstaller use it). The copy answers with the ids of its processes, so the
caller can wait until the .exe is no longer in use.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SHOW, EXIT = b"show", b"exit"
CONNECT_MS = 500
REPLY_MS = 5000


class SingleInstance(QObject):
    activated = Signal()  # another launch asked us to show the window
    exitRequested = Signal()  # ``--exit`` asked us to close

    def __init__(self, name: str, pids: Sequence[int] = (), parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._pids = tuple(pids)  # this copy's processes, told to ``--exit``
        self._server: QLocalServer | None = None

    def already_running(self) -> bool:
        """If another copy runs, ask it to show itself and return True."""
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if socket.waitForConnected(CONNECT_MS):
            _ask(socket, SHOW)
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
            connection.readyRead.connect(lambda c=connection: self._read(c))
            self._read(connection)

    def _read(self, connection: QLocalSocket) -> None:
        # Every message gets a reply: the sender waits for it before hanging up,
        # because a message still unread when it hangs up is lost.
        while connection.canReadLine():
            if bytes(connection.readLine().data()).strip() == EXIT:
                connection.write(" ".join(map(str, self._pids)).encode() + b"\n")
                connection.flush()
                self.exitRequested.emit()
            else:
                connection.write(b"ok\n")
                connection.flush()
                self.activated.emit()


def _ask(socket: QLocalSocket, message: bytes) -> bytes:
    """Send a message to the running copy and return its reply (blocking)."""
    socket.write(message + b"\n")
    socket.waitForBytesWritten(REPLY_MS)  # without an event loop, only this sends it
    reply = b""
    while not reply.endswith(b"\n") and socket.waitForReadyRead(REPLY_MS):
        reply += bytes(socket.readAll().data())
    socket.disconnectFromServer()
    return reply


def request_exit(name: str) -> list[int] | None:
    """Ask the running copy to close; the ids of its processes, or None if no copy runs."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(CONNECT_MS):
        return None
    return [int(pid) for pid in _ask(socket, EXIT).split() if pid.isdigit()]
