"""Start with Windows: the command line and how to read Windows' on/off flag.

The app starts at sign-in through a value named ``RUN_VALUE`` under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` (no admin rights).
Task Manager's Startup apps page can turn it off without deleting it; it then
writes a flag under ``...\\Explorer\\StartupApproved\\Run`` with the same name.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

RUN_VALUE = "RouterChecker"
STARTUP_ARGS = ("--minimized",)  # sign-in starts the app in the tray


def startup_command(program: Sequence[str]) -> str:
    """The Run value for ``program`` (the exe, or python plus its arguments),
    quoted the way Windows splits command lines, with ``STARTUP_ARGS`` added."""
    return subprocess.list2cmdline([*program, *STARTUP_ARGS])


def approved(flag: bytes | None) -> bool:
    """False when Task Manager turned the startup entry off.

    The flag's first byte is even (2, 6) when on and odd (3, 7) when off;
    no flag means on.
    """
    return not flag or flag[0] % 2 == 0
