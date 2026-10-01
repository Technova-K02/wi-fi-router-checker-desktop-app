"""Set up a middle router the app spotted: confirm it and type its port."""

from __future__ import annotations

from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBoxBase,
    SubtitleLabel,
)

from router_checker.core.middle import DEFAULT_PORT, Endpoint


class MiddleRouterDialog(MessageBoxBase):
    def __init__(self, host: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.host = host
        title = SubtitleLabel(f"Use {host} as your middle router?", self)
        text = BodyLabel(
            "Router Checker will ask it to switch routers with "
            f"http://{host}:<port>/change_router?router=<Wi-Fi MAC>, without a login. "
            "Nothing is sent to it before you confirm.",
            self,
        )
        text.setWordWrap(True)
        self.port = LineEdit(self)
        self.port.setText(str(DEFAULT_PORT))
        self.port.setValidator(QIntValidator(1, 65535, self.port))
        self.port.setFixedWidth(120)
        self.port.setAccessibleName("Port of the middle router")
        row = QHBoxLayout()
        row.addWidget(BodyLabel("Port", self))
        row.addWidget(self.port)
        row.addStretch(1)
        self.error = CaptionLabel("", self)
        self.error.setTextColor("#C42B1C", "#FF99A4")
        self.error.hide()

        self.viewLayout.addWidget(title)
        self.viewLayout.addWidget(text)
        self.viewLayout.addLayout(row)
        self.viewLayout.addWidget(self.error)
        self.widget.setMinimumWidth(460)
        self.yesButton.setText("Use it")
        self.cancelButton.setText("Cancel")
        self.port.setFocus()
        self.port.selectAll()

    def endpoint(self) -> Endpoint | None:
        text = self.port.text().strip()
        if not text.isdigit() or not 1 <= int(text) <= 65535:
            return None
        return Endpoint(self.host, int(text))

    def validate(self) -> bool:
        ok = self.endpoint() is not None
        self.error.setText("" if ok else "Type the port it listens on, 1 to 65535.")
        self.error.setVisible(not ok)
        self.port.setError(not ok)
        return ok


def show_middle_test(parent: QWidget, endpoint: Endpoint, problem: str | None) -> None:
    """The result of trying the middle router, as a message at the top of the window."""
    if problem is None:
        InfoBar.success(
            "The middle router answers", f"Router Checker can reach it at {endpoint}.",
            duration=6000, position=InfoBarPosition.TOP, parent=parent,
        )  # fmt: skip
    else:
        InfoBar.warning(
            "No answer from the middle router", f"{endpoint}: {problem}.",
            duration=8000, position=InfoBarPosition.TOP, parent=parent,
        )  # fmt: skip
