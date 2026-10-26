"""Run the fixed Ruff pilot and reject any change to its diagnostic inventory."""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.16.8"
PILOT = ("src/componentpress/domain", "src/componentpress/application")
BASELINE = ROOT / "tests/analyzers/ruff/baseline.json"


def pilot_files(root: Path = ROOT) -> list[Path]:
    return sorted(path for folder in PILOT for path in (root / folder).rglob("*.py"))


def localized(key: str, **values: str) -> str:
    from componentpress.i18n import tr

    return tr(key, **values)


def run(command: list[str], root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60)


def command_for(executable: str, files: list[Path], root: Path = ROOT) -> list[str]:
    return [executable, "check", "--config", str(root / "pyproject.toml"), "--target-version", "py314",
            "--select", "E4,E7,E9,F", "--ignore-noqa", "--no-respect-gitignore", "--no-force-exclude",
            "--no-cache", "--output-format", "json", *map(str, files)]


def diagnostic_rows(raw: str, result_code: int, root: Path = ROOT) -> list[dict]:
    records = json.loads(raw)
    if not isinstance(records, list) or result_code != (1 if records else 0):
        raise ValueError("Ruff report and exit code disagree")
    if any(not isinstance(row, dict) or not all(key in row for key in ("filename", "code", "message", "location"))
           for row in records):
        raise ValueError("Ruff report has an invalid diagnostic")
    rows = [identity(row, root) for row in records]
    allowed = {p.relative_to(root).as_posix() for p in pilot_files(root)}
    if any(row["path"] not in allowed for row in rows):
        raise ValueError("Ruff report contains a file outside the pilot")
    return rows


def identity(row: dict, root: Path = ROOT) -> dict:
    path = Path(row["filename"]).resolve()
    relative = path.relative_to(root.resolve()).as_posix()
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"rule": row["code"], "path": relative, "context": source.splitlines()[row["location"]["row"] - 1].strip(),
                "scope": "", "message": row["message"], "line": row["location"]["row"]}
    scope, context = syntax_context(tree, row["location"]["row"])
    return {"rule": row["code"], "path": relative, "scope": scope, "context": context,
            "message": row["message"], "line": row["location"]["row"]}


def syntax_context(tree: ast.AST, line: int) -> tuple[str, str]:
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.stmt)
                  and node.lineno <= line <= node.end_lineno]
    statement = min(statements, key=lambda node: node.end_lineno - node.lineno) if statements else tree
    scope, branches = [], []
    node, child = statement, statement
    while node in parents:
        child, node = node, parents[node]
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.append(node.name)
        branch = branch_context(node, child)
        if branch:
            branches.append(branch)
    context = "|".join((*reversed(branches), ast.dump(statement, include_attributes=False)))
    return ".".join(reversed(scope)), context


def branch_context(node: ast.AST, child: ast.AST) -> str | None:
    if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        slot = "else" if child in node.orelse else "body"
        test = node.iter if isinstance(node, (ast.For, ast.AsyncFor)) else node.test
        return f"{type(node).__name__}:{slot}:{ast.dump(test)}"
    if isinstance(node, (ast.Try, ast.TryStar)):
        return next((name for name in ("body", "orelse", "finalbody") if child in getattr(node, name)))
    if isinstance(node, ast.ExceptHandler):
        return f"except:{ast.dump(node.type) if node.type else 'bare'}"
    if isinstance(node, ast.Match):
        return f"match:{ast.dump(node.subject)}"
    if isinstance(node, ast.match_case):
        return f"case:{ast.dump(node.pattern)}:{ast.dump(node.guard) if node.guard else ''}"
    return None


def validate_coverage(executable: str, files: list[Path], root: Path = ROOT) -> None:
    result = run([executable, "check", "--show-files", "--config", str(root / "pyproject.toml"),
                  "--no-respect-gitignore", "--no-force-exclude", *map(str, files)], root)
    shown = {str(Path(line).resolve()) for line in result.stdout.splitlines() if line.strip()}
    if result.returncode or shown != {str(path.resolve()) for path in files}:
        raise ValueError("Ruff file discovery differs from the pilot file list")


def inventory(executable: str, root: Path = ROOT) -> tuple[list[dict], str]:
    files = pilot_files(root)
    if not files:
        raise ValueError("Ruff pilot file list is empty")
    validate_coverage(executable, files, root)
    result = run(command_for(executable, files, root), root)
    raw = result.stdout
    (root / ".ruff_cache").mkdir(exist_ok=True)
    (root / ".ruff_cache/ruff-pilot-current.json").write_text(raw, encoding="utf-8")
    (root / ".ruff_cache/ruff-pilot-stderr.txt").write_text(result.stderr, encoding="utf-8")
    (root / ".ruff_cache/ruff-pilot-status.txt").write_text(str(result.returncode), encoding="ascii")
    if result.stderr or result.returncode not in (0, 1):
        raise ValueError("Ruff failed to produce a complete report")
    return diagnostic_rows(raw, result.returncode, root), raw


def report(items: list[dict]) -> Counter:
    return Counter((row["rule"], row["path"], row["scope"], row["context"], row["message"]) for row in items)


def compare(current: list[dict], approved: list[dict]) -> list[str]:
    actual, baseline = report(current), report(approved)
    new = ["new" for key, count in (actual - baseline).items() for _ in range(count)]
    stale = ["stale" for key, count in (baseline - actual).items() for _ in range(count)]
    return new + stale


def config_hash(root: Path = ROOT) -> str:
    return hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()


def load_baseline(root: Path = ROOT) -> dict:
    baseline = json.loads((root / "tests/analyzers/ruff/baseline.json").read_text(encoding="utf-8"))
    if baseline["version"] != VERSION or baseline["config_sha256"] != config_hash(root):
        raise ValueError("Ruff version or configuration differs from the approved baseline")
    return baseline


def source_metadata(root: Path = ROOT) -> dict:
    revision = run(["git", "rev-parse", "HEAD"], root)
    if revision.returncode:
        raise ValueError("Could not record the pilot source revision")
    source = revision.stdout.strip()
    status = run(["git", "status", "--porcelain=v1"], root)
    if status.returncode:
        raise ValueError("Could not record the pilot working tree state")
    dirty = status.stdout.splitlines()
    return {"source_revision": source, "dirty_paths": [line for line in dirty if not line.startswith("??")],
            "untracked_count": sum(line.startswith("??") for line in dirty), "untracked_paths_redacted": True}


def bootstrap(items: list[dict], raw: str, root: Path = ROOT) -> None:
    if BASELINE.exists():
        raise FileExistsError("Ruff baseline already exists")
    for row in items:
        row["stage"], row["owner"] = "27", "coder:" + row["path"].rsplit("/", 1)[0]
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    baseline = {**source_metadata(root), "exit_code": 1 if items else 0, "version": VERSION,
                "python": sys.version.split()[0], "command": "ruff check --config pyproject.toml --target-version py314 --select E4,E7,E9,F --ignore-noqa --no-respect-gitignore --no-force-exclude --no-cache --output-format json",
                "config_sha256": config_hash(root), "files": [p.relative_to(root).as_posix() for p in pilot_files(root)],
                "diagnostics": items}
    BASELINE.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "tests/analyzers/ruff/pilot-output.json").write_text(raw, encoding="utf-8")
    (root / "tests/analyzers/ruff/pilot-stderr.txt").write_text("", encoding="utf-8")


def check(args: SimpleNamespace) -> int:
    executable = ruff_executable()
    version = run([executable, "--version"])
    if version.returncode or version.stdout.strip() != f"ruff {VERSION}":
        raise ValueError("Ruff is missing or its version differs from the lock")
    current, raw = inventory(executable)
    if args.bootstrap:
        bootstrap(current, raw)
        print(localized("dev.ruff_baseline_created", locale=args.language, count=str(len(current))))
        return 0
    approved = load_baseline()["diagnostics"]
    errors = compare(current, approved)
    if errors:
        print(localized("dev.ruff_baseline_changed", locale=args.language, new=str(errors.count("new")),
                        stale=str(errors.count("stale"))), file=sys.stderr)
        return 1
    print(localized("dev.ruff_check_passed", locale=args.language, count=str(len(current))))
    return 0


def ruff_executable() -> str:
    name = "ruff.exe" if os.name == "nt" else "ruff"
    local = Path(sys.executable).with_name(name)
    return str(local) if local.is_file() else shutil.which("ruff") or str(local)


def options(arguments: list[str]) -> SimpleNamespace:
    result = SimpleNamespace(bootstrap=False, language="ru", help=False)
    while arguments:
        option = arguments.pop(0)
        if option == "--bootstrap" and not result.bootstrap:
            result.bootstrap = True
        elif option == "--language" and arguments and arguments[0] in ("ru", "en"):
            result.language = arguments.pop(0)
        elif option == "--help":
            result.help = True
        else:
            raise ValueError("arguments")
    return result


def main() -> int:
    args = options(sys.argv[1:])
    if args.help:
        print(localized("dev.ruff_help", locale=args.language))
        return 0
    return check(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(localized("dev.ruff_check_failed", error=type(error).__name__), file=sys.stderr)
        raise SystemExit(2)
