"""Shared table helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidgetItem
from qfluentwidgets import TableWidget, setCustomStyleSheet

from router_checker.core.models import Event
from router_checker.core.presentation import DASH, fmt_clock

SIZE_FROM_ROWS = 50  # rows measured when sizing columns to their contents


def style_table(table: TableWidget) -> None:
    table.setBorderVisible(True)
    table.setBorderRadius(8)
    table.setWordWrap(False)
    table.verticalHeader().hide()
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    header = table.horizontalHeader()
    # Not ResizeToContents: fill_table sizes the columns once per fill (see there),
    # from the first rows only; that's plenty for these tables and much faster.
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setResizeContentsPrecision(SIZE_FROM_ROWS)
    table.verticalHeader().setResizeContentsPrecision(SIZE_FROM_ROWS)
    header.setStretchLastSection(True)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    qss = "QHeaderView::section:horizontal { padding-left: 16px; }"
    setCustomStyleSheet(table, qss, qss)


def fill_table(table: TableWidget, rows: Sequence[Sequence[str | QTableWidgetItem]]) -> None:
    """Replace every row, then size the columns once.

    With per-cell column sizing, every replaced cell re-measured the whole table
    (through the Fluent cell delegate, in Python) and re-laid out the page:
    refilling a few hundred rows froze the app for minutes.
    """
    table.setUpdatesEnabled(False)
    try:
        table.clearContents()
        table.setRowCount(len(rows))
        for r, cells in enumerate(rows):
            for c, cell in enumerate(cells):
                item = cell if isinstance(cell, QTableWidgetItem) else QTableWidgetItem(cell)
                table.setItem(r, c, item)
        for column in range(table.columnCount() - 1):  # the last one stretches
            table.resizeColumnToContents(column)
    finally:
        table.setUpdatesEnabled(True)


def fill_events(
    table: TableWidget,
    events: Sequence[Event],
    now: datetime,
    router_names: Mapping[str, str] | None = None,
) -> None:
    """Time, (router,) message. Pass ``router_names`` to include the router column."""
    rows = []
    for event in events:
        cells = [fmt_clock(event.timestamp, now)]
        if router_names is not None:
            cells.append(router_names.get(event.router_id or "", DASH))
        cells.append(event.message)
        rows.append(cells)
    fill_table(table, rows)
