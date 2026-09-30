r"""The registry side of Start with Windows, against a throwaway key under
HKCU\Software\RouterCheckerTests (never the real Run key). Windows only."""

import contextlib
import uuid

import pytest

winreg = pytest.importorskip("winreg")

from router_checker.core.startup import RUN_VALUE  # noqa: E402
from router_checker.platform_windows.startup import WindowsStartup  # noqa: E402

TESTS_KEY = r"Software\RouterCheckerTests"


@pytest.fixture
def keys():
    base = rf"{TESTS_KEY}\{uuid.uuid4().hex}"
    yield base + r"\Run", base + r"\Approved"
    for key in (base + r"\Run", base + r"\Approved", base, TESTS_KEY):
        # OSError: never created, or another test's key is still inside.
        with contextlib.suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)


def startup(keys, command):
    run, approved = keys
    return WindowsStartup(command, run_key=run, approved_key=approved)


def turn_off_in_task_manager(keys) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, keys[1], 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_BINARY, bytes([3] + [0] * 11))


def test_on_and_off(keys) -> None:
    app = startup(keys, r'"C:\A B\RouterChecker.exe" --minimized')
    assert (app.registered(), app.enabled()) == (None, False)
    app.set_enabled(False)  # already off: nothing to do
    app.set_enabled(True)
    assert app.registered() == r'"C:\A B\RouterChecker.exe" --minimized'
    assert app.enabled()
    app.set_enabled(False)
    assert (app.registered(), app.enabled()) == (None, False)


def test_task_manager_can_turn_it_off_and_the_app_back_on(keys) -> None:
    app = startup(keys, "RouterChecker.exe --minimized")
    app.set_enabled(True)
    turn_off_in_task_manager(keys)
    assert app.registered() is not None
    assert not app.enabled()
    app.set_enabled(True)
    assert app.enabled()


def test_a_moved_app_points_the_entry_at_itself(keys) -> None:
    old = startup(keys, "old.exe --minimized")
    new = startup(keys, "new.exe --minimized")
    assert not new.update_command()  # off stays off
    assert new.registered() is None
    old.set_enabled(True)
    turn_off_in_task_manager(keys)
    assert new.update_command()
    assert new.registered() == "new.exe --minimized"
    assert not new.enabled()  # Task Manager's choice is kept
    assert not new.update_command()
