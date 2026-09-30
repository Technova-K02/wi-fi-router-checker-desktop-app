"""Cards with charts: a router's history and popular times (router details), and
the score of every router side by side (History page)."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    SegmentedWidget,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
)

from router_checker.core.popularity import PopularTimes
from router_checker.core.presentation import (
    DASH,
    fmt_ms,
    fmt_pct,
    popular_times_summary,
    series_summary,
)
from router_checker.core.series import CHART_SPANS, RouterCharts
from router_checker.ui.charts import Line, Reference, TimeChart
from router_checker.ui.controller import AppController, ScoreHistory
from router_checker.ui.heatmap import HeatSwatch, PopularTimesHeatmap
from router_checker.ui.style import GATEWAY_LINE, INTERNET_LINE

SPANS = dict(CHART_SPANS)


def fmt_score(value: float | None) -> str:
    return DASH if value is None else f"{value:.0f}"


def _span_switch(parent: QWidget, keys: list[str], current: str) -> SegmentedWidget:
    switch = SegmentedWidget(parent)
    for key in keys:
        switch.addItem(routeKey=key, text=key)
    switch.setCurrentItem(current)
    switch.setAccessibleName("Time span")
    return switch


def _span_words(key: str) -> str:
    return {"1 h": "hour", "24 h": "24 hours", "7 d": "7 days"}[key]


class _Card(SimpleCardWidget):
    """A card with a title row (title, stretch, extra widgets) and a body layout."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = SubtitleLabel(title, self)
        self.header = QHBoxLayout()
        self.header.addWidget(self.title)
        self.header.addStretch(1)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(24, 18, 24, 20)
        self.body.setSpacing(10)
        self.body.addLayout(self.header)


class RouterHistoryCard(_Card):
    """Latency, packet loss and score of one router over 1 h / 24 h / 7 d."""

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__("History", parent)
        self.controller = controller
        self.router_id: str | None = None
        self.span_key = "24 h"
        self.span = _span_switch(self, list(SPANS), self.span_key)
        self.span.currentItemChanged.connect(self._span_changed)
        self.header.addWidget(self.span)
        self.note = CaptionLabel("", self)
        self.note.setWordWrap(True)
        self.latency = TimeChart(fmt_ms, self)
        self.loss = TimeChart(fmt_pct, self, min_top=10)
        self.score = TimeChart(fmt_score, self, fixed_top=100)
        self.body.addWidget(self.note)
        for title, chart in (
            ("Latency", self.latency),
            ("Packet loss", self.loss),
            ("Stability score", self.score),
        ):
            self.body.addWidget(StrongBodyLabel(title, self))
            self.body.addWidget(chart)

    def show_router(self, router_id: str) -> None:
        self.router_id = router_id
        self.reload()

    def reload(self) -> None:
        if self.router_id is not None:
            self.controller.load_router_charts(self.router_id, SPANS[self.span_key], self._show)

    def _span_changed(self, key: str) -> None:
        self.span_key = key
        self.reload()

    def _show(self, charts: RouterCharts) -> None:
        router = self.controller.settings.router(charts.router_id)
        if charts.router_id != self.router_id or router is None:
            return
        words = _span_words(self.span_key)
        if charts.checks:
            tests = "1 test" if charts.checks == 1 else f"{charts.checks} tests"
            self.note.setText(f"{tests} in the last {words}.")
        else:
            self.note.setText(
                f"No tests in the last {words}. A router is tested while you're connected to it."
            )
        gateway, internet = charts.latency
        self.latency.set_lines(
            [Line(gateway, GATEWAY_LINE), Line(internet, INTERNET_LINE)],
            charts.start,
            charts.end,
            f"{series_summary(gateway, fmt_ms)} · {series_summary(internet, fmt_ms)}",
        )
        gateway_loss, internet_loss = charts.loss
        threshold = self.controller.settings.thresholds.loss_pct
        self.loss.set_lines(
            [Line(gateway_loss, GATEWAY_LINE), Line(internet_loss, INTERNET_LINE)],
            charts.start,
            charts.end,
            f"{series_summary(gateway_loss, fmt_pct)} · {series_summary(internet_loss, fmt_pct)}",
            [Reference(threshold, f"Unstable at {fmt_pct(threshold)}")],
        )
        measured, estimated = charts.score
        self.score.set_lines(
            [
                Line(measured, router.color, group="Score"),
                Line(estimated, router.color, True, group="Score"),
            ],
            charts.start,
            charts.end,
            " · ".join(
                series_summary(s, fmt_score, lower_is_worse=True)
                for s in (measured, estimated)
                if s.values
            )
            or "No score yet.",
        )


class PopularTimesCard(_Card):
    """How busy the router usually is, by weekday and hour."""

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__("Popular times", parent)
        self.controller = controller
        self.router_id: str | None = None
        self.summary = BodyLabel("", self)
        self.summary.setWordWrap(True)
        self.heatmap = PopularTimesHeatmap(self)
        self.readout = CaptionLabel("", self)
        self.heatmap.cellChanged.connect(self.readout.setText)
        legend = QHBoxLayout()
        legend.setSpacing(6)
        for value, text in ((0.15, "Low"), (0.5, "Medium"), (0.85, "High"), (None, "No data")):
            legend.addWidget(HeatSwatch(value, self))
            legend.addWidget(CaptionLabel(text, self))
            legend.addSpacing(10)
        legend.addStretch(1)
        note = CaptionLabel(
            "From Wi-Fi scans while Router Checker runs, in your local time. "
            "Use the arrow keys to read each hour.",
            self,
        )
        note.setWordWrap(True)
        self.body.addWidget(self.summary)
        self.body.addWidget(self.heatmap)
        self.body.addWidget(self.readout)
        self.body.addLayout(legend)
        self.body.addWidget(note)

    def show_router(self, router_id: str) -> None:
        self.router_id = router_id
        self.reload()

    def reload(self) -> None:
        router_id = self.router_id
        if router_id is not None:
            self.controller.load_popular_times(router_id, lambda t: self._show(router_id, t))

    def _show(self, router_id: str, times: PopularTimes) -> None:
        if router_id == self.router_id:
            self.summary.setText(popular_times_summary(times))
            self.heatmap.set_times(times)


class ScoreComparisonCard(_Card):
    """Every router's score over 24 h / 7 d in one chart."""

    KEYS = ("24 h", "7 d")

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__("Score by router", parent)
        self.controller = controller
        self.span_key = "24 h"
        self.span = _span_switch(self, list(self.KEYS), self.span_key)
        self.span.currentItemChanged.connect(self._span_changed)
        self.header.addWidget(self.span)
        self.chart = TimeChart(fmt_score, self, fixed_top=100, height=220)
        note = CaptionLabel(
            "Solid: measured while connected. Dashed: estimated from Wi-Fi scans.", self
        )
        note.setWordWrap(True)
        self.body.addWidget(self.chart)
        self.body.addWidget(note)

    def reload(self) -> None:
        self.controller.load_score_lines(SPANS[self.span_key], self._show)

    def _span_changed(self, key: str) -> None:
        self.span_key = key
        self.reload()

    def _show(self, history: ScoreHistory) -> None:
        lines: list[Line] = []
        summary: list[str] = []
        for score_line in history.lines:
            router = self.controller.settings.router(score_line.router_id)
            if router is None:
                continue
            latest_estimated = score_line.latest is not None and score_line.latest.estimated
            lines.append(
                Line(score_line.measured, router.color, name=router.name, group=router.id,
                     end_label=not latest_estimated)
            )  # fmt: skip
            lines.append(
                Line(score_line.estimated, router.color, True, name=f"{router.name} (estimated)",
                     group=router.id, end_label=latest_estimated, in_legend=False)
            )  # fmt: skip
            if score_line.latest is not None:
                approx = "~" if latest_estimated else ""
                summary.append(f"{router.name} {approx}{score_line.latest.value}")
        text = "Latest: " + ", ".join(summary) if summary else "No scores in this period yet."
        self.chart.set_lines(lines, history.start, history.end, text)
