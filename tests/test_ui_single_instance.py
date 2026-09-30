import subprocess
import sys
import uuid

import pytest

from router_checker.ui.single_instance import SingleInstance, request_exit

TIMEOUT = 15_000


@pytest.fixture
def running(qtbot):
    name = f"RouterCheckerTest-{uuid.uuid4().hex}"
    first = SingleInstance(name, pids=(4242, 4343))
    assert not first.already_running()
    return name, first


def second_launch(qtbot, call: str) -> str:
    """Run ``call`` in another process, as a second launch would (in one process
    the local socket never delivers), while this one answers; what it printed."""
    code = (
        "from PySide6.QtCore import QCoreApplication\n"
        "from router_checker.ui.single_instance import SingleInstance, request_exit\n"
        f"app = QCoreApplication([])\nprint({call})\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    qtbot.waitUntil(lambda: process.poll() is not None, timeout=TIMEOUT)
    return process.stdout.read().strip()


def test_a_second_launch_brings_the_first_forward(qtbot, running) -> None:
    name, first = running
    with qtbot.waitSignal(first.activated, timeout=TIMEOUT):
        assert second_launch(qtbot, f"SingleInstance({name!r}).already_running()") == "True"


def test_exit_closes_the_running_copy_and_names_its_processes(qtbot, running) -> None:
    name, first = running
    with qtbot.waitSignal(first.exitRequested, timeout=TIMEOUT):
        assert second_launch(qtbot, f"request_exit({name!r})") == "[4242, 4343]"


def test_exit_when_nothing_runs(qtbot) -> None:
    assert request_exit(f"RouterCheckerTest-{uuid.uuid4().hex}") is None
