"""Per-user Windows registration for the .cpress file type."""

from __future__ import annotations

from pathlib import Path
import sys


PROG_ID = "ComponentPress.Project"


def register_current_executable() -> tuple[bool, str]:
    """Register the frozen executable for the current user, without elevation."""
    executable = Path(sys.executable).resolve()
    extraction = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or extraction and executable.is_relative_to(Path(extraction).resolve()):
        return False, "REGISTRATION_UNAVAILABLE"
    if executable.suffix.casefold() != ".exe" or not executable.is_file():
        return False, "REGISTRATION_UNAVAILABLE"
    try:
        import winreg
    except ImportError:
        return False, "REGISTRATION_FAILED"
    return register_executable(executable, winreg)


def register_executable(executable: Path, registry) -> tuple[bool, str]:
    """Write an association through a registry-compatible adapter."""
    classes = r"Software\Classes"
    entries = (
        (classes + r"\.cpress", PROG_ID),
        (classes + "\\" + PROG_ID, "ComponentPress project"),
        (classes + "\\" + PROG_ID + r"\DefaultIcon", f'"{executable}",0'),
        (classes + "\\" + PROG_ID + r"\shell\open\command", f'"{executable}" "%1"'),
    )
    try:
        for subkey, value in entries:
            with registry.CreateKey(registry.HKEY_CURRENT_USER, subkey) as key:
                registry.SetValueEx(key, None, 0, registry.REG_SZ, value)
    except OSError:
        return False, "REGISTRATION_FAILED"
    return True, "REGISTRATION_SUCCEEDED"
