"""Routers: the list (add, edit, delete) and a details view per router."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    ToolTipFilter,
    TransparentToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from router_checker.core.models import CheckRecord, Router, ScanObservation
from router_checker.core.presentation import (
    DASH,
    band_text,
    busy_text,
    fmt_age,
    fmt_clock,
    fmt_dbm,
    fmt_ms,
    fmt_pct,
    reasons_text,
    score_text,
    verdict_level,
)
from router_checker.ui.controller import AppController, RouterDetails
from router_checker.ui.history_cards import PopularTimesCard, RouterHistoryCard
from router_checker.ui.pages.base import Page
from router_checker.ui.tables import fill_events, style_table
from router_checker.ui.widgets import ColorDot, FocusCard


def _tool_button(icon: FIF, tip: str, parent: QWidget) -> TransparentToolButton:
    button = TransparentToolButton(icon, parent)
    button.setToolTip(tip)
    button.setAccessibleName(tip)
    button.installEventFilter(ToolTipFilter(button))
    return button


class RouterRow(FocusCard):
    editRequested = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, router: Router, summary: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.router_id = router.id
        self.setFixedHeight(72)
        dot = ColorDot(router.color, 14, self)
        name = StrongBodyLabel(router.name, self)
        ssid = f'Wi-Fi "{router.ssid}"' if router.ssid else "No Wi-Fi name"
        count = len(router.macs)
        macs = f"{count} MAC address{'es' if count != 1 else ''}" if count else "no MAC yet"
        details = CaptionLabel(f"{ssid} · {macs}", self)
        state = BodyLabel(summary, self)
        edit = _tool_button(FIF.EDIT, f"Edit {router.name}", self)
        delete = _tool_button(FIF.DELETE, f"Delete {router.name}", self)
        edit.clicked.connect(lambda: self.editRequested.emit(self.router_id))
        delete.clicked.connect(lambda: self.deleteRequested.emit(self.router_id))

        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(name)
        text.addWidget(details)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 12, 10)
        layout.setSpacing(14)
        layout.addWidget(dot)
        layout.addLayout(text, 1)
        layout.addWidget(state)
        layout.addSpacing(8)
        layout.addWidget(edit)
        layout.addWidget(delete)
        self.setAccessibleName(f"{router.name}, {summary}. {ssid}, {macs}")
        self.setToolTip("Open router details")


class InfoCard(SimpleCardWidget):
    """A titled card with label/value rows."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = SubtitleLabel(title, self)
        self.note = BodyLabel("", self)
        self.note.setWordWrap(True)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnMinimumWidth(0, 140)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(self.title)
        layout.addWidget(self.note)
        layout.addLayout(self.grid)
        self._rows: list[QWidget] = []

    def set_rows(self, rows: list[tuple[str, str]], note: str = "") -> None:
        for widget in self._rows:
            self.grid.removeWidget(widget)
            widget.deleteLater()
        self._rows = []
        for i, (label, value) in enumerate(rows):
            name = CaptionLabel(label, self)
            text = BodyLabel(value, self)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            text.setWordWrap(True)
            self.grid.addWidget(name, i, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(text, i, 1, Qt.AlignmentFlag.AlignTop)
            self._rows += [name, text]
        self.note.setText(note)
        self.note.setVisible(bool(note))


class RouterDetailsView(Page):
    back = Signal()
    editRequested = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__("routerDetails", "", parent)
        self.controller = controller
        self.router_id: str | None = None
        back = _tool_button(FIF.RETURN, "Back to all routers (Esc)", self.view)
        back.clicked.connect(self.back)
        self.dot = ColorDot(size=16, parent=self.view)
        self.header.insertWidget(0, back)
        self.header.insertWidget(1, self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        edit = PushButton(FIF.EDIT, "Edit", self.view)
        delete = PushButton(FIF.DELETE, "Delete", self.view)
        edit.clicked.connect(lambda: self.editRequested.emit(self.router_id))
        delete.clicked.connect(lambda: self.deleteRequested.emit(self.router_id))
        self.header.addWidget(edit)
        self.header.addWidget(delete)

        self.identity = InfoCard("Identity", self.view)
        self.last_check = InfoCard("Latest test", self.view)
        self.last_seen = InfoCard("Last seen nearby", self.view)
        self.history_card = RouterHistoryCard(controller, self.view)
        self.popular_card = PopularTimesCard(controller, self.view)
        events_title = SubtitleLabel("Events", self.view)
        self.events = TableWidget(self.view)
        self.events.setColumnCount(2)
        self.events.setHorizontalHeaderLabels(["Time", "Event"])
        style_table(self.events)
        self.events.setMinimumHeight(220)

        for widget in (
            self.identity,
            self.last_check,
            self.last_seen,
            self.history_card,
            self.popular_card,
            events_title,
            self.events,
        ):
            self.body.addWidget(widget)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.back.emit)
        controller.checkFinished.connect(self._reload)
        controller.settingsChanged.connect(self._reload)

    def show_router(self, router_id: str) -> None:
        self.router_id = router_id
        self._reload()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._reload_charts()

    def _reload(self, *_args: object) -> None:
        router = self.controller.settings.router(self.router_id) if self.router_id else None
        if router is None:
            return
        self.title.setText(router.name)
        self.dot.set_color(router.color)
        macs = "\n".join(str(m) for m in router.macs) or "None yet"
        self.identity.set_rows(
            [("Wi-Fi name (SSID)", router.ssid or DASH), ("MAC addresses", macs)],
            "MAC addresses are linked automatically when you connect to this router.",
        )
        self.controller.load_router_details(router.id, self._show_details)
        if self.isVisible():
            self._reload_charts()

    def _reload_charts(self) -> None:
        if self.router_id is not None and self.controller.settings.router(self.router_id):
            self.history_card.show_router(self.router_id)
            self.popular_card.show_router(self.router_id)

    def _show_details(self, details: RouterDetails) -> None:
        if details.router_id != self.router_id:
            return
        now = self.controller.now()
        self.last_check.set_rows(*_check_rows(details.last_check, now))
        self.last_seen.set_rows(*_seen_rows(details.last_seen, now))
        fill_events(self.events, details.events, now)


def _check_rows(check: CheckRecord | None, now) -> tuple[list[tuple[str, str]], str]:
    if check is None:
        return [], "No test yet. A router is tested while you're connected to it."
    level = verdict_level(check.verdict)
    result = f"{level.glyph} {check.verdict.value}"
    if check.reasons:
        result += f": {reasons_text(check.reasons)}"
    gateway = (
        "doesn't answer ping"
        if check.gateway_silent
        else f"{fmt_ms(check.gateway_avg_ms)} average, p95 {fmt_ms(check.gateway_p95_ms)}"
    )
    return [
        ("When", f"{fmt_clock(check.timestamp, now)} ({fmt_age(check.timestamp, now)})"),
        ("Result", result),
        ("Gateway ping", gateway),
        ("Internet ping", fmt_ms(check.internet_latency_ms)),
        ("Packet loss", fmt_pct(check.internet_loss_pct)),
        ("Jitter", fmt_ms(check.internet_jitter_ms)),
        ("Score", DASH if check.score is None else f"{round(check.score)} (this test)"),
    ], ""


def _seen_rows(obs: ScanObservation | None, now) -> tuple[list[tuple[str, str]], str]:
    if obs is None:
        return [], "Not seen in a Wi-Fi scan yet."
    e = obs.entry
    return [
        ("When", f"{fmt_clock(obs.timestamp, now)} ({fmt_age(obs.timestamp, now)})"),
        ("Network", f'"{e.ssid}" · {e.bssid}' if e.ssid else str(e.bssid)),
        ("Signal", f"{fmt_dbm(e.rssi)} (quality {e.link_quality}%)"),
        ("Band", band_text(e)),
        ("Same channel", f"{obs.same_channel_count} other network(s)"),
        ("Busy", busy_text(obs.busyness, e)),
    ], ""


class RoutersPage(QStackedWidget):
    addRequested = Signal()
    editRequested = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("routersPage")
        self.controller = controller
        self.list_page = Page("routerList", "Routers", self)
        add = PrimaryPushButton(FIF.ADD, "Add router", self.list_page.view)
        add.setToolTip("Add a router (Ctrl+N)")
        add.clicked.connect(self.addRequested)
        self.list_page.header.addWidget(add)
        hint = CaptionLabel(
            "Routers are recognized by their Wi-Fi name (SSID) or MAC addresses. "
            "Select one to see its details.",
            self.list_page.view,
        )
        hint.setWordWrap(True)
        self.list_page.body.addWidget(hint)
        self.rows_host = QWidget(self.list_page.view)
        self.rows = QVBoxLayout(self.rows_host)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(8)
        self.list_page.body.addWidget(self.rows_host)
        self.empty = BodyLabel(
            "No routers yet. Add the routers you can connect to.", self.list_page.view
        )
        self.list_page.body.addWidget(self.empty)

        self.details = RouterDetailsView(controller, self)
        self.details.back.connect(self.show_list)
        self.details.editRequested.connect(self.editRequested)
        self.details.deleteRequested.connect(self.deleteRequested)
        self.addWidget(self.list_page)
        self.addWidget(self.details)

        controller.settingsChanged.connect(self._rebuild)
        controller.checkFinished.connect(self._rebuild)
        self._rebuild()

    def show_list(self) -> None:
        self.setCurrentWidget(self.list_page)

    def show_details(self, router_id: str) -> None:
        if self.controller.settings.router(router_id) is None:
            self.show_list()
            return
        self.details.show_router(router_id)
        self.setCurrentWidget(self.details)

    def _rebuild(self, *_args: object) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        snap = self.controller.last_snapshot
        statuses = {s.router.id: s for s in snap.report.statuses} if snap else {}
        routers = self.controller.settings.routers
        for router in routers:
            status = statuses.get(router.id)
            if status is None:
                summary = "Waiting for a check"
            else:
                summary = status.state.value
                if status.score is not None:
                    summary += f" · {score_text(status.score)}"
            row = RouterRow(router, summary, self.rows_host)
            row.activated.connect(lambda rid=router.id: self.show_details(rid))
            row.editRequested.connect(self.editRequested)
            row.deleteRequested.connect(self.deleteRequested)
            self.rows.addWidget(row)
        self.empty.setVisible(not routers)
        if (
            self.currentWidget() is self.details
            and self.controller.settings.router(self.details.router_id or "") is None
        ):
            self.show_list()
