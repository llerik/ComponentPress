"""Apply the disposable QSettings backend to test subprocesses."""

import os

from PySide6 import QtCore

from isolate_settings import install

install()
language = os.environ.get("COMPONENTPRESS_TEST_LANGUAGE")
if language in ("ru", "en"):
    settings = QtCore.QSettings("ComponentPress", "ComponentPress")
    settings.setValue("language", language)
    settings.sync()
