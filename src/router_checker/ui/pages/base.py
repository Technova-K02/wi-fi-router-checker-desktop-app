"""Scrollable page with a transparent background (so Mica shows through)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import ScrollArea, TitleLabel


class Page(ScrollArea):
    def __init__(self, object_name: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.view = QWidget(self)
        self.view.setObjectName("view")
        self.setWidget(self.view)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea {border: none; background: transparent}")
        self.view.setStyleSheet("#view {background: transparent}")

        self.body = QVBoxLayout(self.view)
        self.body.setContentsMargins(36, 12, 36, 36)
        self.body.setSpacing(16)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.header = QHBoxLayout()
        self.header.setSpacing(12)
        self.title = TitleLabel(title, self.view)
        self.header.addWidget(self.title)
        self.header.addStretch(1)
        self.body.addLayout(self.header)
