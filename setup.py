"""cx_Freeze build script.

    python fetch_ffmpeg.py      -> ffmpeg/ (bundled ffmpeg + ffprobe, once)
    python setup.py build_exe   -> build/exe.win-*/mp4trim.exe
    python setup.py bdist_msi   -> dist/mp4trim-<version>-win64.msi

The installer is self-contained: it ships Python, Qt, the MSVC runtime and
ffmpeg/ffprobe, so the target PC needs nothing else installed.
"""

import sys
from pathlib import Path

from cx_Freeze import Executable, setup

HERE = Path(__file__).parent
FFMPEG = HERE / "ffmpeg"
ff_files = sorted(p for p in FFMPEG.glob("*")
                  if p.suffix.lower() in (".exe", ".dll")
                  and p.name.lower() != "ffplay.exe") if FFMPEG.is_dir() else []
if any(a.startswith(("build", "bdist")) for a in sys.argv) and not any(
        p.name.lower() == "ffmpeg.exe" for p in ff_files):
    sys.exit("ffmpeg/ffmpeg.exe missing. Run: python fetch_ffmpeg.py")

build_exe_options = {
    "packages": [
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    ],
    "excludes": [
        "tkinter", "unittest", "email", "http", "xmlrpc", "pydoc", "test",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtDBus", "PySide6.QtPdf",
    ],
    "include_files": [
        "icon.ico",
        *[(str(p), f"ffmpeg/{p.name}") for p in ff_files],
        *([(str(FFMPEG / "LICENSE.txt"), "ffmpeg/LICENSE.txt")]
          if (FFMPEG / "LICENSE.txt").exists() else []),
    ],
    # ship vcruntime140*.dll / msvcp140*.dll so a PC without the
    # Visual C++ Redistributable can still start the app
    "include_msvcr": True,
}

bdist_msi_options = {
    # keep this GUID stable across releases so upgrades replace old installs
    "upgrade_code": "{7A5C0F3E-9B21-4D64-8E7A-2F1B3C4D5E6F}",
    "all_users": False,
    "initial_target_dir": r"[LocalAppDataFolder]\mp4trim",
    # icon shown in the installer UI and Apps / Add-Remove Programs list
    "install_icon": "icon.ico",
}

setup(
    name="mp4trim",
    version="2.2.0",
    description="MP4 trimmer - lossless trim and Discord-sized exports",
    options={"build_exe": build_exe_options, "bdist_msi": bdist_msi_options},
    executables=[
        Executable(
            "mp4trim.py",
            base="gui",
            icon="icon.ico",
            target_name="mp4trim.exe",
            shortcut_name="mp4trim",
            shortcut_dir="ProgramMenuFolder",
        )
    ],
)
