# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import runpy
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)


spec_directory = Path(SPECPATH)
settings = runpy.run_path(str(spec_directory / "common.py"))
root = settings["ROOT"]
release_version = settings["RELEASE_VERSION"]
executable_name = settings["EXECUTABLE_NAME"]
version_parts = tuple(int(part) for part in release_version.split("."))
fixed_version = (*version_parts, *(0 for _ in range(4 - len(version_parts))))
application_assets = root / "src" / "componentpress" / "app_assets"
qt_translations = root / ".venv" / "Lib" / "site-packages" / "PySide6" / "translations"
i18n_assets = root / "src" / "componentpress" / "i18n"
windows_icon = spec_directory / "componentpress.ico"
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=fixed_version,
        prodvers=fixed_version,
        mask=0x3f,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable(
                "040904B0",
                [
                    StringStruct("CompanyName", "ComponentPress"),
                    StringStruct("FileDescription", "ComponentPress — редактор печатных компонентов"),
                    StringStruct("FileVersion", release_version),
                    StringStruct("InternalName", "ComponentPress"),
                    StringStruct("OriginalFilename", f"{executable_name}.exe"),
                    StringStruct("ProductName", "ComponentPress"),
                    StringStruct("ProductVersion", release_version),
                ],
            )
        ]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

analysis = Analysis(
    [str(spec_directory / "windows_entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[
        *((str(asset), "componentpress/app_assets") for asset in application_assets.glob("*.png")),
        *((str(asset), "componentpress/i18n") for asset in i18n_assets.glob("*.json")),
        *((str(qt_translations / f"qtbase_{language}.qm"), "PySide6/translations") for language in ("ru", "en") if (qt_translations / f"qtbase_{language}.qm").is_file()),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
# The desktop host prepends its own document-tool runtimes to PATH. They are
# not application dependencies and, in particular, its version-suffixed ICU
# build is binary-incompatible with the Windows ICU API used by Qt.
analysis.binaries = [
    entry for entry in analysis.binaries
    if "\\.cache\\codex-runtimes\\" not in entry[1].lower()
]
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name=executable_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
    icon=str(windows_icon),
)
