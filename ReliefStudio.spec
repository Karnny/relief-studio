# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Relief Studio.

Windows:  pyinstaller ReliefStudio.spec --noconfirm --clean
          -> dist/ReliefStudio.exe

The same spec builds a testable binary on macOS/Linux (dist/ReliefStudio),
which is how the packaging is smoke-tested before shipping to Windows.
"""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(SPECPATH)
STATIC = os.path.join(ROOT, "static")
ICON_ICO = os.path.join(ROOT, "assets", "icon.ico")
ICON_ICNS = os.path.join(ROOT, "assets", "icon.icns")
ICON = ICON_ICNS if sys.platform == "darwin" else ICON_ICO

a = Analysis(
    [os.path.join(ROOT, "relief_gui.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[(STATIC, "static")],
    hiddenimports=collect_submodules("PIL"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ReliefStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windows: keep the console - it shows the URL, gives Ctrl+C, and lets the
    # app run from an SSH/service session (a windowed build refuses redirected stdin).
    # macOS: the bundle has no console concept, so stay windowed.
    console=(sys.platform != "darwin"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON if os.path.exists(ICON) else None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Relief Studio.app",
        icon=ICON_ICNS if os.path.exists(ICON_ICNS) else None,
        bundle_identifier="tools.rea.reliefstudio",
        info_plist={
            "CFBundleName": "Relief Studio",
            "CFBundleDisplayName": "Relief Studio",
            "CFBundleShortVersionString": "1.5.0",
            "CFBundleVersion": "1.5.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.graphics-design",
            "NSRequiresAquaSystemAppearance": False,
        },
    )
