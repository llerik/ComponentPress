from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


SINGLE_IMPORTS = (
    "from componentpress.project_io.yaml_codec import parse_component",
    "from componentpress.project_io.repository import FileProjectRepository",
    "from componentpress.bindings import BindingResolver",
    "from componentpress.rendering import render_component",
)
IMPORT_ORDERS = (
    (
        "from componentpress.project_io.yaml_codec import parse_component",
        "from componentpress.bindings import BindingResolver",
        "from componentpress.rendering import render_component",
    ),
    (
        "from componentpress.rendering import render_component",
        "from componentpress.bindings import BindingResolver",
        "from componentpress.project_io.yaml_codec import parse_component",
    ),
)


def _run_imports(statements: tuple[str, ...], working_directory: Path) -> None:
    script = "\n".join((*statements, """from pathlib import Path
from PySide6.QtWidgets import QApplication
assert QApplication.instance() is None
assert not list(Path.cwd().iterdir())
"""))
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=working_directory,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert not list(working_directory.iterdir())


@pytest.mark.parametrize("statement", SINGLE_IMPORTS)
def test_public_import_succeeds_in_a_fresh_process(statement: str, tmp_path: Path) -> None:
    _run_imports((statement,), tmp_path)


@pytest.mark.parametrize("statements", IMPORT_ORDERS)
def test_public_import_orders_succeed_in_a_fresh_process(
    statements: tuple[str, ...], tmp_path: Path
) -> None:
    _run_imports(statements, tmp_path)
