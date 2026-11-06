# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

root = Path(SPECPATH)
sys.path.insert(0, str(root / "src"))
from ed_fast_telemetry import DISPLAY_VERSION

executable_name = os.environ.get(
    "ED_FAST_TELEMETRY_EXE_NAME", f"ED-fast-telemetry-v{DISPLAY_VERSION}"
)

a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[(str(root / name), ".") for name in ("LICENSE", "AI_DEVELOPMENT.md")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Qt 6.11 on this Windows build links against the Windows system ICU DLL.
# PyInstaller can discover an unrelated ICU installation on PATH and bundle its
# unversioned icuuc.dll, which shadows the compatible system DLL and makes
# PySide6.QtWidgets fail with WinError 127 at startup. Keep the system ICU.
a.binaries = [
    item
    for item in a.binaries
    if item[0].lower() != "icuuc.dll" and not item[0].lower().startswith("icudt")
]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=executable_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
