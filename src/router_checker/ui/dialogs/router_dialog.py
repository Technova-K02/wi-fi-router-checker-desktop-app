"""Add or edit a router: pick a nearby network or type a MAC, then name and color."""

from __future__ import annotations

import ipaddress
from dataclasses import replace

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ColorPickerButton,
    IndeterminateProgressRing,
    LineEdit,
    ListWidget,
    MessageBoxBase,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    ToolTipFilter,
    TransparentToolButton,
    themeColor,
)
from qfluentwidgets import FluentIcon as FIF

from router_checker.core.errors import LocationPermissionError
from router_checker.core.mac import MacAddress, try_parse_mac
from router_checker.core.models import DEFAULT_ROUTER_COLORS, Router
from router_checker.core.presentation import NearbyNetwork, group_networks
from router_checker.ui.controller import AppController, CurrentRouter, ScanResult
from router_checker.ui.shell import open_location_settings
from router_checker.ui.style import text_color

MAC_HELP = "Use B0-0A-D5-9A-7B-B4, b0:0a:d5:9a:7b:b4 or b00ad59a7bb4."
ERROR_COLORS = (QColor("#C42B1C"), QColor("#FF99A4"))


class ColorSwatch(QAbstractButton):
    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.color = color
        self.setCheckable(True)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(color)
        self.setAccessibleName(f"Color {color}")

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(0, 0, 0, 40), 1))
        painter.setBrush(QColor(self.color))
        painter.drawEllipse(QRectF(5, 5, 20, 20))
        if self.isChecked() or self.hasFocus():
            color = themeColor() if self.hasFocus() else text_color(200)
            painter.setPen(QPen(color, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(1.5, 1.5, 27, 27))


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


class RouterDialog(MessageBoxBase):
    def __init__(
        self,
        controller: AppController,
        router: Router | None = None,
        prefill: Router | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self._editing = router
        base = router or prefill
        self._macs: list[MacAddress] = list(base.macs) if base else []
        self._color = base.color if base else self._next_color()
        self._auto_name = ""
        self._networks: list[NearbyNetwork] = []
        self._closed = False
        self._result: Router | None = None

        self.widget.setMinimumWidth(660)
        title = SubtitleLabel("Edit router" if router else "Add router", self)

        # Nearby networks
        self.spinner = IndeterminateProgressRing(self, start=False)
        self.spinner.setFixedSize(16, 16)
        self.spinner.setStrokeWidth(2)
        rescan = self.rescan = TransparentToolButton(FIF.SYNC, self)
        rescan.setToolTip("Scan again")
        rescan.setAccessibleName("Scan again")
        rescan.installEventFilter(ToolTipFilter(rescan))
        rescan.clicked.connect(self._scan)
        nearby_header = QHBoxLayout()
        nearby_header.addWidget(StrongBodyLabel("Pick a nearby network", self))
        nearby_header.addStretch(1)
        nearby_header.addWidget(self.spinner)
        nearby_header.addWidget(rescan)
        self.network_list = ListWidget(self)
        self.network_list.setFixedHeight(150)
        self.network_list.setAccessibleName("Nearby networks")
        self.network_list.itemClicked.connect(self._pick)
        self.network_list.itemActivated.connect(self._pick)
        self.scan_note = CaptionLabel("", self)
        self.scan_note.setWordWrap(True)
        self.location_button = PushButton("Open location settings", self)
        self.location_button.clicked.connect(open_location_settings)
        self.location_button.hide()
        note_row = QHBoxLayout()
        note_row.addWidget(self.scan_note, 1)
        note_row.addWidget(self.location_button)

        # Form
        self.name = LineEdit(self)
        self.name.setPlaceholderText("For example: Living room")
        self.name.setAccessibleName("Router name")
        self.name.setText(base.name if base else "")
        self.ssid = LineEdit(self)
        self.ssid.setPlaceholderText("Optional")
        self.ssid.setAccessibleName("Wi-Fi name (SSID)")
        self.ssid.setText((base.ssid or "") if base else "")
        self.address = LineEdit(self)
        self.address.setPlaceholderText("Behind your middle router, e.g. 192.168.1.1")
        self.address.setAccessibleName("Address behind the middle router")
        self.address.setText((base.address or "") if base else "")
        self.mac_rows = QWidget(self)
        self.mac_layout = QVBoxLayout(self.mac_rows)
        self.mac_layout.setContentsMargins(0, 0, 0, 0)
        self.mac_layout.setSpacing(0)
        self.mac_edit = LineEdit(self)
        self.mac_edit.setPlaceholderText("MAC address, e.g. B0-0A-D5-9A-7B-B4")
        self.mac_edit.setAccessibleName("New MAC address")
        self.mac_edit.textChanged.connect(self._check_mac)
        self.mac_edit.returnPressed.connect(self._add_mac)
        add_mac = PushButton(FIF.ADD, "Add", self)
        add_mac.clicked.connect(self._add_mac)
        self.mac_feedback = CaptionLabel("", self)
        self.mac_feedback.setWordWrap(True)
        mac_input = QHBoxLayout()
        mac_input.addWidget(self.mac_edit, 1)
        mac_input.addWidget(add_mac)
        self.use_current = PushButton(FIF.CONNECT, "Use the router I'm connected to", self)
        self.use_current.setToolTip(
            "Adds the MAC of the router you're connected to right now, over Wi-Fi or Ethernet"
        )
        self.use_current.installEventFilter(ToolTipFilter(self.use_current))
        self.use_current.clicked.connect(self._use_current)
        current_row = QHBoxLayout()
        current_row.addWidget(self.use_current)
        current_row.addStretch(1)

        self.swatches = QButtonGroup(self)
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(2)
        for color in DEFAULT_ROUTER_COLORS:
            swatch = ColorSwatch(color, self)
            swatch.clicked.connect(lambda _c=False, col=color: self._set_color(col))
            self.swatches.addButton(swatch)
            swatch_row.addWidget(swatch)
        self.picker = ColorPickerButton(QColor(self._color), "router color", self)
        self.picker.setAccessibleName("Custom color")
        self.picker.colorChanged.connect(lambda c: self._set_color(c.name().upper()))
        swatch_row.addSpacing(10)
        swatch_row.addWidget(self.picker)
        swatch_row.addStretch(1)

        form = QGridLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setColumnStretch(1, 1)
        labels = ["Name", "Wi-Fi name (SSID)", "MAC addresses", "", "", "", "Color", "Address"]
        for row, text in enumerate(labels):
            if text:
                align = Qt.AlignmentFlag.AlignTop if row == 2 else Qt.AlignmentFlag.AlignVCenter
                form.addWidget(CaptionLabel(text, self), row, 0, align)
        form.addWidget(self.name, 0, 1)
        form.addWidget(self.ssid, 1, 1)
        form.addWidget(self.mac_rows, 2, 1)
        form.addLayout(mac_input, 3, 1)
        form.addWidget(self.mac_feedback, 4, 1)
        form.addLayout(current_row, 5, 1)
        form.addLayout(swatch_row, 6, 1)
        form.addWidget(self.address, 7, 1)
        self.address_label = form.itemAtPosition(7, 0).widget()
        # Only matters behind a middle router (it's learned there, too).
        if controller.settings.middle is None and not (base and base.address):
            self.address.hide()
            self.address_label.hide()

        self.error = BodyLabel("", self)
        self.error.setTextColor(*ERROR_COLORS)
        self.error.setWordWrap(True)
        self.error.hide()

        self.viewLayout.addWidget(title)
        self.viewLayout.addLayout(nearby_header)
        self.viewLayout.addWidget(self.network_list)
        self.viewLayout.addLayout(note_row)
        self.viewLayout.addSpacing(6)
        self.viewLayout.addLayout(form)
        self.viewLayout.addWidget(self.error)

        self.yesButton.setText("Save" if router else "Add router")
        self.cancelButton.setText("Cancel")
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

        self._rebuild_macs()
        self._set_color(self._color)
        self._check_mac("")
        if controller.has_wifi:
            self._scan()
        else:
            self.network_list.hide()
            self.spinner.hide()
            self.rescan.hide()
            self.scan_note.setText(
                "This PC has no Wi-Fi adapter, so there are no nearby networks to pick from. "
                "Use the router you're connected to, or type its MAC."
            )
        self.name.setFocus()

    # --- result -----------------------------------------------------------------

    def router(self) -> Router:
        assert self._result is not None, "call after the dialog was accepted"
        return self._result

    def validate(self) -> bool:
        pending = self.mac_edit.text().strip()
        if pending and not self._add_mac():
            self._show_error(f"{pending} isn't a MAC address. {MAC_HELP}")
            return False
        name = self.name.text().strip()
        ssid = self.ssid.text().strip() or None
        if not name:
            self._show_error("Give the router a name.")
            self.name.setFocus()
            return False
        address = self.address.text().strip() or None
        if address is not None and not _is_ipv4(address):
            self._show_error(f"{address} isn't an address like 192.168.1.1.")
            self.address.setFocus()
            return False
        if not self._macs and not ssid and not address:
            self._show_error("Add a MAC address or a Wi-Fi name so the router can be recognized.")
            return False
        others = [
            r
            for r in self.controller.settings.routers
            if r.id != getattr(self._editing, "id", None)
        ]
        for mac in self._macs:
            owner = next((r for r in others if mac in r.macs), None)
            if owner is not None:
                self._show_error(f"{mac} already belongs to {owner.name}.")
                return False
        if ssid and any(r.ssid == ssid for r in others):
            self._show_error(f'Another router already uses the Wi-Fi name "{ssid}".')
            return False
        owner = next((r for r in others if address and r.address == address), None)
        if owner is not None:
            self._show_error(f"{owner.name} already has the address {address}.")
            return False
        try:
            if self._editing is None:
                created = Router.create(name, color=self._color, ssid=ssid, macs=self._macs)
                self._result = replace(created, address=address)
            else:
                bssid = self._editing.middle_bssid
                self._result = replace(
                    self._editing, name=name, color=self._color, ssid=ssid,
                    macs=tuple(self._macs), address=address,
                    middle_bssid=bssid if bssid in self._macs else None,
                )  # fmt: skip
        except ValueError as exc:
            self._show_error(str(exc))
            return False
        return True

    def done(self, code: int) -> None:
        self._closed = True
        super().done(code)

    # --- nearby networks ----------------------------------------------------------

    def _scan(self) -> None:
        self.spinner.show()
        self.spinner.start()
        self.scan_note.setText("Scanning for networks…")
        self.location_button.hide()
        self.controller.scan_networks(self._on_scan, self._on_scan_error)

    def _stop_spinner(self) -> None:
        self.spinner.stop()
        self.spinner.hide()

    def _on_scan(self, result: ScanResult) -> None:
        if self._closed:
            return
        self._stop_spinner()
        self._networks = group_networks(
            result.entries, result.profiles, self.controller.settings.routers
        )
        self.network_list.clear()
        for net in self._networks:
            bands = ", ".join(b.value for b in net.bands)
            parts = [net.ssid, bands, f"{net.best_rssi} dBm"]
            if net.has_profile:
                parts.append("password saved in Windows")
            if net.router_name:
                parts.append(f"added as {net.router_name}")
            item = QListWidgetItem("  ·  ".join(parts))
            item.setToolTip("BSSIDs: " + ", ".join(str(b) for b in net.bssids))
            self.network_list.addItem(item)
        count = len(self._networks)
        self.scan_note.setText(
            f"{count} network{'s' if count != 1 else ''} nearby. Selecting one fills in its "
            "Wi-Fi name and MAC addresses."
            if count
            else "No networks found. You can still type the MAC address."
        )

    def _on_scan_error(self, exc: BaseException) -> None:
        if self._closed:
            return
        self._stop_spinner()
        if isinstance(exc, LocationPermissionError):
            self.scan_note.setText(
                "Location access is off, so Windows won't list nearby networks. "
                "Type the MAC address below instead."
            )
            self.location_button.show()
        else:
            self.scan_note.setText(f"Couldn't scan: {exc}")

    def _pick(self, item: QListWidgetItem) -> None:
        net = self._networks[self.network_list.row(item)]
        if not self.name.text().strip() or self.name.text() == self._auto_name:
            self.name.setText(net.ssid)
            self._auto_name = net.ssid
        self.ssid.setText(net.ssid)
        for mac in net.bssids:
            if mac not in self._macs:
                self._macs.append(mac)
        self._rebuild_macs()
        self._show_error("")

    def _use_current(self) -> None:
        self.use_current.setEnabled(False)
        self.controller.current_gateway(self._on_current, self._on_current_error)

    def _on_current(self, current: CurrentRouter) -> None:
        self.use_current.setEnabled(True)
        if current.behind_middle:
            self._use_address(current.upstream_ip)
            return
        gateway = current.gateway
        if gateway is None:
            self._set_feedback("You're not connected to a router right now.", error=True)
            return
        mac = gateway.gateway_mac
        if mac is None:
            self._set_feedback(
                f"The router at {gateway.gateway_ip} didn't tell its MAC. Try again.", error=True
            )
            return
        editing = getattr(self._editing, "id", None)
        owner = next(
            (r for r in self.controller.settings.routers if mac in r.macs and r.id != editing),
            None,
        )
        if owner is not None:
            self._set_feedback(
                f"You're connected to {owner.name}, which you've already added.", error=True
            )
            return
        if mac not in self._macs:
            self._macs.append(mac)
            self._rebuild_macs()
        how = gateway.kind.label
        self._set_feedback(f"✓ Added {mac}, the router you're connected to over {how}.")
        self._show_error("")

    def _use_address(self, address: str | None) -> None:
        """Behind the middle router, "the router I'm connected to" is the one it's on."""
        if address is None:
            self._set_feedback(
                "Couldn't tell which router your middle router is on (no second hop).",
                error=True,
            )
            return
        editing = getattr(self._editing, "id", None)
        routers = self.controller.settings.routers
        owner = next((r for r in routers if r.address == address and r.id != editing), None)
        if owner is not None:
            self._set_feedback(
                f"Your middle router is on {owner.name}, which you've already added.", error=True
            )
            return
        self.address.setText(address)
        self.address.show()
        self.address_label.show()
        self._set_feedback(f"✓ Your middle router is on the router at {address}: added it below.")
        self._show_error("")

    def _on_current_error(self, exc: BaseException) -> None:
        self.use_current.setEnabled(True)
        self._set_feedback(f"Couldn't read the connection: {exc}", error=True)

    def _set_feedback(self, text: str, error: bool = False) -> None:
        self.mac_feedback.setText(text)
        if error:
            self.mac_feedback.setTextColor(*ERROR_COLORS)
        else:
            self.mac_feedback.setTextColor(QColor("#0F7B0F"), QColor("#6CCB5F"))

    # --- MAC list -----------------------------------------------------------------

    def _check_mac(self, text: str) -> None:
        text = text.strip()
        if not text:
            self.mac_feedback.setText(
                "Add each Wi-Fi (BSSID) or LAN MAC the router uses. " + MAC_HELP
            )
            self.mac_feedback.setTextColor(QColor(0, 0, 0, 158), QColor(255, 255, 255, 170))
            self.mac_edit.setError(False)
            return
        mac = try_parse_mac(text)
        if mac is None:
            self.mac_feedback.setText(f"Not a complete MAC address yet. {MAC_HELP}")
            self.mac_feedback.setTextColor(*ERROR_COLORS)
            self.mac_edit.setError(True)
        else:
            self.mac_feedback.setText(f"✓ {mac}. Press Enter or Add.")
            self.mac_feedback.setTextColor(QColor("#0F7B0F"), QColor("#6CCB5F"))
            self.mac_edit.setError(False)

    def _add_mac(self) -> bool:
        mac = try_parse_mac(self.mac_edit.text())
        if mac is None:
            return False
        if mac not in self._macs:
            self._macs.append(mac)
        self.mac_edit.clear()
        self._rebuild_macs()
        return True

    def _remove_mac(self, mac: MacAddress) -> None:
        self._macs.remove(mac)
        self._rebuild_macs()

    def _rebuild_macs(self) -> None:
        while self.mac_layout.count():
            item = self.mac_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._macs:
            self.mac_layout.addWidget(CaptionLabel("None yet", self.mac_rows))
        for mac in self._macs:
            row = QWidget(self.mac_rows)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(BodyLabel(str(mac), row), 1)
            remove = TransparentToolButton(FIF.DELETE, row)
            remove.setToolTip(f"Remove {mac}")
            remove.setAccessibleName(f"Remove {mac}")
            remove.installEventFilter(ToolTipFilter(remove))
            remove.clicked.connect(lambda _c=False, m=mac: self._remove_mac(m))
            layout.addWidget(remove)
            self.mac_layout.addWidget(row)

    # --- color --------------------------------------------------------------------

    def _next_color(self) -> str:
        used = {r.color.upper() for r in self.controller.settings.routers}
        free = [c for c in DEFAULT_ROUTER_COLORS if c.upper() not in used]
        index = len(self.controller.settings.routers) % len(DEFAULT_ROUTER_COLORS)
        return free[0] if free else DEFAULT_ROUTER_COLORS[index]

    def _set_color(self, color: str) -> None:
        self._color = color.upper()
        if self.picker.color.name().upper() != self._color:
            self.picker.setColor(QColor(self._color))
        self.swatches.setExclusive(False)
        for button in self.swatches.buttons():
            button.setChecked(button.color.upper() == self._color)
        self.swatches.setExclusive(True)

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))
