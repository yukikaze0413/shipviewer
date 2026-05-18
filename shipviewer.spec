# -*- mode: python ; coding: utf-8 -*-

import os
import sys

from PyInstaller.utils.hooks import collect_all


datas = [("styles/dark_theme.qss", "styles")]
binaries = []
hiddenimports = []

for package_name in ("PySide6", "vtk", "vtkmodules", "rhino3dm", "numpy"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports


def add_runtime_dlls():
    dll_names = (
        "ffi.dll",
        "ffi-7.dll",
        "ffi-8.dll",
        "libcrypto-3-x64.dll",
        "libssl-3-x64.dll",
        "libbz2.dll",
        "liblzma.dll",
        "libexpat.dll",
        "libmpdec-4.dll",
        "tcl86t.dll",
        "tk86t.dll",
    )
    search_dirs = (
        os.path.join(sys.base_prefix, "Library", "bin"),
        os.path.join(sys.base_prefix, "DLLs"),
    )
    seen_binary = {os.path.normcase(src) for src, _dest in binaries}
    seen_data = {os.path.normcase(src) for src, _dest in datas}
    for dll_name in dll_names:
        for directory in search_dirs:
            candidate = os.path.join(directory, dll_name)
            if not os.path.exists(candidate):
                continue
            normalized = os.path.normcase(candidate)
            if normalized in seen_binary and normalized in seen_data:
                break
            if normalized not in seen_binary:
                binaries.append((candidate, "."))
                seen_binary.add(normalized)
            if normalized not in seen_data:
                datas.append((candidate, "."))
                seen_data.add(normalized)
            break


add_runtime_dlls()


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ShipViewer",
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
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ShipViewer",
)
