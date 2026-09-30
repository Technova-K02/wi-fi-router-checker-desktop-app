"""Shared table helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidgetItem
from qfluentwidgets import TableWidget, setCustomStyleSheet

from router_checker.core.models import Event
from router_checker.core.presentation import DASH, fmt_clock


def style_table(table: TableWidget) -> None:
    table.setBorderVisible(True)
    table.setBorderRadius(8)
    table.setWordWrap(False)
    table.verticalHeader().hide()
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(True)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    qss = "QHeaderView::section:horizontal { padding-left: 16px; }"
    setCustomStyleSheet(table, qss, qss)


def fill_events(
    table: TableWidget,
    events: Sequence[Event],
    now: datetime,
    router_names: Mapping[str, str] | None = None,
) -> None:
    """Time, (router,) message. Pass ``router_names`` to include the router column."""
    table.setRowCount(len(events))
    for row, event in enumerate(events):
        cells = [fmt_clock(event.timestamp, now)]
        if router_names is not None:
            cells.append(router_names.get(event.router_id or "", DASH))
        cells.append(event.message)
        for col, text in enumerate(cells):
            table.setItem(row, col, QTableWidgetItem(text))
