"""cx_Freeze build script.

    python setup.py build_exe   -> build/exe.win-*/mp4trim.exe
    python setup.py bdist_msi   -> dist/mp4trim-1.0.0-win64.msi
"""

from cx_Freeze import Executable, setup

build_exe_options = {
    "packages": [
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    ],
    "excludes": [
        "tkinter", "unittest", "email", "http", "xmlrpc", "pydoc", "test",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtDBus", "PySide6.QtPdf",
    ],
    "include_files": ["icon.ico"],
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
    version="1.1.0",
    description="Simple MP4 trimmer - stream copy, hybrid DV/HDR10 safe",
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
