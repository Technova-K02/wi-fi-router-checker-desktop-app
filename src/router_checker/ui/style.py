"""Colors and painted shapes shared by the UI (status icons, app icon)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from qfluentwidgets import isDarkTheme

from router_checker.core.presentation import StatusLevel

# Icon fills work on light and dark backgrounds (tray, cards). The glyph on top
# (check mark, "!", cross, "?") keeps the status readable without color.
_FILL = {
    StatusLevel.GOOD: "#1E9E4A",
    StatusLevel.WARNING: "#F2B01E",
    StatusLevel.BAD: "#D13438",
    StatusLevel.UNKNOWN: "#8A8886",
}
_GLYPH = {
    StatusLevel.GOOD: "#FFFFFF",
    StatusLevel.WARNING: "#1F1F1F",
    StatusLevel.BAD: "#FFFFFF",
    StatusLevel.UNKNOWN: "#FFFFFF",
}
# Text colors with enough contrast on the light and dark window backgrounds.
_TEXT_LIGHT = {
    StatusLevel.GOOD: "#0F7B0F",
    StatusLevel.WARNING: "#9D5D00",
    StatusLevel.BAD: "#C42B1C",
    StatusLevel.UNKNOWN: "#5C5C5C",
}
_TEXT_DARK = {
    StatusLevel.GOOD: "#6CCB5F",
    StatusLevel.WARNING: "#FCE100",
    StatusLevel.BAD: "#FF99A4",
    StatusLevel.UNKNOWN: "#C5C5C5",
}

APP_BLUE = "#0078D4"


def level_fill(level: StatusLevel) -> QColor:
    return QColor(_FILL[level])


def level_text_colors(level: StatusLevel) -> tuple[QColor, QColor]:
    """(light theme, dark theme) text color for a status level."""
    return QColor(_TEXT_LIGHT[level]), QColor(_TEXT_DARK[level])


def level_text_color(level: StatusLevel) -> QColor:
    light, dark = level_text_colors(level)
    return dark if isDarkTheme() else light


def text_color(alpha: int = 230) -> QColor:
    return QColor(255, 255, 255, alpha) if isDarkTheme() else QColor(0, 0, 0, alpha)


def paint_status_icon(painter: QPainter, rect: QRectF, level: StatusLevel) -> None:
    """A filled circle with a glyph: check mark, "!", cross or "?"."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(level_fill(level))
    painter.drawEllipse(rect)

    w = rect.width()
    glyph = QColor(_GLYPH[level])

    def at(fx: float, fy: float) -> QPointF:
        return QPointF(rect.x() + fx * w, rect.y() + fy * w)

    pen = QPen(glyph, max(1.3, w * 0.12))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if level is StatusLevel.GOOD:
        path = QPainterPath(at(0.28, 0.53))
        path.lineTo(at(0.44, 0.68))
        path.lineTo(at(0.73, 0.36))
        painter.drawPath(path)
    elif level is StatusLevel.BAD:
        painter.drawLine(at(0.35, 0.35), at(0.65, 0.65))
        painter.drawLine(at(0.65, 0.35), at(0.35, 0.65))
    elif level is StatusLevel.WARNING:
        painter.drawLine(at(0.5, 0.25), at(0.5, 0.56))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glyph)
        painter.drawEllipse(at(0.5, 0.74), w * 0.075, w * 0.075)
    else:
        font = QFont("Segoe UI")
        font.setPixelSize(max(8, round(w * 0.68)))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(glyph)
        painter.drawText(rect.adjusted(0, -w * 0.02, 0, 0), Qt.AlignmentFlag.AlignCenter, "?")
    painter.restore()


def _icon(paint, sizes=(16, 20, 24, 32, 40, 48, 64)) -> QIcon:
    icon = QIcon()
    for size in sizes:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        paint(painter, size)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def status_icon(level: StatusLevel) -> QIcon:
    return _icon(lambda p, s: paint_status_icon(p, QRectF(0.5, 0.5, s - 1, s - 1), level))


def app_icon() -> QIcon:
    """Placeholder app icon (Wi-Fi arcs on a blue tile) until Phase 6."""

    def paint(p: QPainter, s: int) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(APP_BLUE))
        p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
        pen = QPen(QColor("#FFFFFF"), max(1.2, s * 0.08))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        cx, cy = s / 2, s * 0.74
        for r in (s * 0.16, s * 0.3, s * 0.44):
            p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 45 * 16, 90 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QPointF(cx, cy), s * 0.06, s * 0.06)

    return _icon(paint, (16, 20, 24, 32, 48, 64, 128, 256))


# Chart lines as (light theme, dark theme): readable on both window backgrounds.
GATEWAY_LINE = ("#005FB8", "#60CDFF")
INTERNET_LINE = ("#8E4EC6", "#D6A6FF")
