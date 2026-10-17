"""Opt-in render breadcrumbs and one isolated reproduction; no project data in traces."""

from __future__ import annotations

import faulthandler
import gc
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time


TRACE_DIRECTORY: Path | None = None
ROOT = Path(__file__).resolve().parents[2]
NODE = "tests/test_stage9_modes.py::test_test_build_selects_test_rows_reports_mode_and_zip_manifest"


def trace(event: str) -> None:
    if TRACE_DIRECTORY is None:
        return
    try:
        path = TRACE_DIRECTORY / f"trace-{os.getpid()}-{threading.get_native_id()}.tsv"
        with path.open("ab", buffering=0) as stream:
            stream.write(f"{time.monotonic_ns()}\t{event}\n".encode("ascii"))
    except OSError:
        pass


def wrap(owner, name: str, label: str) -> None:
    original = getattr(owner, name)

    def traced(*args, **kwargs):
        trace(f"{label}.before")
        try:
            result = original(*args, **kwargs)
        except BaseException:
            trace(f"{label}.error")
            raise
        trace(f"{label}.after")
        return result

    setattr(owner, name, traced)


def trace_task(original):
    def run(self):
        trace(f"task.{self.instance.index}.before")
        try:
            return original(self)
        finally:
            trace(f"task.{self.instance.index}.after")
    return run


def install_trace() -> None:
    from PySide6.QtGui import QImage, QPainter
    from componentpress.execution import tasks
    from componentpress.rendering import painter

    for method in ("__init__", "end", "setClipRect", "fillRect", "drawImage", "drawRect", "drawEllipse", "drawLine"):
        wrap(QPainter, method, f"painter.{method}")
    for method in ("__init__", "fill", "save"):
        wrap(QImage, method, f"image.{method}")
    wrap(tasks, "paint_component", "component")
    wrap(painter, "prepare_html", "html.prepare")
    from componentpress.rendering.html_document import PreparedHtml
    wrap(PreparedHtml, "draw", "html.draw")
    tasks.RenderInstanceTask.run = trace_task(tasks.RenderInstanceTask.run)


def gc_trace(phase, _info) -> None:
    trace(f"gc.{phase}")


def set_workers(workers: int) -> None:
    from componentpress.application.contracts import BuildRequest
    original = BuildRequest.__init__

    def initialize(self, *args, **kwargs):
        kwargs["max_render_workers"] = workers
        original(self, *args, **kwargs)

    BuildRequest.__init__ = initialize


def warm_enums() -> None:
    from PySide6.QtGui import QGradient
    tuple(QGradient.Preset)
    trace("enums.gradient_preset.warmed")


def execute(run_directory: Path) -> int:
    if os.environ.get("COMPONENTPRESS_PROBE_DIRECT") == "1":
        import runpy
        import isolate_settings
        isolate_settings.install()
        namespace = runpy.run_path(str(ROOT / NODE.split("::")[0]))
        temporary = run_directory / "direct"
        temporary.mkdir()
        namespace[NODE.split("::")[1]](temporary)
        return 0
    import pytest

    class CaptureDiagnostics:
        @pytest.hookimpl(tryfirst=True)
        def pytest_configure(self, config):
            actual = os.environ["COMPONENTPRESS_TEST_SETTINGS_ROOT"]
            (run_directory / "test-paths.json").write_text(json.dumps({"settings_root": actual}), encoding="utf-8")

        @pytest.hookimpl(tryfirst=True)
        def pytest_sessionfinish(self, session, exitstatus):
            try:
                shutil.copytree(os.environ["COMPONENTPRESS_TEST_SETTINGS_ROOT"], run_directory / "test-settings")
            except OSError as error:
                (run_directory / "capture-error.json").write_text(json.dumps({"type": type(error).__name__}))

    selected = os.environ.get("COMPONENTPRESS_PROBE_NODE", NODE)
    nodes = [] if selected == "all" else selected.split()
    return int(pytest.main(["-v", "-rs", "--durations=10", "-p", "no:cacheprovider",
                            f"--basetemp={run_directory / 'pytest'}", *nodes], plugins=[CaptureDiagnostics()]))


def main() -> int:
    global TRACE_DIRECTORY
    run_directory = Path(sys.argv[1]).resolve()
    worker_setting = os.environ.get("COMPONENTPRESS_PROBE_WORKERS")
    workers = int(worker_setting) if worker_setting else None
    gc_mode = os.environ.get("COMPONENTPRESS_PROBE_GC", "default")
    if workers not in (None, 1, 2, 4) or gc_mode not in ("default", "off", "precollect"):
        raise ValueError("probe_configuration")
    if os.environ.get("COMPONENTPRESS_PROBE_TRACE", "1") == "1":
        TRACE_DIRECTORY = run_directory
        install_trace()
        gc.callbacks.append(gc_trace)
    if workers is not None:
        set_workers(workers)
    if os.environ.get("COMPONENTPRESS_PROBE_WARM_ENUMS") == "1":
        warm_enums()
    previously_enabled = gc.isenabled()
    if gc_mode in ("off", "precollect"):
        gc.collect()
    if gc_mode == "off":
        gc.disable()
    with (run_directory / "fault.log").open("w", encoding="ascii") as faults:
        faulthandler.enable(file=faults, all_threads=True)
        try:
            trace("probe.before")
            result = execute(run_directory)
            trace("probe.after")
            (run_directory / "probe-result.json").write_text(json.dumps({"exit_code": result}), encoding="ascii")
            return result
        finally:
            faulthandler.disable()
            if gc_trace in gc.callbacks:
                gc.callbacks.remove(gc_trace)
            if previously_enabled:
                gc.enable()


if __name__ == "__main__":
    raise SystemExit(main())
