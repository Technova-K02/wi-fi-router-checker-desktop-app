"""Popular times: a 7 x 24 grid (weekday x hour, local time) of how busy a router
usually is. Darker cells are busier; cells without data are only outlined.

The grid takes keyboard focus: arrow keys move between cells, Home and End
jump to the first and last hour. The selected (or hovered) cell is described
in words through ``cellChanged`` and the accessible description, so the grid
is not color-only.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget
from qfluentwidgets import isDarkTheme, qconfig, themeColor

from router_checker.core.popularity import PopularTimes
from router_checker.core.presentation import popular_cell_text

DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CELL = 22
GAP = 3
LEFT = 40  # day labels
TOP = 18  # hour labels


def busy_color(value: float) -> QColor:
    """The accent color, more opaque for busier hours."""
    color = QColor(themeColor())
    color.setAlpha(round(35 + max(0.0, min(1.0, value)) * 220))
    return color


class HeatSwatch(QWidget):
    """A legend sample: one cell at busyness ``value``, or outlined for "no data"."""

    def __init__(self, value: float | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = value
        self.setFixedSize(14, 14)
        qconfig.themeChanged.connect(self.update)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        if self._value is None:
            empty = QColor(255, 255, 255, 45) if isDarkTheme() else QColor(0, 0, 0, 40)
            painter.setPen(QPen(empty, 1))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(busy_color(self._value))
        painter.drawRoundedRect(rect, 3, 3)


class PopularTimesHeatmap(QWidget):
    cellChanged = Signal(str)  # the selected or hovered cell in words

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._times: PopularTimes | None = None
        self._cell = (0, 0)  # (day, hour) with keyboard focus
        self._hover: tuple[int, int] | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("Popular times by weekday and hour")
        self.setToolTip("Use the arrow keys to read each hour")
        qconfig.themeChanged.connect(self.update)

    @property
    def cell(self) -> tuple[int, int]:
        return self._cell

    def set_times(self, times: PopularTimes) -> None:
        first = self._times is None
        self._times = times
        if first:
            window = times.busiest()
            if window is not None:
                self._cell = (window.day, window.start_hour)
        self.update()
        self._announce(self._cell)

    def sizeHint(self) -> QSize:
        return QSize(LEFT + 24 * (CELL + GAP), TOP + 7 * (CELL + GAP))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # --- geometry -----------------------------------------------------------------

    def _rect(self, day: int, hour: int) -> QRectF:
        return QRectF(LEFT + hour * (CELL + GAP), TOP + day * (CELL + GAP), CELL, CELL)

    def _cell_at(self, x: float, y: float) -> tuple[int, int] | None:
        hour, day = int((x - LEFT) // (CELL + GAP)), int((y - TOP) // (CELL + GAP))
        if 0 <= day < 7 and 0 <= hour < 24 and self._rect(day, hour).contains(x, y):
            return day, hour
        return None

    # --- painting -----------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = QColor(255, 255, 255, 190) if isDarkTheme() else QColor(0, 0, 0, 160)
        empty = QColor(255, 255, 255, 45) if isDarkTheme() else QColor(0, 0, 0, 40)
        font = QFont(self.font())
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(text)
        for day, label in enumerate(DAY_LABELS):
            row = self._rect(day, 0)
            painter.drawText(
                QRectF(0, row.y(), LEFT - 6, CELL),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
        for hour in range(0, 24, 3):
            col = self._rect(0, hour)
            painter.drawText(
                QRectF(col.x(), 0, CELL * 2, TOP - 2),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                f"{hour:02d}",
            )
        for day in range(7):
            for hour in range(24):
                rect = self._rect(day, hour)
                value = self._times.values[day][hour] if self._times else None
                if value is None:
                    painter.setPen(QPen(empty, 1))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
                else:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(busy_color(value))
                    painter.drawRoundedRect(rect, 4, 4)
        marked = self._hover or (self._cell if self.hasFocus() else None)
        if marked is not None:
            painter.setPen(QPen(text if self._hover else themeColor(), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self._rect(*marked).adjusted(-1, -1, 1, 1), 5, 5)

    # --- keyboard and mouse -------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        day, hour = self._cell
        moves = {
            Qt.Key.Key_Left: (day, max(0, hour - 1)),
            Qt.Key.Key_Right: (day, min(23, hour + 1)),
            Qt.Key.Key_Up: (max(0, day - 1), hour),
            Qt.Key.Key_Down: (min(6, day + 1), hour),
            Qt.Key.Key_Home: (day, 0),
            Qt.Key.Key_End: (day, 23),
        }
        target = moves.get(Qt.Key(event.key()))
        if target is None:
            super().keyPressEvent(event)
            return
        self._cell = target
        self.update()
        self._announce(target)

    def mouseMoveEvent(self, event) -> None:
        cell = self._cell_at(event.position().x(), event.position().y())
        if cell != self._hover:
            self._hover = cell
            self.update()
            self._announce(cell or self._cell)

    def mousePressEvent(self, event) -> None:
        cell = self._cell_at(event.position().x(), event.position().y())
        if cell is not None:
            self._cell = cell
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.update()
            self._announce(cell)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._hover = None
        self.update()
        self._announce(self._cell)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()
        self._announce(self._cell)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def _announce(self, cell: tuple[int, int]) -> None:
        if self._times is None:
            return
        text = popular_cell_text(self._times, *cell)
        self.setAccessibleDescription(text)
        self.cellChanged.emit(text)
