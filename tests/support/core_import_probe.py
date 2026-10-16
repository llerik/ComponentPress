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
