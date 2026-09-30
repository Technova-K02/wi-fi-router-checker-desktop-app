"""Small custom widgets: status icon, score ring, sparkline, color dot, badge,
metric block and a keyboard-friendly card."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    isDarkTheme,
    qconfig,
    themeColor,
)

from router_checker.core.models import Score, ScorePoint
from router_checker.core.presentation import DASH, StatusLevel
from router_checker.ui.style import level_text_color, level_text_colors, paint_status_icon


def _repaint_on_theme_change(widget: QWidget) -> None:
    qconfig.themeChanged.connect(widget.update)


class StatusIcon(QWidget):
    def __init__(self, size: int = 16, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._level = StatusLevel.UNKNOWN
        self.setFixedSize(size, size)

    def set_level(self, level: StatusLevel) -> None:
        self._level = level
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        paint_status_icon(
            painter, QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), self._level
        )


class StatusLine(QWidget):
    """Status icon, a colored title and a plain detail text."""

    def __init__(self, icon_size: int = 18, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon = StatusIcon(icon_size, self)
        self.title = StrongBodyLabel(self)
        self.detail = BodyLabel(self)
        self.detail.setWordWrap(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.title, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.detail, 1, Qt.AlignmentFlag.AlignTop)

    def set_status(self, level: StatusLevel, title: str, detail: str = "") -> None:
        self.icon.set_level(level)
        self.title.setText(title)
        self.title.setTextColor(*level_text_colors(level))
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.setAccessibleName(f"{level.glyph} {title}. {detail}".strip())


class ScoreRing(QWidget):
    """Stability score as a ring, with the value and label in the middle."""

    def __init__(self, size: int = 128, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._score: Score | None = None
        self._level = StatusLevel.UNKNOWN
        self._caption = ""
        _repaint_on_theme_change(self)

    def set_score(self, score: Score | None, level: StatusLevel, caption: str = "") -> None:
        self._score, self._level, self._caption = score, level, caption
        if score is None:
            self.setAccessibleName("No stability score yet")
        else:
            kind = "estimated " if score.estimated else ""
            self.setAccessibleName(f"{kind}stability score {score.value} of 100, {score.label}")
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        stroke = max(6.0, self.width() * 0.075)
        rect = QRectF(
            stroke / 2 + 1, stroke / 2 + 1, self.width() - stroke - 2, self.height() - stroke - 2
        )

        track = QColor(255, 255, 255, 30) if isDarkTheme() else QColor(0, 0, 0, 22)
        pen = QPen(track, stroke)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawEllipse(rect)

        if self._score is not None and self._score.value > 0:
            pen.setColor(level_text_color(self._level))
            painter.setPen(pen)
            painter.drawArc(rect, 90 * 16, -round(self._score.value / 100 * 360 * 16))

        painter.setPen(QColor(255, 255, 255) if isDarkTheme() else QColor(0, 0, 0, 228))
        value_font = QFont(self.font())
        value_font.setPixelSize(round(self.height() * 0.25))
        value_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(value_font)
        value = (
            DASH
            if self._score is None
            else f"{'~' if self._score.estimated else ''}{self._score.value}"
        )
        value_rect = QRectF(0, self.height() * 0.24, self.width(), self.height() * 0.34)
        painter.drawText(value_rect, Qt.AlignmentFlag.AlignCenter, value)

        label_font = QFont(self.font())
        label_font.setPixelSize(max(11, round(self.height() * 0.1)))
        painter.setFont(label_font)
        label = self._caption or (self._score.label if self._score else "No score")
        painter.setPen(QColor(255, 255, 255, 190) if isDarkTheme() else QColor(0, 0, 0, 150))
        label_rect = QRectF(0, self.height() * 0.58, self.width(), self.height() * 0.16)
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label)


class Sparkline(QWidget):
    """Score over the last 24 hours as a small line."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(30)
        self._points: list[ScorePoint] = []
        self._color = QColor(themeColor())
        self._end = datetime.now().astimezone()
        self._span = timedelta(hours=24)
        _repaint_on_theme_change(self)

    def set_points(
        self, points: Sequence[ScorePoint], color: QColor, end: datetime, span: timedelta
    ) -> None:
        self._points, self._color, self._end = list(points), color, end
        # Fit the time axis to the data (at least 1 h), so a new router isn't a dot.
        covered = end - points[0].timestamp if points else span
        self._span = max(timedelta(hours=1), min(span, covered))
        if len(points) >= 2:
            low, high = min(p.value for p in points), max(p.value for p in points)
            hours = max(1, round(self._span.total_seconds() / 3600))
            self.setToolTip(
                f"Score over the last {hours} h: {low} to {high}, now {points[-1].value}"
            )
        else:
            self.setToolTip("Not enough history for a trend yet")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(200, 30)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        base = QColor(255, 255, 255, 40) if isDarkTheme() else QColor(0, 0, 0, 30)
        painter.setPen(QPen(base, 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(0, h - 1.5), QPointF(w, h - 1.5))
        if len(self._points) < 2:
            return
        start = self._end - self._span
        span_s = self._span.total_seconds()

        def point(p: ScorePoint) -> QPointF:
            x = (p.timestamp - start).total_seconds() / span_s * (w - 4) + 2
            y = (h - 4) - p.value / 100 * (h - 8) + 2
            return QPointF(max(2.0, min(w - 2.0, x)), y)

        pts = [point(p) for p in self._points]
        line = QPainterPath(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)
        area = QPainterPath(line)
        area.lineTo(pts[-1].x(), h)
        area.lineTo(pts[0].x(), h)
        area.closeSubpath()
        fill = QColor(self._color)
        fill.setAlpha(40)
        painter.fillPath(area, fill)
        pen = QPen(self._color, 1.8)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(line)


class ColorDot(QWidget):
    def __init__(self, color: str = "#0078D4", size: int = 12, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(0, 0, 0, 40), 1))
        painter.setBrush(self._color)
        painter.drawEllipse(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1))


class Badge(QWidget):
    """Small pill with text on the accent color (e.g. "Recommended")."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        font = QFont(self.font())
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self.setAccessibleName(text)
        _repaint_on_theme_change(self)

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self._text) + 20, metrics.height() + 8)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(themeColor())
        rect = QRectF(self.rect())
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setPen(QColor(0, 0, 0) if isDarkTheme() else QColor(255, 255, 255))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


class MetricBlock(QWidget):
    """Caption, big value and a small note under it."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = CaptionLabel(title, self)
        self.value = SubtitleLabel(DASH, self)
        self.note = CaptionLabel("", self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.note)

    def set_values(self, value: str, note: str = "", tooltip: str = "") -> None:
        self.value.setText(value)
        self.note.setText(note)
        self.setToolTip(tooltip)
        self.setAccessibleName(f"{self.title.text()}: {value}. {note}".strip())


class FocusCard(CardWidget):
    """A clickable card you can also reach with Tab and open with Enter or Space."""

    activated = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.clicked.connect(self.activated)
        _repaint_on_theme_change(self)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.hasFocus():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(themeColor(), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)
