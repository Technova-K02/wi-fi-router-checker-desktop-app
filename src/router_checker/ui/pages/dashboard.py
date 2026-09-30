"""Dashboard: the current router in a large card, every other router in a small one."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FlowLayout,
    IconWidget,
    IndeterminateProgressBar,
    IndeterminateProgressRing,
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

from router_checker.core.models import Recommendation, Router, RouterState, RouterStatus, Score
from router_checker.core.presentation import (
    DASH,
    band_text,
    busy_text,
    current_metrics,
    fmt_clock,
    fmt_countdown,
    fmt_dbm,
    fmt_ms,
    fmt_pct,
    overall_status,
    recommendation_text,
    score_text,
    state_detail,
    targets_text,
    vpn_text,
)
from router_checker.core.switching import ON_ETHERNET
from router_checker.ui.controller import SPARKLINE_SPAN, AppController, RunState, Snapshot
from router_checker.ui.pages.base import Page
from router_checker.ui.shell import open_location_settings
from router_checker.ui.widgets import (
    Badge,
    ColorDot,
    FocusCard,
    MetricBlock,
    ScoreRing,
    Sparkline,
    StatusLine,
)

LOCATION_OFF_TEXT = (
    "Windows only shares Wi-Fi details with desktop apps when location access is on. "
    "Your current router is still tested (it's recognized by its gateway MAC), but other "
    "routers can't be seen."
)
STATE_ICONS = {
    RouterState.ONLINE: FIF.ACCEPT,
    RouterState.VISIBLE: FIF.WIFI,
    RouterState.NOT_FOUND: FIF.REMOVE,
    RouterState.UNKNOWN: FIF.QUESTION,
}
RUN_TITLES = {
    "test_all": "Testing all routers",
    "switch": "Switching networks",
    "recover": "Going back",
}
TEST_ALL_TIP = "Connect to each of your routers in turn and test it (Ctrl+T)"
TEST_ALL_NEEDS_LOCATION = "Test all needs location access (see the message below)"


class RunCard(SimpleCardWidget):
    """What Test all (or a switch) is doing, with a progress bar and Cancel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon = IconWidget(FIF.ROTATE, self)
        self.icon.setFixedSize(20, 20)
        self.title = StrongBodyLabel(self)
        self.detail = BodyLabel(self)
        self.detail.setWordWrap(True)
        self.bar = ProgressBar(self, useAni=False)
        self.busy_bar = IndeterminateProgressBar(self, start=False)
        self.cancel_button = PushButton(FIF.CLOSE, "Cancel", self)
        self.cancel_button.setToolTip("Stop after this step and reconnect to where you were")

        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(self.title)
        texts.addWidget(self.detail)
        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addLayout(texts, 1)
        top.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addWidget(self.bar)
        layout.addWidget(self.busy_bar)
        self.hide()

    def show_state(self, state: RunState | None) -> None:
        self.setVisible(state is not None)
        if state is None:
            self.busy_bar.stop()
            return
        self.title.setText(RUN_TITLES[state.kind])
        self.detail.setText(state.text)
        self.bar.setVisible(state.steps is not None)
        self.busy_bar.setVisible(state.steps is None)
        if state.steps is not None:
            done, steps = state.steps
            self.bar.setRange(0, steps)
            self.bar.setValue(done)
            self.busy_bar.stop()
        elif not self.busy_bar.isStarted():
            self.busy_bar.start()
        self.cancel_button.setVisible(state.kind == "test_all")
        self.cancel_button.setEnabled(state.can_cancel)
        self.setAccessibleName(f"{self.title.text()}: {state.text}")


class CurrentRouterCard(SimpleCardWidget):
    """Score ring, network, status and the four key numbers of the connection."""

    addRouterRequested = Signal(object)  # a prefilled Router
    switchRequested = Signal(str)  # router id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prefill: Router | None = None
        self._recommended: str | None = None
        self.ring = ScoreRing(124, self)
        self.dot = ColorDot(parent=self)
        self.name = SubtitleLabel("Checking…", self)
        self.network = CaptionLabel("", self)
        self.network.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status = StatusLine(parent=self)
        self.gateway = MetricBlock("Gateway ping", self)
        self.internet = MetricBlock("Internet ping", self)
        self.loss = MetricBlock("Packet loss", self)
        self.signal = MetricBlock("Signal", self)
        self.recommend_badge = Badge("Recommended", self)
        self.recommend_text = BodyLabel("", self)
        self.switch_button = PushButton(FIF.WIFI, "Switch", self)
        self.switch_button.clicked.connect(self._switch)
        self.auto_text = CaptionLabel(self)  # automatic switching: progress or pause
        self.auto_text.hide()
        self.unknown_text = BodyLabel("", self)
        self.add_button = PushButton(FIF.ADD, "Add this router", self)
        self.add_button.clicked.connect(lambda: self.addRouterRequested.emit(self._prefill))

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        name_row.addWidget(self.name)
        name_row.addStretch(1)

        metrics = QHBoxLayout()
        metrics.setSpacing(28)
        for block in (self.gateway, self.internet, self.loss, self.signal):
            metrics.addWidget(block)
        metrics.addStretch(1)

        rec_row = QHBoxLayout()
        rec_row.setSpacing(8)
        rec_row.addWidget(self.recommend_badge)
        rec_row.addWidget(self.recommend_text, 1)
        rec_row.addWidget(self.switch_button)

        unknown_row = QHBoxLayout()
        unknown_row.addWidget(self.unknown_text, 1)
        unknown_row.addWidget(self.add_button)

        info = QVBoxLayout()
        info.setSpacing(6)
        info.addLayout(name_row)
        info.addWidget(self.network)
        info.addWidget(self.status)
        info.addSpacing(8)
        info.addLayout(metrics)

        top = QHBoxLayout()
        top.setSpacing(24)
        top.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(info, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addLayout(rec_row)
        layout.addWidget(self.auto_text)
        layout.addLayout(unknown_row)
        self._set_rows(recommend=False, unknown=False, metrics=False)

    def _set_rows(
        self, recommend: bool, unknown: bool, metrics: bool, can_switch: bool = False
    ) -> None:
        self.recommend_badge.setVisible(recommend)
        self.recommend_text.setVisible(recommend)
        self.switch_button.setVisible(recommend and can_switch)
        self.unknown_text.setVisible(unknown)
        self.add_button.setVisible(unknown)
        for block in (self.gateway, self.internet, self.loss, self.signal):
            block.setVisible(metrics)

    def show_waiting(self, failure: str | None) -> None:
        status = overall_status(None, failure)
        self.ring.set_score(None, status.level)
        self.name.setText("Checking…" if failure is None else "No data")
        self.network.setText("")
        self.dot.hide()
        self.status.set_status(status.level, status.title, status.detail)
        self._set_rows(False, False, False)

    def _switch(self) -> None:
        if self._recommended is not None:
            self.switchRequested.emit(self._recommended)

    def show_snapshot(
        self,
        snap: Snapshot,
        routers: tuple[Router, ...],
        failure: str | None,
        can_switch: bool = False,  # the Switch button may connect to the recommended router
    ) -> None:
        report = snap.report
        status = overall_status(report, failure)
        current = report.current
        router = report.match.router
        known = next((r for r in routers if router and r.id == router.id), router)

        # Title and network line.
        conn = report.connection
        if known is not None:
            self.name.setText(known.name)
            self.dot.set_color(known.color)
            self.dot.show()
        else:
            self.dot.hide()
            if report.gateway is None:
                self.name.setText("Not connected")
            else:
                self.name.setText(conn.ssid if conn and conn.ssid else "Unknown network")
        parts = []
        if report.gateway is not None and report.gateway.wired:
            parts.append("Ethernet")
        elif conn is not None:
            parts.append(f'Wi-Fi "{conn.ssid}"' if conn.ssid else "Hidden Wi-Fi")
        vpn = vpn_text(report)
        entry = current.observation.entry if current and current.observation else None
        if entry is None and conn is not None and conn.bssid and report.scan:
            entry = next((e for e in report.scan if e.bssid == conn.bssid), None)
        if entry is not None:
            parts.append(band_text(entry))
        if report.gateway is not None:
            parts.append(f"gateway {report.gateway.gateway_ip}")
            if report.gateway.gateway_mac:
                parts.append(str(report.gateway.gateway_mac))
        if vpn:
            parts.append(vpn)
        self.network.setText(" · ".join(parts))
        self.status.set_status(status.level, status.title, status.detail)

        # Score: the router's rolling score, or this single check on an unknown network.
        score = current.score if current else None
        if score is None and report.record is not None and report.record.score is not None:
            score = Score(round(report.record.score), estimated=False)
        caption = "this check" if current is None and score is not None else ""
        self.ring.set_score(score, status.level, caption)

        metrics = current_metrics(report)
        if metrics is not None:
            gw_note = (
                "doesn't answer ping"
                if metrics.gateway_silent
                else f"p95 {fmt_ms(metrics.gateway_p95_ms)}"
            )
            self.gateway.set_values(
                DASH if metrics.gateway_silent else fmt_ms(metrics.gateway_ms),
                gw_note,
                "Round trip to your router (the default gateway), median of the pings.",
            )
            vpn_note = "\nMeasured through the VPN, which blocks other traffic."
            self.internet.set_values(
                fmt_ms(metrics.internet_ms),
                f"p95 {fmt_ms(metrics.internet_p95_ms)}" + (" · VPN" if metrics.via_vpn else ""),
                targets_text(report) + (vpn_note if metrics.via_vpn else ""),
            )
            self.loss.set_values(
                fmt_pct(metrics.loss_pct),
                f"gateway {fmt_pct(metrics.gateway_loss_pct)}",
                "Share of internet pings that got no answer.",
            )
            if metrics.wired:
                self.signal.set_values(
                    "Cable", "no Wi-Fi signal", "Connected with an Ethernet cable."
                )
            else:
                quality = (
                    f"quality {metrics.signal_quality}%"
                    if metrics.signal_quality is not None
                    else ""
                )
                self.signal.set_values(
                    fmt_dbm(metrics.rssi), quality, "Wi-Fi signal strength (RSSI)."
                )

        rec = report.recommendation
        self._show_recommendation(rec, routers)
        unknown = report.gateway is not None and router is None
        if unknown:
            ssid = conn.ssid if conn else ""
            self.unknown_text.setText(
                "Add it to compare it with your other routers and get alerts when it's unstable."
            )
            macs = [m for m in (report.gateway.gateway_mac, conn.bssid if conn else None) if m]
            self._prefill = Router.create(ssid or "New router", ssid=ssid or None, macs=macs)
        self._set_rows(rec is not None, unknown, metrics is not None, can_switch)

    def _show_recommendation(self, rec: Recommendation | None, routers: tuple[Router, ...]) -> None:
        self._recommended = rec.router_id if rec else None
        if rec is None:
            return
        self.recommend_text.setText(recommendation_text(rec, routers))
        name = next((r.name for r in routers if r.id == rec.router_id), "it")
        self.switch_button.setText(f"Switch to {name}")
        self.switch_button.setToolTip(
            "Connect to it with the profile Windows saved. If that fails, "
            "Router Checker goes back to this network."
        )


class RouterCard(FocusCard):
    """One of the other routers: state, signal, busy level, score and a sparkline."""

    def __init__(self, router_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.router_id = router_id
        self.setFixedSize(312, 184)
        self.dot = ColorDot(parent=self)
        self.name = StrongBodyLabel(self)
        self.badge = Badge("Recommended", self)
        self.state_icon = IconWidget(FIF.QUESTION, self)
        self.state_icon.setFixedSize(16, 16)
        self.state = BodyLabel(self)
        self.state_note = CaptionLabel(self)
        self.hint = CaptionLabel(self)
        self.hint.setWordWrap(True)
        self.hint.hide()
        self.signal = CaptionLabel(self)
        self.busy = CaptionLabel(self)
        self.score = CaptionLabel(self)
        self.sparkline = Sparkline(self)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.name, 1)
        top.addWidget(self.badge)
        state_row = QHBoxLayout()
        state_row.setSpacing(6)
        state_row.addWidget(self.state_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        state_row.addWidget(self.state)
        state_row.addWidget(self.state_note, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(4)
        layout.addLayout(top)
        layout.addSpacing(2)
        layout.addLayout(state_row)
        layout.addWidget(self.hint)
        layout.addWidget(self.signal)
        layout.addWidget(self.busy)
        layout.addWidget(self.score)
        layout.addStretch(1)
        layout.addWidget(self.sparkline)

    def show_router(
        self,
        router: Router,
        status: RouterStatus | None,
        snap: Snapshot | None,
    ) -> None:
        self.name.setText(router.name)
        self.dot.set_color(router.color)
        location_allowed = snap.report.location_allowed if snap else True
        if status is None:
            self.state_icon.setIcon(FIF.QUESTION)
            self.state.setText("Waiting")
            self.state_note.setText("for the next check")
        else:
            self.state_icon.setIcon(STATE_ICONS[status.state])
            self.state.setText(status.state.value)
            self.state_note.setText(f"· {state_detail(status.state, location_allowed)}")
        missing_name = (
            status is not None and status.state is RouterState.NOT_FOUND and not router.ssid
        )
        self.hint.setText("Tip: add its Wi-Fi name (Edit) so scans can find it.")
        self.hint.setVisible(missing_name)
        obs = status.observation if status else None
        if obs is not None:
            self.signal.setText(f"Signal {fmt_dbm(obs.entry.rssi)} · {band_text(obs.entry)}")
            self.busy.setText(f"Busy: {busy_text(obs.busyness, obs.entry)}")
        else:
            self.signal.setText(f"Signal {DASH}")
            self.busy.setText(f"Busy: {DASH}")
        score = status.score if status else None
        suffix = " (estimated)" if score and score.estimated else ""
        self.score.setText(f"Score {score_text(score)}{suffix}")
        self.badge.setVisible(bool(status and status.recommended))
        points = snap.sparklines.get(router.id, []) if snap else []
        end = snap.report.timestamp if snap else None
        if end is not None:
            self.sparkline.set_points(points, QColor(router.color), end, SPARKLINE_SPAN)
        state_text = status.state.value if status else "Waiting"
        self.setAccessibleName(
            f"{router.name}: {state_text}. {self.signal.text()}. {self.busy.text()}. "
            f"{self.score.text()}.{' Recommended.' if self.badge.isVisible() else ''}"
        )
        self.setToolTip("Open router details")


class DashboardPage(Page):
    addRouterRequested = Signal(object)  # prefilled Router or None
    openRouterRequested = Signal(str)
    exitRequested = Signal()
    testAllRequested = Signal()
    switchRequested = Signal(str)  # router id

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__("dashboardPage", "Dashboard", parent)
        self.controller = controller
        self._preparing = False  # Test all is being planned

        self.spinner = IndeterminateProgressRing(self.view, start=False)
        self.spinner.setFixedSize(18, 18)
        self.spinner.setStrokeWidth(2)
        self.spinner.hide()
        self.when = CaptionLabel("", self.view)
        self.check_button = PrimaryPushButton(FIF.SYNC, "Check now", self.view)
        self.check_button.setToolTip("Test the current router now (F5)")
        self.check_button.clicked.connect(controller.check_now)
        self.test_all_button = PushButton(FIF.ROTATE, "Test all now", self.view)
        self.test_all_button.setToolTip(TEST_ALL_TIP)
        self.test_all_button.clicked.connect(lambda: self.testAllRequested.emit())
        self.test_all_button.setVisible(controller.can_switch)
        self.exit_button = PushButton(FIF.POWER_BUTTON, "Exit", self.view)
        self.exit_button.setToolTip("Stop monitoring and close Router Checker (Ctrl+Q)")
        self.exit_button.clicked.connect(lambda: self.exitRequested.emit())  # drop "checked"
        self.header.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        self.header.addWidget(self.when, 0, Qt.AlignmentFlag.AlignVCenter)
        for button in (self.check_button, self.test_all_button, self.exit_button):
            self.header.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.run_card = RunCard(self.view)
        self.run_card.cancel_button.clicked.connect(lambda: controller.cancel_test_all())

        self.location_bar = InfoBar(
            InfoBarIcon.WARNING,
            "Location access is off",
            LOCATION_OFF_TEXT,
            orient=Qt.Orientation.Vertical,
            isClosable=False,
            duration=-1,
            position=InfoBarPosition.NONE,
            parent=self.view,
        )
        open_button = PushButton("Open location settings", self.location_bar)
        open_button.clicked.connect(open_location_settings)
        self.location_bar.addWidget(open_button)
        self.location_bar.hide()
        self.wifi_bar = InfoBar(
            InfoBarIcon.ERROR,
            "Wi-Fi problem",
            "",
            orient=Qt.Orientation.Horizontal,
            isClosable=False,
            duration=-1,
            position=InfoBarPosition.NONE,
            parent=self.view,
        )
        self.wifi_bar.hide()

        self.current = CurrentRouterCard(self.view)
        self.current.addRouterRequested.connect(self.addRouterRequested)
        self.current.switchRequested.connect(self.switchRequested)

        self.others_title = SubtitleLabel("Other routers", self.view)
        self.cards_host = QWidget(self.view)
        self.flow = FlowLayout(self.cards_host, needAni=False)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.flow.setHorizontalSpacing(12)
        self.flow.setVerticalSpacing(12)
        self._cards: dict[str, RouterCard] = {}

        self.empty = QWidget(self.view)
        empty_layout = QHBoxLayout(self.empty)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.addWidget(
            BodyLabel("Add the other routers you can use to compare them here.", self.empty)
        )
        add_button = PushButton(FIF.ADD, "Add router", self.empty)
        add_button.clicked.connect(lambda: self.addRouterRequested.emit(None))
        empty_layout.addWidget(add_button)
        empty_layout.addStretch(1)

        self.body.addWidget(self.run_card)
        self.body.addWidget(self.location_bar)
        self.body.addWidget(self.wifi_bar)
        self.body.addWidget(self.current)
        self.body.addSpacing(8)
        self.body.addWidget(self.others_title)
        self.body.addWidget(self.cards_host)
        self.body.addWidget(self.empty)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        controller.checkStarted.connect(self.refresh)
        controller.checkFinished.connect(self.refresh)
        controller.checkFailed.connect(self.refresh)
        controller.settingsChanged.connect(self.refresh)
        controller.activityChanged.connect(self.refresh)
        self.refresh()

    def set_preparing(self, preparing: bool) -> None:
        """Test all is being planned (reading the saved profiles and a scan)."""
        self._preparing = preparing
        self.refresh()

    def refresh(self, *_args: object) -> None:
        c = self.controller
        snap = c.last_snapshot
        routers = c.settings.routers
        report = snap.report if snap else None
        busy = c.is_busy or self._preparing
        self.check_button.setEnabled(not busy)
        location_off = report is not None and not report.location_allowed
        wired = c.on_ethernet
        self.test_all_button.setEnabled(not busy and not location_off and not wired)
        self.test_all_button.setToolTip(
            ON_ETHERNET if wired else TEST_ALL_NEEDS_LOCATION if location_off else TEST_ALL_TIP
        )
        if busy == self.spinner.isHidden():  # only on changes: start() restarts the animation
            self.spinner.setVisible(busy)
            if busy:
                self.spinner.start()
            else:
                self.spinner.stop()
        self.run_card.show_state(c.run_state)
        if snap is None:
            self.current.show_waiting(c.last_failure)
        else:
            rec = report.recommendation
            can_switch = rec is not None and c.can_switch_to(rec.router_id)
            self.current.show_snapshot(snap, routers, c.last_failure, can_switch)

        self.location_bar.setVisible(location_off)
        # On a cable, Wi-Fi trouble only means the other routers can't be scanned.
        wifi_error = report.wifi_error if report and not wired else None
        self.wifi_bar.setVisible(bool(wifi_error))
        if wifi_error:
            self.wifi_bar.content = wifi_error[:1].upper() + wifi_error[1:] + "."
            self.wifi_bar._adjustText()
        self._sync_cards(snap)
        self._tick()

    def _sync_cards(self, snap: Snapshot | None) -> None:
        current_id = snap.report.match.router.id if snap and snap.report.match.router else None
        statuses = {s.router.id: s for s in snap.report.statuses} if snap else {}
        others = [r for r in self.controller.settings.routers if r.id != current_id]
        wanted = {r.id for r in others}
        for router_id in list(self._cards):
            if router_id not in wanted:
                card = self._cards.pop(router_id)
                self.flow.removeWidget(card)
                card.deleteLater()
        self.flow.removeAllWidgets()
        for router in others:
            card = self._cards.get(router.id)
            if card is None:
                card = RouterCard(router.id, self.cards_host)
                card.activated.connect(lambda rid=router.id: self.openRouterRequested.emit(rid))
                self._cards[router.id] = card
            card.show_router(router, statuses.get(router.id), snap)
            self.flow.addWidget(card)
            card.show()
        self.others_title.setVisible(bool(others))
        self.cards_host.setVisible(bool(others))
        self.empty.setVisible(not others)
        self.cards_host.updateGeometry()

    def _tick(self) -> None:
        c = self.controller
        auto = c.auto_switch_text
        self.current.auto_text.setText(auto or "")
        self.current.auto_text.setVisible(auto is not None)
        activity = c.activity
        if activity is not None:
            self.when.setText(activity)
            return
        if self._preparing:
            self.when.setText("Preparing Test all…")
            return
        if c.is_checking:
            self.when.setText("Checking now…")
            return
        now = c.now()
        parts = []
        if c.last_snapshot is not None:
            parts.append(f"Last check {fmt_clock(c.last_snapshot.report.timestamp, now)}")
        if c.next_check_at is not None:
            parts.append(f"next in {fmt_countdown((c.next_check_at - now).total_seconds())}")
        self.when.setText(" · ".join(parts))
