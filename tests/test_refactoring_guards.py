from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from refactoring_guard import (
    Inventory, Import, current_debt, cycle_edges, forbidden, import_graph,
    module_name, production_files, ratchet_errors,
)

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "tests/refactoring_baseline.json").read_text(encoding="utf-8"))
LEAVES = sorted(module_name(file.relative_to(ROOT).as_posix())
                for folder in ("domain", "application")
                for file in (ROOT / "src/componentpress" / folder).rglob("*.py")
                if file.name != "__init__.py")


@pytest.mark.parametrize("kind,metric", [("functions", "length"), ("imports", "count"), ("cycles", None)])
def test_production_debt_does_not_grow(kind, metric):
    actual = current_debt(ROOT)[kind]
    errors = ratchet_errors(actual, BASELINE[kind], metric)
    assert not errors, "\n".join(errors)
    assert all(entry["stage"] and entry["owner"] for entry in BASELINE[kind].values())


@pytest.mark.parametrize("length", [20, 21])
@pytest.mark.parametrize("header", ["def rule():", "async def rule():", "def rule(\n    value=None,\n):"])
def test_physical_length_boundary_includes_signature_docstring_and_blanks(length, header):
    lines = header.splitlines() + ['    """A docstring."""', "    # a comment", ""]
    lines.extend("    pass" for _ in range(length - len(lines) - 1))
    lines.append("    return None")
    source = "@decorator\n" + "\n".join(lines) + "\n"
    function = Inventory("src/componentpress/domain/rule.py", ast.parse(source)).functions[0]
    assert function["line"] == 2
    assert function["length"] == length
    assert (function["length"] > 20) == (length == 21)


def test_nested_methods_and_async_are_all_inventoried():
    source = "class Rules:\n    def outer(self):\n        async def inner():\n            pass\n        return inner\n"
    functions = Inventory("src/componentpress/domain/rule.py", ast.parse(source)).functions
    assert [(f["name"], f["length"]) for f in functions] == [("Rules.outer", 4), ("Rules.outer.inner", 2)]


def test_only_explicit_production_roots_are_scanned(tmp_path):
    included = ("src/componentpress/test_smoke.py", "packaging/release_test.py", "examples/tests/generator.py")
    excluded = ("tests/test_rule.py", "tests/support/helper.py", ".venv/test.py", "build/test.py", "dist/test.py", "releases/1/test.py")
    for path in (*included, *excluded):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("def long():\n" + "    pass\n" * 21, encoding="utf-8")
    assert {f.relative_to(tmp_path).as_posix() for f in production_files(tmp_path)} == set(included)
    assert len(current_debt(tmp_path)["functions"]) == 3


@pytest.mark.parametrize("source", [
    "import PySide6.QtCore",
    "from componentpress import rendering",
    "from ..rendering import exporter",
    "from .. import rendering",
    "def rule():\n    from componentpress.build import engine",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from componentpress.build import input_snapshot",
    "import importlib as loader\nloader.import_module('componentpress.rendering')",
    "from importlib import import_module as load\nload('..rendering', 'componentpress.application')",
    "__import__('componentpress.rendering')",
    "import builtins as b\nb.__import__('componentpress.rendering')",
    "import importlib\nimportlib.import_module(name)",
])
def test_forbidden_import_forms_are_detected(source):
    inventory = Inventory("src/componentpress/application/rule.py", ast.parse(source))
    assert any(forbidden(edge) for edge in inventory.imports)


@pytest.mark.parametrize("source", ["from .nodes import Node", "import pathlib", "from pydantic import BaseModel"])
def test_domain_allowed_dependencies(source):
    inventory = Inventory("src/componentpress/domain/rule.py", ast.parse(source))
    assert not any(forbidden(edge) for edge in inventory.imports)


def test_file_adapter_cannot_import_service_implementation():
    edge = Import("src/componentpress/project_io/files.py", "write", 3, "componentpress.application.version_service.recover", "from")
    assert forbidden(edge)
    build_adapter = Import("src/componentpress/build/input_snapshot.py", "create", 4, "componentpress.application.identifiers.instance_id", "from")
    assert forbidden(build_adapter)


def test_static_guard_covers_all_import_escape_hatches():
    source = "\n".join((
        "import importlib as loader",
        "if TYPE_CHECKING:",
        "    from componentpress.build import BuildInputSnapshot",
        "def run():",
        "    from ..rendering import exporter",
        "    loader.import_module('componentpress.rendering')",
    ))
    inventory = Inventory("src/componentpress/application/rule.py", ast.parse(source))
    assert {edge.form for edge in inventory.imports} == {"import", "from", "dynamic"}
    assert sum(forbidden(edge) for edge in inventory.imports) >= 2
    assert any(edge.form == "dynamic" and forbidden(edge) for edge in inventory.imports)


def test_import_exception_identity_includes_type_checking_context():
    runtime = Inventory("src/componentpress/application/rule.py", ast.parse(
        "from componentpress.build import BuildInputSnapshot\n"
    )).imports[0]
    type_only = Inventory("src/componentpress/application/rule.py", ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from componentpress.build import BuildInputSnapshot\n"
    )).imports[1]
    assert runtime.key != type_only.key


def test_ratchet_rejects_new_moved_increased_and_stale_debt():
    old = {"module.py|rule": {"length": 21}}
    assert ratchet_errors(old, old, "length") == []
    assert ratchet_errors({"module.py|rule": {"length": 22}}, old, "length")
    assert ratchet_errors({"other.py|rule": {"length": 21}}, old, "length")
    assert ratchet_errors({}, old, "length")
    assert ratchet_errors({"edge": {"count": 2}}, {"edge": {"count": 1}}, "count")
    # Changing only a diagnostic line must not invalidate the exception.
    assert not ratchet_errors({"edge": {"count": 1, "line": 9}}, {"edge": {"count": 1, "line": 1}}, "count")


def test_new_function_cannot_borrow_an_existing_exception(tmp_path):
    file = tmp_path / "src/componentpress/domain/rule.py"
    file.parent.mkdir(parents=True)
    file.write_text("def old():\n" + "    pass\n" * 20, encoding="utf-8")
    old = current_debt(tmp_path)["functions"]
    file.write_text(file.read_text(encoding="utf-8") + "\ndef new():\n" + "    pass\n" * 20, encoding="utf-8")
    assert ratchet_errors(current_debt(tmp_path)["functions"], old, "length") == ["new: src/componentpress/domain/rule.py|new"]


def test_new_edge_inside_existing_cycle_is_rejected():
    graph = {"a": {"b"}, "b": {"a", "c"}, "c": {"b"}}
    previous = dict.fromkeys(cycle_edges(graph))
    graph["a"].add("c")
    assert ratchet_errors(dict.fromkeys(cycle_edges(graph)), previous) == ["new: a -> c"]


def test_package_reexports_participate_in_import_graph():
    edges = [Import("src/componentpress/domain/rule.py", "", 1, "componentpress.application.contracts.Value", "from")]
    graph = import_graph(edges, {"componentpress.domain.rule", "componentpress.application", "componentpress.application.contracts"})
    assert graph["componentpress.domain.rule"] == {"componentpress.application", "componentpress.application.contracts"}


def _probe(modules: list[str], cwd: Path, source: Path | None = None, site_packages: Path | None = None):
    # -I -S excludes PYTHONPATH/sitecustomize; add dependencies without processing .pth files.
    packages = site_packages or Path(sys.prefix) / "Lib" / "site-packages"
    command = [sys.executable, "-I", "-S", "-B", str(ROOT / "tests/support/core_import_probe.py"),
               str(source or ROOT / "src"), str(packages), *modules]
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_runtime_inventory_covers_every_current_leaf():
    assert set(LEAVES) == BASELINE["runtime"].keys()


def test_application_build_contracts_resolve_without_adapters(tmp_path):
    loaded = _probe(["componentpress.application.ports"], tmp_path)
    assert loaded == BASELINE["runtime"]["componentpress.application.ports"]["modules"]


@pytest.mark.parametrize("module", LEAVES)
def test_core_leaf_import_has_no_new_runtime_dependencies(module, tmp_path):
    loaded = _probe([module], tmp_path)
    assert loaded == BASELINE["runtime"][module]["modules"]


@pytest.mark.parametrize("reverse", [False, True])
def test_core_import_orders_have_no_cycles_or_effects(reverse, tmp_path):
    leaves = list(reversed(LEAVES)) if reverse else LEAVES
    loaded = _probe(leaves, tmp_path)
    assert loaded == sorted({name for leaf in leaves for name in BASELINE["runtime"][leaf]["modules"]})


@pytest.mark.parametrize("body", [
    "open('written.txt', 'w').write('bad')",
    "from pathlib import Path\nPath('directory').mkdir()",
    "import threading\nthreading.Thread(target=lambda: None).start()",
    "import _thread\n_thread.start_new_thread(lambda: None, ())",
    "import os\nfd=os.open('sentinel', os.O_CREAT | os.O_RDONLY, 0o600)\nos.close(fd)",
])
def test_import_probe_rejects_file_and_thread_side_effects(body, tmp_path):
    source, cwd = tmp_path / "source", tmp_path / "cwd"
    source.mkdir()
    cwd.mkdir()
    (source / "bad_import.py").write_text(body, encoding="utf-8")
    with pytest.raises(AssertionError):
        _probe(["bad_import"], cwd, source)
    assert not list(cwd.iterdir())


def test_runtime_probe_starts_clean_despite_parent_pythonpath(tmp_path, monkeypatch):
    poison = tmp_path / "poison"
    poison.mkdir()
    (tmp_path / "cwd").mkdir()
    (poison / "sitecustomize.py").write_text("raise RuntimeError('sitecustomize ran')", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(poison))
    monkeypatch.chdir(tmp_path.parent)
    assert _probe(["componentpress.domain.schema"], tmp_path / "cwd", site_packages=poison) == []
