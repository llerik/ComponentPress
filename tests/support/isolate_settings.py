"""Route test-process QSettings constructors to disposable INI files."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from PySide6 import QtCore


_QSettings = QtCore.QSettings
_ROOT = Path(os.environ.get("COMPONENTPRESS_TEST_SETTINGS_ROOT", tempfile.gettempdir()))
_TEST_ID = os.environ.get("COMPONENTPRESS_TEST_SETTINGS_ID", "pytest-startup")


def _settings_path(scope: _QSettings.Scope) -> Path:
    root = Path(os.environ.get("COMPONENTPRESS_TEST_SETTINGS_ROOT", str(_ROOT)))
    return root / _TEST_ID / f"process-{os.getpid()}" / scope.name.lower()


def set_test_id(value: str) -> None:
    global _TEST_ID
    _TEST_ID = value
    os.environ["COMPONENTPRESS_TEST_SETTINGS_ID"] = value
    for scope in (_QSettings.Scope.UserScope, _QSettings.Scope.SystemScope):
        _QSettings.setPath(
            _QSettings.Format.IniFormat,
            scope,
            str(_settings_path(scope)),
        )


class IsolatedQSettings(_QSettings):
    def __init__(self, *args, **kwargs):
        if not args:
            super().__init__(
                self.Format.IniFormat,
                self.Scope.UserScope,
                self._QCoreApplication.organizationName() or "ComponentPressTest",
                self._QCoreApplication.applicationName() or "ComponentPressTest",
                **kwargs,
            )
            return
        if len(args) >= 2 and all(isinstance(value, str) for value in args[:2]):
            args = (self.Format.IniFormat, self.Scope.UserScope, *args)
        elif args[0] == self.Format.NativeFormat:
            args = (self.Format.IniFormat, *(args[1:] or (self.Scope.UserScope,)))
        super().__init__(*args, **kwargs)

    _QCoreApplication = QtCore.QCoreApplication

    @staticmethod
    def setDefaultFormat(_format):
        _QSettings.setDefaultFormat(_QSettings.Format.IniFormat)

    @staticmethod
    def setPath(format, scope, _path):
        if format == _QSettings.Format.NativeFormat:
            return
        _QSettings.setPath(format, scope, str(_settings_path(scope)))


def install() -> None:
    _QSettings.setDefaultFormat(_QSettings.Format.IniFormat)
    for scope in (_QSettings.Scope.UserScope, _QSettings.Scope.SystemScope):
        _QSettings.setPath(
            _QSettings.Format.IniFormat,
            scope,
            str(_settings_path(scope)),
        )
    QtCore.QSettings = IsolatedQSettings


install()
