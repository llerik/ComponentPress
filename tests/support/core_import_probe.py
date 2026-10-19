"""Run with -I -B in a disposable cwd; no pytest/sitecustomize/Qt setup."""

import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from typing import get_type_hints

sys.path[:0] = [sys.argv[1], sys.argv[2], str(Path(__file__).parent)]
from refactoring_guard import forbidden_runtime

assert not [name for name in sys.modules if forbidden_runtime(name)], "unclean interpreter"
before_threads = threading.enumerate()


def audit(event, args):
    if event == "open":
        _, mode, flags = args
        write_flags = os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_EXCL | os.O_WRONLY | os.O_RDWR
        write_flags |= getattr(os, "O_TMPFILE", 0) | getattr(os, "O_TEMPORARY", 0)
        assert not (flags & write_flags), (event, args)
        assert not mode or not any(letter in mode for letter in "wax+"), (event, args)
    assert event not in {
        "os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink",
        "subprocess.Popen", "os.system", "os.chdir",
    }, (event, args)


def no_thread(*args, **kwargs):
    raise AssertionError("thread started during import")


sys.addaudithook(audit)
threading.Thread.start = no_thread
import _thread
_thread.start_new_thread = no_thread
for module in sys.argv[3:]:
    importlib.import_module(module)
if "componentpress.application.ports" in sys.argv[3:]:
    from componentpress.application.contracts import BuildInputSnapshot, SnapshotResource
    from componentpress.application.ports import BuildRunner, Cancellation

    assert get_type_hints(BuildRunner.start)["snapshot"] is BuildInputSnapshot
    assert tuple(inspect.signature(BuildInputSnapshot).parameters) == (
        "job_id", "root", "job_directory", "staging_directory", "project", "print_settings",
        "mode", "instances", "resources", "input_hashes", "font_families",
        "registered_font_ids", "max_render_workers", "memory_budget_bytes", "warnings",
    )
    assert tuple(SnapshotResource.__dataclass_fields__) == (
        "relative_path", "content_hash", "size", "data", "copied_path",
    )
    assert inspect.signature(SnapshotResource).parameters["data"].default is None
    assert inspect.signature(SnapshotResource).parameters["copied_path"].default is None
    assert not hasattr(SnapshotResource, "read")
    assert inspect.signature(BuildInputSnapshot).parameters["warnings"].default == ()
    assert isinstance(Cancellation.cancelled, property)
    snapshot = object.__new__(BuildInputSnapshot)
    object.__setattr__(snapshot, "instances", (SimpleNamespace(copies=2), SimpleNamespace(copies=3)))
    assert snapshot.total_copies == 5

    class FakeCancellation:
        cancelled = False

        def cancel(self):
            self.cancelled = True

        def check(self):
            assert not self.cancelled

    def is_cancelled(token: Cancellation) -> bool:
        token.check()
        return token.cancelled

    token = FakeCancellation()
    assert not is_cancelled(token)
    token.cancel()
    assert token.cancelled
    try:
        is_cancelled(token)
    except AssertionError:
        pass
    else:
        raise AssertionError("cancelled fake did not stop the operation")
if sys.argv[3:] == ["componentpress.domain.diagnostics"]:
    from componentpress.domain.diagnostics import Diagnostic, ProjectError

    diagnostic = Diagnostic("PROBE_ERROR", "private message")
    assert str(diagnostic) == "PROBE_ERROR"
    assert str(ProjectError(diagnostic)) == "PROBE_ERROR"
    assert not any(name == "componentpress.i18n" or name.startswith("componentpress.i18n.") for name in sys.modules)
if sys.argv[3:] == ["componentpress.application.preview_service"]:
    from pathlib import Path
    from componentpress.application.contracts import DocumentSnapshot, FileFingerprint, ProjectSnapshot
    from componentpress.application.preview_service import PreviewService
    from componentpress.domain.component import ComponentDefinition, DataBinding, SizeMM
    from componentpress.domain.project import ComponentRef, DataSource, ProjectDefinition
    from componentpress.domain.render_plan import ResolvedComponent
    from componentpress.domain.values import DataRow, DataSheetSnapshot

    root = Path("unwritten-project")
    component = ComponentDefinition(schema_version=8, id="card", name="Card", size_mm=SizeMM(width=40, height=30))
    project = ProjectDefinition(schema_version=8, id="game", name="Game", version="0.0.1",
                                components=(ComponentRef(id="card", path="components/card.yaml"),))
    document = DocumentSnapshot(root / "components/card.yaml", "", "", component)
    snapshot = ProjectSnapshot(root, "", "", project, {"card": document})

    class Store:
        def canonical_root(self, path):
            return path

        def source_path(self, path, relative):
            return path / relative

        def fingerprint(self, path):
            return FileFingerprint(1, 2)

        def store_validation(self, *args):
            self.stored = args
            return None

    class Resolver:
        def __init__(self):
            self.validate_resources = True

        def resolve_component(self, model, data, row, *, owner=None):
            return ResolvedComponent(model, model.id, row, None)

        def validate_columns(self, component, data, *, owner=None):
            return None

        def validate_static_resources(self, component, *, owner=None):
            return None

    resolver = Resolver()

    class Reader:
        def read(self, path, sheet, **kwargs):
            row = DataRow(2, "source-id", 1, {})
            return DataSheetSnapshot("main", path, sheet, "hash", kwargs["version"], (),
                                     (row,), kwargs["request_id"])

    store = Store()

    def resolver_factory(*args, validate_resources=True):
        resolver.validate_resources = validate_resources
        return resolver

    events = []
    service = PreviewService(Reader(), store, resolver_factory, lambda *args, **kwargs: None,
                             event_logger=lambda event, **context: events.append((event, context)))
    assert service.validate_all(snapshot) == ()
    assert events[0][0] == "preview.validation" and events[0][1]["status"] == "started"
    assert events[-1][1]["status"] == "succeeded"

    bound = component.model_copy(update={"data": DataBinding(sheet="Sheet")})
    source_project = project.model_copy(update={"data_source": DataSource(path="source.xlsx")})
    source_document = DocumentSnapshot(document.path, "", "", bound)
    source_snapshot = ProjectSnapshot(root, "", "", source_project, {"card": source_document})
    assert service.validate_all(source_snapshot) == ()
    cached = service.cached_data(source_snapshot, "card")
    assert cached is not None and len(cached.rows) == 1
    assert store.stored[3] == ["card"]
    assert all(not any(key in context for key in ("path", "root", "model")) for _, context in events)
    assert service.select_row(source_snapshot, "card", validate_resources=False).component is bound
    assert resolver.validate_resources is False
    service.invalidate(root)
if sys.argv[3:] == ["componentpress.application.project_service"]:
    from contextlib import nullcontext
    from pathlib import Path
    from componentpress.application.project_service import ProjectService

    calls = []

    class Projects:
        def create(self, root, name):
            calls.append(("create", root, name))
            return "snapshot"

    def event_logger(event, **context):
        calls.append((event, context))

    service = ProjectService(
        Projects(), object(), lambda _root: nullcontext(), object(),
        diagnostic_formatter=str, event_logger=event_logger,
        exception_logger=lambda *_args, **_kwargs: "incident",
    )
    assert service.create(Path("unwritten-project"), "Game") == "snapshot"
    assert [call[0] for call in calls] == [
        "project.operation", "create", "project.operation",
    ]
    assert calls[0][1]["status"] == "started"
    assert calls[-1][1]["status"] == "succeeded"
if sys.argv[3:] == ["componentpress.application.version_service"]:
    from componentpress.application.contracts import ArchiveExportResult
    from componentpress.application.version_service import VersionService

    class Archive:
        def export(self, snapshot, target, **options):
            return ArchiveExportResult(target, "0.1.0", True)

        def restore_path(self, archive_path, destination, **options):
            return destination

    class Projects:
        def open(self, root):
            return root

    service = VersionService(Archive(), Projects())
    assert service.export(object(), "archive.zip") == ArchiveExportResult("archive.zip", "0.1.0", True)
    assert service.restore("archive.zip", "restored") == "restored"
if all(module.startswith("componentpress.domain.") for module in sys.argv[3:]):
    assert not any(name.startswith("componentpress.application.") for name in sys.modules), "domain imported application"
assert threading.enumerate() == before_threads
assert not list(Path.cwd().iterdir()), "files created during import"
for name in ("PySide6.QtCore", "PySide6.QtWidgets"):
    qt = sys.modules.get(name)
    if qt is not None:
        application = getattr(qt, "QCoreApplication", None) or getattr(qt, "QApplication", None)
        assert application is None or application.instance() is None, "Qt application created"
print(json.dumps(sorted(name for name in sys.modules if forbidden_runtime(name))))
