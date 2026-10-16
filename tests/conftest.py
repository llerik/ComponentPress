from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile

import pytest


_SUPPORT = Path(__file__).parent / "support"
sys.path.insert(0, str(_SUPPORT))
_SETTINGS_ROOT = Path(tempfile.mkdtemp(prefix="componentpress-tests-"))
os.environ["COMPONENTPRESS_TEST_SETTINGS_ROOT"] = str(_SETTINGS_ROOT)
os.environ["COMPONENTPRESS_LOG_DIR"] = str(_SETTINGS_ROOT / "logs")
os.environ["PYTHONPATH"] = os.pathsep.join(
    (str(_SUPPORT), os.environ.get("PYTHONPATH", ""))
).rstrip(os.pathsep)

from isolate_settings import install, set_test_id

install()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    node_id = hashlib.sha256(item.nodeid.encode("utf-8")).hexdigest()
    set_test_id(node_id)
    os.environ["COMPONENTPRESS_TEST_SETTINGS_ID"] = node_id


@pytest.fixture(autouse=True)
def restore_language_and_theme(request):
    from componentpress.i18n import language, set_language

    log_directory = _SETTINGS_ROOT / hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest() / "logs"
    original_log_directory = os.environ.get("COMPONENTPRESS_LOG_DIR")
    os.environ["COMPONENTPRESS_LOG_DIR"] = str(log_directory)
    technical_log = sys.modules.get("componentpress.diagnostics.logging")
    if technical_log is not None:
        technical_log.shutdown()
    original_language = language()
    theme = sys.modules.get("componentpress.editor.theme")
    original_theme = getattr(theme, "_CURRENT_THEME", None)
    yield
    set_language(original_language)
    if theme is not None and original_theme is not None and theme._CURRENT_THEME != original_theme:
        widgets = sys.modules.get("PySide6.QtWidgets")
        application = widgets.QApplication.instance() if widgets else None
        if application is None:
            theme._CURRENT_THEME = original_theme
            theme._SYSTEM_WATCHING = False
        else:
            theme.apply_theme(original_theme)
    if technical_log is not None:
        technical_log.shutdown()
    if original_log_directory is None:
        os.environ.pop("COMPONENTPRESS_LOG_DIR", None)
    else:
        os.environ["COMPONENTPRESS_LOG_DIR"] = original_log_directory


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SETTINGS_ROOT, ignore_errors=True)
