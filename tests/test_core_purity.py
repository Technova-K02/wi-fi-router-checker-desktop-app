"""core must stay free of Windows-only and Qt imports so it runs on any OS."""

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).parents[1] / "src" / "router_checker" / "core"
FORBIDDEN = ("ctypes", "winreg", "msvcrt", "_winapi", "PySide6", "qfluentwidgets", "pyqtgraph",
             "windows_toasts", "router_checker.platform_windows", "router_checker.ui")  # fmt: skip


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", sorted(CORE.glob("*.py")), ids=lambda p: p.name)
def test_core_module_has_no_platform_imports(path: Path) -> None:
    bad = {
        m for m in imported_modules(path) if m.split(".")[0] in FORBIDDEN or m.startswith(FORBIDDEN)
    }
    assert not bad, f"{path.name} imports {sorted(bad)}"
