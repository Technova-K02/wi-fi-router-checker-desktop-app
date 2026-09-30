"""Build dist\\RouterChecker.exe, a single windowed .exe of the desktop app, and
its installer dist\\RouterChecker-<version>-setup.exe.

    uv run --group build python packaging/build.py [--exe-only]

The icon comes from the app's own icon drawing and the file version from
``router_checker.__version__``; both are written to build\\ first. The console
harness (``router-checker``) is not part of the exe. The installer needs
Inno Setup 6 (``winget install JRSoftware.InnoSetup``); without it only the
exe is built.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DIST = ROOT / "dist"
NAME = "RouterChecker"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

sys.path.insert(0, str(ROOT / "src"))

from router_checker import __version__  # noqa: E402


def write_icon(path: Path) -> None:
    """A multi-size .ico (PNG images inside, as Windows Vista and later read them)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtWidgets import QApplication

    from router_checker.ui.style import app_icon

    app = QApplication.instance() or QApplication([])
    icon = app_icon()
    images = []
    for size in ICON_SIZES:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        icon.pixmap(size, size).toImage().save(buffer, "PNG")
        images.append((size, bytes(data)))
    del app

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        side = 0 if size >= 256 else size  # 0 means 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.write_bytes(header + entries + blobs)


def write_version_file(path: Path) -> None:
    """File properties and Task Manager show "Router Checker" and the version."""
    parts = [int(p) for p in __version__.split(".")] + [0] * 4
    numbers = tuple(parts[:4])
    strings = {
        "CompanyName": "Router Checker",
        "FileDescription": "Router Checker",
        "FileVersion": __version__,
        "InternalName": NAME,
        "LegalCopyright": "Personal use",
        "OriginalFilename": f"{NAME}.exe",
        "ProductName": "Router Checker",
        "ProductVersion": __version__,
    }
    items = ",\n          ".join(f"StringStruct({k!r}, {v!r})" for k, v in strings.items())
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
          {items}])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


def find_iscc() -> Path | None:
    """Inno Setup's compiler: on PATH, or where its installer puts it."""
    found = shutil.which("iscc")
    if found:
        return Path(found)
    env = os.environ
    folders = [Path(env["LOCALAPPDATA"]) / "Programs"] if env.get("LOCALAPPDATA") else []
    folders += [Path(env[name]) for name in ("ProgramFiles(x86)", "ProgramFiles") if env.get(name)]
    for folder in folders:  # per-user install first, then for all users
        candidate = folder / "Inno Setup 6" / "ISCC.exe"
        if candidate.exists():
            return candidate
    return None


def build_installer(exe: Path, icon: Path) -> Path | None:
    iscc = find_iscc()
    if iscc is None:
        print("\nInno Setup 6 not found, so no installer (winget install JRSoftware.InnoSetup).")
        return None
    subprocess.run(
        [
            str(iscc),
            "/Q",
            f"/DAppVersion={__version__}",
            f"/DExe={exe}",
            f"/DIcon={icon}",
            f"/DOutputDir={DIST}",
            str(ROOT / "packaging" / "installer.iss"),
        ],
        check=True,
    )
    return DIST / f"{NAME}-{__version__}-setup.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the exe and its installer.")
    parser.add_argument("--exe-only", action="store_true", help="skip the installer")
    args = parser.parse_args()
    BUILD.mkdir(exist_ok=True)
    icon, version = BUILD / f"{NAME}.ico", BUILD / "version.txt"
    write_icon(icon)
    write_version_file(version)

    import PyInstaller.__main__

    # Absolute paths throughout: PyInstaller resolves relative ones against --specpath.
    PyInstaller.__main__.run(
        [
            str(ROOT / "packaging" / "launcher.py"),
            f"--name={NAME}",
            "--onefile",
            "--windowed",
            "--noconfirm",
            "--clean",
            f"--icon={icon}",
            f"--version-file={version}",
            f"--paths={ROOT / 'src'}",
            f"--distpath={DIST}",
            f"--workpath={BUILD / 'pyinstaller'}",
            f"--specpath={BUILD}",
            # Notifications: the WinRT projections windows-toasts loads.
            "--collect-submodules=winrt",
            "--collect-binaries=winrt",
            "--exclude-module=tkinter",
        ]
    )
    exe = DIST / f"{NAME}.exe"
    print(f"\nBuilt {exe} ({exe.stat().st_size / 1_000_000:.0f} MB), version {__version__}")
    if not args.exe_only:
        setup = build_installer(exe, icon)
        if setup is not None:
            print(f"Built {setup} ({setup.stat().st_size / 1_000_000:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
