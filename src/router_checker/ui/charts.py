"""Line charts over time (latency, packet loss, score), drawn with pyqtgraph.

The charts don't pan or zoom, so the mouse wheel keeps scrolling the page.
Hovering shows the values at that moment in a text line under the chart; when
the mouse is elsewhere that line shows the summary (median and worst value),
so the numbers are always available as text, not only as a drawing.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, isDarkTheme, qconfig

from router_checker.core.presentation import DASH, fmt_clock
from router_checker.core.series import Series
from router_checker.ui.widgets import ColorDot

pg.setConfigOptions(antialias=True)

SYMBOLS_BELOW = 60  # also draw dots when a line has fewer points, so single points show
DARK_THEME_MIN_LIGHTNESS = 0.6  # router colors darker than this are lifted on dark backgrounds


@dataclass(frozen=True, slots=True)
class Line:
    series: Series
    color: str | tuple[str, str]  # "#RRGGBB", or (light theme, dark theme)
    dashed: bool = False
    name: str = ""  # legend and read-out name; "" uses the series name
    group: str = ""  # lines of one group share a read-out entry (measured/estimated)
    end_label: bool = False  # write the name at the line's last point
    in_legend: bool = True

    @property
    def label(self) -> str:
        return self.name or self.series.name


@dataclass(frozen=True, slots=True)
class Reference:
    """A dashed horizontal line, e.g. the "unstable" threshold."""

    value: float
    label: str


class _Plot(pg.PlotWidget):
    def wheelEvent(self, event) -> None:  # let the page scroll instead of zooming
        event.ignore()


def resolve(color: str | tuple[str, str]) -> QColor:
    """A line color for the current theme; dark router colors get lighter on dark."""
    if isinstance(color, tuple):
        return QColor(color[1] if isDarkTheme() else color[0])
    result = QColor(color)
    if isDarkTheme() and result.lightnessF() < DARK_THEME_MIN_LIGHTNESS:
        result = QColor.fromHslF(
            result.hslHueF(), result.hslSaturationF(), DARK_THEME_MIN_LIGHTNESS
        )
    return result


def _axis_color() -> QColor:
    return QColor(255, 255, 255, 170) if isDarkTheme() else QColor(0, 0, 0, 150)


class TimeChart(QWidget):
    def __init__(
        self,
        fmt: Callable[[float | None], str],
        parent: QWidget | None = None,
        *,
        fixed_top: float | None = None,  # 100 for scores; None fits the data
        min_top: float = 10.0,
        height: int = 170,
    ) -> None:
        super().__init__(parent)
        self._fmt = fmt
        self._fixed_top = fixed_top
        self._min_top = min_top
        self._lines: list[Line] = []
        self._references: list[Reference] = []
        self._summary = ""
        self._range: tuple[float, float] | None = None  # epoch seconds
        self._cursor: pg.InfiniteLine | None = None

        self.plot = _Plot(
            background=None, axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self.plot.setFixedHeight(height)
        # Show the card behind the chart; the view would paint the system palette's
        # base color (dark whenever Windows is dark, even in the light app theme).
        self.plot.setStyleSheet("background: transparent; border: none;")
        self.plot.setFrameShape(QFrame.Shape.NoFrame)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self.plot.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.legend_row = QHBoxLayout()
        self.legend_row.setSpacing(6)
        self.readout = CaptionLabel("", self)
        self.readout.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(self.legend_row)
        layout.addWidget(self.plot)
        layout.addWidget(self.readout)
        qconfig.themeChanged.connect(self._retheme)

    # --- data ---------------------------------------------------------------------

    def set_lines(
        self,
        lines: Sequence[Line],
        start: datetime,
        end: datetime,
        summary: str,
        references: Sequence[Reference] = (),
    ) -> None:
        self._lines, self._references = list(lines), list(references)
        self._range = (start.timestamp(), end.timestamp())
        self._summary = summary
        self._draw()
        self._fill_legend()
        self.readout.setText(summary)
        self.setAccessibleDescription(summary)

    def readout_at(self, x: float) -> str:
        """The value of each line (or group of lines) closest to the time ``x``."""
        start, end = self._range or (x, x + 50)
        tolerance = (end - start) / 50
        entries: dict[str, str] = {}
        for line in self._lines:
            key = line.group or line.label
            value = _nearest(line.series, x, tolerance)
            if value is not None:
                if key not in entries or entries[key].endswith(DASH):
                    entries[key] = f"{line.label} {self._fmt(value)}"
            elif key not in entries:
                entries[key] = f"{key} {DASH}"
        when = fmt_clock(datetime.fromtimestamp(x, UTC), datetime.now(UTC))
        return " · ".join([when, *entries.values()])

    # --- drawing ------------------------------------------------------------------

    def _retheme(self) -> None:
        self._draw()
        self._fill_legend()

    def _draw(self) -> None:
        plot = self.plot
        plot.clear()
        self._cursor = None
        color = _axis_color()
        for name in ("left", "bottom"):
            axis = plot.getAxis(name)
            axis.setPen(color)
            axis.setTextPen(color)
        if self._range is None:
            return
        top = self._top()
        for line in self._lines:
            xs = np.asarray(line.series.xs, dtype=float)
            ys = np.asarray(line.series.ys, dtype=float)
            finite = np.flatnonzero(np.isfinite(ys))
            line_color = resolve(line.color)
            style = Qt.PenStyle.DashLine if line.dashed else Qt.PenStyle.SolidLine
            item = pg.PlotDataItem(
                xs, ys, pen=pg.mkPen(line_color, width=2, style=style), connect="finite"
            )
            if 0 < len(finite) < SYMBOLS_BELOW:
                item.setSymbol("o")
                item.setSymbolSize(5)
                item.setSymbolBrush(line_color)
                item.setSymbolPen(None)
            plot.addItem(item)
            if line.end_label and len(finite):
                last = int(finite[-1])
                below = ys[last] > top * 0.8  # keep labels near the top inside the chart
                text = pg.TextItem(line.label, color=line_color, anchor=(1, 0 if below else 1))
                text.setPos(float(xs[last]), float(ys[last]))
                plot.addItem(text)
        for ref in self._references:
            plot.addItem(
                pg.InfiniteLine(
                    pos=ref.value,
                    angle=0,
                    pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
                    label=ref.label,
                    labelOpts={"position": 0.02, "color": color, "anchors": [(0, 1), (0, 1)]},
                ),
                ignoreBounds=True,
            )
        self._cursor = pg.InfiniteLine(angle=90, pen=pg.mkPen(color, width=1))
        self._cursor.hide()
        plot.addItem(self._cursor, ignoreBounds=True)

        plot.setXRange(*self._range, padding=0.01)
        plot.setYRange(0, top, padding=0.02)

    def _top(self) -> float:
        """The top of the value axis: fixed, or room above the data and references."""
        if self._fixed_top is not None:
            return self._fixed_top
        values = [v for line in self._lines for v in line.series.values]
        refs = [r.value * 1.3 for r in self._references]
        return max([self._min_top, *(v * 1.15 for v in values), *refs])

    def _fill_legend(self) -> None:
        while self.legend_row.count():
            item = self.legend_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for line in self._lines:
            if not line.in_legend or (line.dashed and not line.series.values):
                continue  # no "estimated" entry when nothing was estimated
            self.legend_row.addWidget(ColorDot(resolve(line.color).name(), 10, self))
            dashed = " (dashed)" if line.dashed else ""
            self.legend_row.addWidget(CaptionLabel(f"{line.label}{dashed}", self))
            self.legend_row.addSpacing(10)
        self.legend_row.addStretch(1)

    # --- hover --------------------------------------------------------------------

    def _on_mouse_moved(self, pos: QPointF) -> None:
        view = self.plot.getPlotItem().getViewBox()
        if self._cursor is None or not view.sceneBoundingRect().contains(pos):
            self._reset_readout()
            return
        x = view.mapSceneToView(pos).x()
        self._cursor.setPos(x)
        self._cursor.show()
        self.readout.setText(self.readout_at(x))

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._reset_readout()

    def _reset_readout(self) -> None:
        if self._cursor is not None:
            self._cursor.hide()
        self.readout.setText(self._summary)


def _nearest(series: Series, x: float, tolerance: float) -> float | None:
    """The value of the point closest to ``x``, if one is within ``tolerance``."""
    xs = series.xs
    i = bisect.bisect_left(xs, x)
    best: tuple[float, float] | None = None
    for j in (i - 1, i):
        if 0 <= j < len(xs) and not math.isnan(series.ys[j]):
            distance = abs(xs[j] - x)
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, series.ys[j])
    return None if best is None else best[1]
