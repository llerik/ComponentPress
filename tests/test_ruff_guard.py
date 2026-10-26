from __future__ import annotations

import json
import ast
from pathlib import Path
import subprocess
import sys

import pytest

from refactoring_guard import Inventory
from ruff_guard import ROOT, VERSION, check, compare, identity, pilot_files, validate_coverage


def diagnostic(path: Path, line: int) -> dict:
    return {"filename": str(path), "code": "F821", "message": "undefined name", "location": {"row": line}}


def test_violation_identity_includes_expression_and_branch(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("def run(flag):\n    if flag:\n        missing()\n    else:\n        missing()\n", encoding="utf-8")
    first, second = identity(diagnostic(source, 3), tmp_path), identity(diagnostic(source, 5), tmp_path)
    assert first["path"] == second["path"] and first["scope"] == second["scope"]
    assert first["context"] != second["context"]


def test_blank_lines_do_not_change_violation_identity(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("def run():\n    missing()\n", encoding="utf-8")
    original = identity(diagnostic(source, 2), tmp_path)
    source.write_text("def run():\n\n    missing()\n", encoding="utf-8")
    moved = identity(diagnostic(source, 3), tmp_path)
    assert compare([original], [moved]) == []


def test_violation_moved_between_try_sections_is_new_debt(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("def run():\n    try:\n        missing()\n    finally:\n        missing()\n", encoding="utf-8")
    body, final = identity(diagnostic(source, 3), tmp_path), identity(diagnostic(source, 5), tmp_path)
    assert body["scope"] == final["scope"] and body["context"] != final["context"]


def test_ratchet_rejects_replacement_repeats_and_stale_entries():
    approved = [{"rule": "F821", "path": "a.py", "scope": "run", "context": "if/body", "message": "undefined"}]
    assert compare(approved, approved) == []
    assert compare([{**approved[0], "context": "else/body"}], approved) == ["new", "stale"]
    assert compare([{**approved[0], "path": "b.py"}], approved) == ["new", "stale"]
    assert compare(approved * 2, approved) == ["new"]
    assert compare([], approved) == ["stale"]


@pytest.mark.parametrize("raw,code", [("{", 1), ("[]", 1), ("[]", 2), ('[{"code":"F401"}]', 1)])
def test_report_parser_fails_closed(raw, code):
    from ruff_guard import diagnostic_rows

    with pytest.raises((ValueError, KeyError, TypeError, json.JSONDecodeError)):
        diagnostic_rows(raw, code)


def test_pilot_baseline_runs_and_reports_in_both_languages(capsys):
    from argparse import Namespace

    assert check(Namespace(bootstrap=False, language="ru")) == 0
    assert "Проверка пилота Ruff" in capsys.readouterr().out
    assert check(Namespace(bootstrap=False, language="en")) == 0
    assert "Ruff pilot passed" in capsys.readouterr().out


def test_guard_functions_follow_the_architecture_length_limit():
    path = ROOT / "tests/support/ruff_guard.py"
    functions = Inventory(path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"))).functions
    assert not [function for function in functions if function["length"] > 20]


def test_guard_import_has_no_side_effects(tmp_path):
    from test_refactoring_guards import _probe

    assert _probe(["ruff_guard"], tmp_path, ROOT / "src") == []


def test_checker_code_stays_clean_under_the_pilot_profile():
    executable = Path(sys.executable).with_name("ruff.exe" if sys.platform == "win32" else "ruff")
    files = [ROOT / "tests/support/ruff_guard.py", Path(__file__).resolve()]
    result = subprocess.run([str(executable), "check", "--config", str(ROOT / "pyproject.toml"),
                             "--ignore-noqa", "--no-cache", *map(str, files)], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_pilot_file_discovery_covers_all_current_sources():
    files = pilot_files()
    assert len(files) == 23
    executable = Path(sys.executable).with_name("ruff.exe" if sys.platform == "win32" else "ruff")
    validate_coverage(str(executable), files)


def test_changed_ruff_version_or_configuration_blocks_baseline(tmp_path):
    from ruff_guard import load_baseline

    baseline = tmp_path / "tests/analyzers/ruff/baseline.json"
    baseline.parent.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("changed = true", encoding="utf-8")
    baseline.write_text(json.dumps({"version": VERSION, "config_sha256": "old"}), encoding="utf-8")
    with pytest.raises(ValueError, match="version or configuration"):
        load_baseline(tmp_path)


def test_wrong_or_missing_ruff_binary_is_a_hard_failure(monkeypatch):
    import ruff_guard
    from types import SimpleNamespace

    monkeypatch.setattr(ruff_guard, "ruff_executable", lambda: "missing-ruff")
    monkeypatch.setattr(ruff_guard, "run", lambda *_args: SimpleNamespace(returncode=127, stdout="", stderr="missing"))
    with pytest.raises(ValueError, match="missing or its version"):
        check(SimpleNamespace(bootstrap=False, language="en"))


def test_analyzer_timeout_is_a_hard_failure(monkeypatch):
    import ruff_guard
    from subprocess import TimeoutExpired
    from types import SimpleNamespace

    def timeout(*_args):
        raise TimeoutExpired("ruff", 60)

    monkeypatch.setattr(ruff_guard, "run", timeout)
    with pytest.raises(TimeoutExpired):
        check(SimpleNamespace(bootstrap=False, language="en"))


def test_noqa_cannot_hide_a_new_pilot_diagnostic(tmp_path):
    import json
    from ruff_guard import command_for

    source = tmp_path / "hidden.py"
    source.write_text("import os  # noqa: F401\n", encoding="utf-8")
    executable = Path(sys.executable).with_name("ruff.exe" if sys.platform == "win32" else "ruff")
    result = subprocess.run(command_for(str(executable), [source]), cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", check=False)
    assert result.returncode == 1 and json.loads(result.stdout)[0]["code"] == "F401"
