"""First render in a fresh process must not initialize shared PySide converters in workers."""

import os
import subprocess
import sys


def test_painter_initializes_gradient_preset_before_render_threads():
    source = """
from PySide6 import QtGui
assert 'QGradient' not in vars(QtGui), 'probe_must_start_without_gradient'
import componentpress.rendering.painter
gradient = vars(QtGui).get('QGradient')
assert gradient is not None, 'gradient_must_be_initialized_before_workers'
assert 'Preset' in vars(gradient), 'preset_converter_must_be_initialized_before_workers'
assert QtGui.QGuiApplication.instance() is None
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", source], capture_output=True,
                            text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=20)
    assert result.returncode == 0, result.stderr
