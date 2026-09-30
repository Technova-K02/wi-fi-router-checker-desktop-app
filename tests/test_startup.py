from router_checker.core.startup import approved, startup_command

INSTALLED = r"C:\Users\Ann Lee\AppData\Local\Programs\Router Checker\RouterChecker.exe"


def test_the_command_quotes_paths_with_spaces_and_starts_in_the_tray() -> None:
    assert startup_command([INSTALLED]) == f'"{INSTALLED}" --minimized'
    assert startup_command([r"C:\dev\.venv\Scripts\pythonw.exe", "-m", "router_checker.ui"]) == (
        r"C:\dev\.venv\Scripts\pythonw.exe -m router_checker.ui --minimized"
    )


def test_task_managers_flag_turns_the_entry_off_when_its_first_byte_is_odd() -> None:
    assert approved(None)
    assert approved(b"")
    assert approved(bytes([2] + [0] * 11))
    assert approved(bytes([6] + [0] * 11))
    assert not approved(bytes([3] + [0] * 11))
    assert not approved(bytes([7] + [0] * 11))
