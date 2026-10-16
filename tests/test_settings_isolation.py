from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QSettings


def test_qsettings_constructors_are_confined_in_parent_and_subprocess(tmp_path: Path) -> None:
    root = Path(os.environ["COMPONENTPRESS_TEST_SETTINGS_ROOT"]).resolve()
    parent = QSettings("ComponentPressIsolation", "Parent")
    system = QSettings(
        QSettings.Format.IniFormat,
        QSettings.Scope.SystemScope,
        "ComponentPressIsolation",
        "System",
    )
    assert parent.format() == QSettings.Format.IniFormat
    assert system.format() == QSettings.Format.IniFormat
    assert Path(parent.fileName()).resolve().is_relative_to(root)
    assert Path(system.fileName()).resolve().is_relative_to(root)
    parent.setValue("sentinel", str(tmp_path))
    system.setValue("sentinel", str(tmp_path))
    parent.sync()
    system.sync()
    assert parent.status() == QSettings.Status.NoError
    assert system.status() == QSettings.Status.NoError

    code = """
import json, sys
from pathlib import Path
from PySide6.QtCore import QSettings
user = QSettings('ComponentPressIsolation', 'Child')
system = QSettings(QSettings.Format.NativeFormat, QSettings.Scope.SystemScope,
                   'ComponentPressIsolation', 'ChildSystem')
print(json.dumps({
    'user_format': user.format().name,
    'user_path': user.fileName(),
    'system_format': system.format().name,
    'system_path': system.fileName(),
    'application_modules': sorted(name for name in sys.modules
                                  if name.startswith('componentpress.')),
}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    child = json.loads(result.stdout)
    assert child["user_format"] == child["system_format"] == "IniFormat"
    assert Path(child["user_path"]).resolve().is_relative_to(root)
    assert Path(child["system_path"]).resolve().is_relative_to(root)
    assert child["application_modules"] == []
