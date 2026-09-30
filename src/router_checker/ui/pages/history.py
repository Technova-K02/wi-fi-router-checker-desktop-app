"""History: recent checks and the event log (charts come in Phase 3)."""

from __future__ import annotations

from PySide6.QtWidgets import QTableWidgetItem, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, SubtitleLabel, TableWidget, qconfig

from router_checker.core.models import CheckRecord
from router_checker.core.presentation import (
    DASH,
    fmt_clock,
    fmt_ms,
    fmt_pct,
    reasons_text,
    verdict_level,
)
from router_checker.ui.controller import AppController, HistoryData
from router_checker.ui.pages.base import Page
from router_checker.ui.style import level_text_color
from router_checker.ui.tables import fill_events, style_table

ALL = "__all__"
CHECK_COLUMNS = ["Time", "Network", "Result", "Gateway p95", "Internet", "Loss", "Jitter", "Score"]


class HistoryPage(Page):
    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__("historyPage", "History", parent)
        self.controller = controller
        self.header.addWidget(CaptionLabel("Show", self.view))
        self.filter = ComboBox(self.view)
        self.filter.setMinimumWidth(200)
        self.filter.setAccessibleName("Show the history of")
        self.filter.currentIndexChanged.connect(lambda _i: self.reload())
        self.header.addWidget(self.filter)

        self.body.addWidget(SubtitleLabel("Checks", self.view))
        self.checks = TableWidget(self.view)
        self.checks.setColumnCount(len(CHECK_COLUMNS))
        self.checks.setHorizontalHeaderLabels(CHECK_COLUMNS)
        style_table(self.checks)
        self.checks.setMinimumHeight(360)
        self.body.addWidget(self.checks)

        self.body.addWidget(SubtitleLabel("Events", self.view))
        self.events = TableWidget(self.view)
        self.events.setColumnCount(3)
        self.events.setHorizontalHeaderLabels(["Time", "Router", "Event"])
        style_table(self.events)
        self.events.setMinimumHeight(240)
        self.body.addWidget(self.events)

        controller.settingsChanged.connect(self._fill_filter)
        controller.checkFinished.connect(self._reload_if_visible)
        qconfig.themeChanged.connect(self._reload_if_visible)  # result colors
        self._fill_filter()

    def _fill_filter(self, *_args: object) -> None:
        current = self.filter.currentData() if self.filter.count() else ALL
        self.filter.blockSignals(True)
        self.filter.clear()
        self.filter.addItem("All networks", userData=ALL)
        for router in self.controller.settings.routers:
            self.filter.addItem(router.name, userData=router.id)
        index = self.filter.findData(current)
        self.filter.setCurrentIndex(max(0, index))
        self.filter.blockSignals(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.reload()

    def _reload_if_visible(self, *_args: object) -> None:
        if self.isVisible():
            self.reload()

    def reload(self) -> None:
        data = self.filter.currentData()
        router_id = None if data in (None, ALL) else data
        self.controller.load_history(router_id, self._show)

    def _show(self, data: HistoryData) -> None:
        now = self.controller.now()
        names = {r.id: r.name for r in self.controller.settings.routers}
        self.checks.setRowCount(len(data.checks))
        for row, check in enumerate(data.checks):
            for col, item in enumerate(self._check_items(check, names, now)):
                self.checks.setItem(row, col, item)
        fill_events(self.events, data.events, now, router_names=names)

    @staticmethod
    def _check_items(check: CheckRecord, names: dict[str, str], now) -> list[QTableWidgetItem]:
        if check.router_id in names:
            network = names[check.router_id]
        elif check.ssid:
            network = f'"{check.ssid}" (not yours)'
        else:
            network = "Other network"
        level = verdict_level(check.verdict)
        result = f"{level.glyph} {check.verdict.value}"
        if check.reasons:
            result += f": {reasons_text(check.reasons)}"
        texts = [
            fmt_clock(check.timestamp, now),
            network,
            result,
            "no reply" if check.gateway_silent else fmt_ms(check.gateway_p95_ms),
            fmt_ms(check.internet_latency_ms),
            fmt_pct(check.internet_loss_pct),
            fmt_ms(check.internet_jitter_ms),
            DASH if check.score is None else str(round(check.score)),
        ]
        items = [QTableWidgetItem(text) for text in texts]
        items[2].setForeground(level_text_color(level))
        return items
