"""The diagnostic sink must preserve operation behavior and survive sink failures."""

from pathlib import Path
import os
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import render_crash_probe as probe


def test_trace_is_opt_in_and_wrap_preserves_results_and_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "TRACE_DIRECTORY", None)
    probe.trace("disabled")
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(probe, "TRACE_DIRECTORY", tmp_path)

    class Operation:
        def run(self, value):
            if value < 0:
                raise ValueError("private-project-text")
            return value * 2

    probe.wrap(Operation, "run", "operation")
    assert Operation().run(21) == 42
    with pytest.raises(ValueError, match="private-project-text"):
        Operation().run(-1)
    lines = next(tmp_path.glob("trace-*.tsv")).read_text().splitlines()
    assert [line.split("\t")[1] for line in lines] == [
        "operation.before", "operation.after", "operation.before", "operation.error"]
    assert all(line.split("\t")[0].isdigit() for line in lines)
    assert "private-project-text" not in "\n".join(lines)


def test_unavailable_trace_sink_does_not_change_operation(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "TRACE_DIRECTORY", tmp_path / "missing" / "directory")
    probe.trace("unavailable")
    assert not list(tmp_path.iterdir())


def test_qt_wrappers_preserve_real_pixels_and_write_boundaries(tmp_path):
    source = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import render_crash_probe as probe
probe.TRACE_DIRECTORY = Path(sys.argv[2])
probe.install_trace()
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QColor
image = QImage(10, 10, QImage.Format.Format_ARGB32_Premultiplied)
image.fill(0)
painter = QPainter(image)
painter.setClipRect(QRectF(0, 0, 5, 5), Qt.ClipOperation.ReplaceClip)
painter.fillRect(QRectF(0, 0, 10, 10), QColor('#ff0000'))
painter.end()
assert image.pixelColor(1, 1) == QColor('#ff0000')
assert image.pixelColor(8, 8).alpha() == 0
assert image.save(str(probe.TRACE_DIRECTORY / 'image.png'), 'PNG')
"""
    result = subprocess.run([sys.executable, "-B", "-c", source, str(Path(probe.__file__).parent), str(tmp_path)],
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    text = next(tmp_path.glob("trace-*.tsv")).read_text()
    assert "painter.setClipRect.before" in text and "painter.setClipRect.after" in text
    assert "image.save.before" in text and "image.save.after" in text
