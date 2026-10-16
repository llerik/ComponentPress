"""Stdlib-only inventory and ratchets used exclusively by refactoring tests."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys


ROOTS = ("src/componentpress", "packaging", "examples")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
ADAPTERS = (
    "bindings", "build", "data_sources", "diagnostics", "editor", "execution",
    "platforms", "project_io", "rendering", "resources", "versioning", "i18n",
)


def production_files(root: Path) -> list[Path]:
    # No name-based exemptions: smoke/test-named production files still count.
    return sorted(file for folder in ROOTS for file in (root / folder).rglob("*.py"))


def module_name(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def debt_owner(path: str, scope: str = "", target: str = "") -> dict[str, str]:
    """Assign every initial exception to an existing substage, not a catch-all."""
    if path.startswith("packaging/") or path.endswith("/gui.py"):
        stage = "9г"
    elif path.startswith("examples/"):
        stage = "9д"
    elif "/domain/diagnostics.py" in path or target == "componentpress.i18n":
        stage = "1б"
    elif path.endswith(("/ports.py", "/contracts.py")):
        stage = "1а"
    else:
        stage = _area_stage(path, scope, target)
    return {"stage": stage, "owner": "coder:" + path.rsplit("/", 1)[0]}


def _area_stage(path: str, scope: str, target: str) -> str:
    areas = {
        "preview_service.py": "2а", "preview_worker.py": "2б",
        "input_snapshot.py": "3а", "build_service.py": "3б", "engine.py": "3в",
        "png_archive.py": "3г", "version_service.py": "4б", "properties.py": "6а",
        "yaml_codec.py": "8а", "xlsx_reader.py": "8б", "resolver.py": "8в",
    }
    if path.endswith("/main_window.py"):
        return "5а" if "history" in scope or "component" in scope else "6б"
    if path.endswith("/project_service.py"):
        return "5в" if ".diagnostics" in target else "5а"
    if path.endswith("/repository.py") and ".application" in target:
        return "4а"
    if "/canvas/" in path or path.endswith("/tree.py"):
        return "7"
    if "/project_io/" in path or path.endswith("/cleanup.py"):
        return "9б"
    if "/diagnostics/" in path or path.endswith(("/cli.py", "/report.py")):
        return "9в"
    return areas.get(Path(path).name, "9а")


@dataclass(frozen=True)
class Import:
    path: str
    scope: str
    line: int
    target: str
    form: str

    @property
    def key(self) -> str:
        return "|".join((self.path, self.scope, self.form, self.target))


class Inventory(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.AST):
        self.path = path
        self.module = module_name(path)
        self.package = self.module if path.endswith("/__init__.py") else self.module.rpartition(".")[0]
        self.scope: list[str] = []
        self.functions: list[dict] = []
        self.imports: list[Import] = []
        self.calls: list[dict] = []
        self.aliases = _import_aliases(tree)
        self.visit(tree)

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self.functions.append({"path": self.path, "name": ".".join(self.scope),
                               "line": node.lineno, "length": node.end_lineno - node.lineno + 1})
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def add_import(self, node, target: str, form: str):
        self.imports.append(Import(self.path, ".".join(self.scope), node.lineno, target, form))

    def visit_Import(self, node):
        for alias in node.names:
            self.add_import(node, alias.name, "import")

    def visit_ImportFrom(self, node):
        target = "." * node.level + (node.module or "")
        if node.level:
            target = importlib.util.resolve_name(target, self.package)
        # Keep imported names: `from componentpress import rendering` is an edge too.
        for alias in node.names:
            self.add_import(node, target + "." + alias.name, "from")

    def visit_If(self, node):
        condition = _expression_name(node.test)
        root = condition.split(".")[0]
        expanded = self.aliases.get(root, root) + condition[len(root):]
        if expanded == "typing.TYPE_CHECKING":
            self.scope.append("<TYPE_CHECKING>")
            for statement in node.body:
                self.visit(statement)
            self.scope.pop()
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_Call(self, node):
        name = _expression_name(node.func)
        expanded = self.aliases.get(name.split(".")[0], name.split(".")[0])
        expanded += name[len(name.split(".")[0]):]
        self.calls.append({"path": self.path, "scope": ".".join(self.scope),
                           "line": node.lineno, "callee": expanded})
        if expanded in ("__import__", "builtins.__import__", "importlib.import_module"):
            self.add_import(node, _dynamic_target(node, self.package, expanded), "dynamic")
        self.generic_visit(node)


def _expression_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _expression_name(node.value) + "." + node.attr
    return ast.unparse(node)


def _import_aliases(tree) -> dict[str, str]:
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({n.asname or n.name.split(".")[0]: n.name if n.asname else n.name.split(".")[0] for n in node.names})
        elif isinstance(node, ast.ImportFrom) and not node.level:
            aliases.update({n.asname or n.name: (node.module or "") + "." + n.name for n in node.names})
    return aliases


def _dynamic_target(node, package: str, name: str) -> str:
    values = {keyword.arg: keyword.value for keyword in node.keywords}
    argument = node.args[0] if node.args else values.get("name")
    if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
        return "<unresolved>"
    target = argument.value
    if target.startswith("."):
        context = node.args[1] if len(node.args) > 1 else values.get("package")
        if name != "importlib.import_module" or not isinstance(context, ast.Constant):
            return "<unresolved>"
        try:
            target = importlib.util.resolve_name(target, context.value)
        except (ValueError, TypeError, ImportError):
            return "<unresolved>"
    return target


def forbidden(edge: Import) -> bool:
    source = module_name(edge.path)
    layer = source.split(".")[1] if source.startswith("componentpress.") else ""
    if layer not in ("domain", "application"):
        adapter = source.startswith("componentpress.") and layer in ADAPTERS
        return adapter and edge.target.startswith("componentpress.application.") and not edge.target.startswith(("componentpress.application.ports.", "componentpress.application.contracts."))
    if edge.form == "dynamic":
        return True
    top = edge.target.split(".")[0]
    if top in sys.stdlib_module_names or top == "__future__":
        return False
    if layer == "domain" and top == "pydantic":
        return False
    allowed = ("componentpress.domain.",) if layer == "domain" else ("componentpress.domain.", "componentpress.application.")
    return not edge.target.startswith(allowed)


def forbidden_runtime(module: str) -> bool:
    application_edges = ("componentpress.bootstrap", "componentpress.cli", "componentpress.gui", "componentpress.__main__")
    prefixes = ("PySide6", "openpyxl", "ruamel", "jinja2") + application_edges + tuple("componentpress." + name for name in ADAPTERS)
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def scan(root: Path) -> tuple[list[dict], list[Import], list[dict]]:
    functions, imports, calls = [], [], []
    for file in production_files(root):
        inventory = Inventory(file.relative_to(root).as_posix(), ast.parse(file.read_text(encoding="utf-8-sig")))
        functions.extend(inventory.functions)
        imports.extend(inventory.imports)
        calls.extend(inventory.calls)
    return functions, imports, calls


def import_graph(imports: list[Import], modules: set[str]) -> dict[str, set[str]]:
    graph = {module: set() for module in modules}
    for edge in imports:
        source, target = module_name(edge.path), edge.target
        if source not in graph:
            continue
        # Importing a leaf executes every parent __init__ too.
        parts = target.split(".")
        for end in range(1, len(parts) + 1):
            candidate = ".".join(parts[:end])
            if candidate in modules and candidate != source:
                graph[source].add(candidate)
    return graph


def cycle_edges(graph: dict[str, set[str]]) -> list[str]:
    """Each directed edge in a cycle: extending an existing SCC is still new debt."""
    result = []
    for source, targets in graph.items():
        for target in targets:
            pending, seen = [target], set()
            while pending:
                current = pending.pop()
                if current == source:
                    result.append(source + " -> " + target)
                    break
                if current not in seen:
                    seen.add(current)
                    pending.extend(graph[current])
    return sorted(result)


def current_debt(root: Path) -> dict:
    functions, imports, _ = scan(root)
    lengths = {f["path"] + "|" + f["name"]: {**f, **debt_owner(f["path"], f["name"])} for f in functions if f["length"] > 20}
    counts = Counter(edge.key for edge in imports if forbidden(edge))
    dependencies = {}
    for edge in imports:
        if edge.key in counts:
            dependencies[edge.key] = {"count": counts[edge.key], "line": edge.line, **debt_owner(edge.path, edge.scope, edge.target)}
    modules = {module_name(file.relative_to(root).as_posix()) for file in production_files(root) if file.is_relative_to(root / "src")}
    paths = {module_name(file.relative_to(root).as_posix()): file.relative_to(root).as_posix()
             for file in production_files(root) if file.is_relative_to(root / "src")}
    cycles = {edge: debt_owner(paths[edge.split(" -> ")[0]], target=edge.split(" -> ")[1])
              for edge in cycle_edges(import_graph(imports, modules))}
    return {"functions": lengths, "imports": dependencies, "cycles": cycles}


def ratchet_errors(actual: dict, baseline: dict, metric: str | None = None) -> list[str]:
    errors = ["new: " + key for key in sorted(actual.keys() - baseline.keys())]
    errors += ["stale exception: " + key for key in sorted(baseline.keys() - actual.keys())]
    if metric:
        errors += [f"increased {metric}: {key}: {actual[key][metric]} > {baseline[key][metric]}"
                   for key in actual.keys() & baseline.keys() if actual[key][metric] > baseline[key][metric]]
    return errors
