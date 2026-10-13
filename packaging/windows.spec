# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import runpy


spec_directory = Path(SPECPATH)
settings = runpy.run_path(str(spec_directory / "common.py"))
root = settings["ROOT"]
executable_name = settings["EXECUTABLE_NAME"]
application_assets = root / "src" / "componentpress" / "app_assets"
qt_translations = root / ".venv" / "Lib" / "site-packages" / "PySide6" / "translations"
i18n_assets = root / "src" / "componentpress" / "i18n"
windows_icon = spec_directory / "componentpress.ico"

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
    version=str(spec_directory / "windows_version_info.txt"),
    icon=str(windows_icon),
)
