"""Refilling a table must stay fast: columns are sized once per fill, not per cell.

This used to take minutes on the History page once it had a few hundred rows,
freezing the app after every check while the page was open.
"""

import time
from datetime import timedelta

from PySide6.QtCore import Qt

from fakes import T0, FakeWatcher, network_parts, record, sample_routers
from router_checker.core.settings import Settings
from router_checker.ui.controller import AppController, HistoryData, Services
from router_checker.ui.main_window import MainWindow

ROWS = 40  # took about 20 s to refill before the fix


def test_refilling_the_history_table_is_fast(qtbot) -> None:
    parts = network_parts()
    services = Services(
        parts["wifi"], parts["ping"], parts["dns"], parts["netinfo"], parts["clock"], FakeWatcher()
    )
    settings = Settings(routers=sample_routers(), first_run_done=True)
    controller = AppController(settings, None, parts["store"], services)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)  # laid out, but off screen
    window.show()
    window.switchTo(window.history)
    checks = [record(timestamp=T0 - timedelta(minutes=5 * i), router_id="zte") for i in range(ROWS)]
    try:
        for _ in range(2):  # the second fill replaces every cell: that was the slow part
            qtbot.wait(200)  # let the page lay out, as it has when a check finishes
            started = time.perf_counter()
            window.history._show(HistoryData(checks, []))
            assert time.perf_counter() - started < 2.0
        table = window.history.checks
        assert table.rowCount() == ROWS and table.item(0, 1).text() == "ZTE"
        assert table.columnWidth(0) > 40  # sized to the contents
    finally:
        window.prepare_quit()
        controller.shutdown()
