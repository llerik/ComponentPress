from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import zipfile

import pytest

from componentpress.execution.cancellation import CancellationToken
from componentpress.execution.png_archive_runtime import QtPngArchiveRuntime, _create_temp_file
from componentpress.execution.job_store import JobStore
from componentpress.domain.diagnostics import ProjectError


pytestmark = pytest.mark.skipif(os.name != "nt", reason="FILE_FLAG_DELETE_ON_CLOSE is Windows-specific")


@pytest.mark.parametrize("stage", ("created", "packing", "ready", "published"))
def test_process_exit_removes_delete_on_close_temp(tmp_path: Path, stage: str) -> None:
    marker = tmp_path / "temp-name.txt"
    target = tmp_path / "published.zip"
    script = """
import os, sys, zipfile
from pathlib import Path
from componentpress.execution.png_archive_runtime import _create_temp_file
stage, marker, target = sys.argv[1:]
temporary, stream = _create_temp_file(None)
Path(marker).write_text(str(temporary))
if stage == 'packing':
    archive = zipfile.ZipFile(stream, 'w')
    entry = archive.open('images/partial.png', 'w')
    entry.write(b'partial')
elif stage in ('ready', 'published'):
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('manifest.json', '{"manifest_version": 1}')
    stream.flush()
if stage == 'published':
    os.link(temporary, target)
os._exit(73)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(Path.cwd() / "src"), env.get("PYTHONPATH", "")))
    result = subprocess.run([sys.executable, "-c", script, stage, str(marker), str(target)], cwd=Path.cwd(), env=env, timeout=30)
    assert result.returncode == 73
    assert not Path(marker.read_text()).exists()
    assert target.exists() is (stage == "published")
    if target.exists():
        with zipfile.ZipFile(target) as archive:
            assert archive.testzip() is None


def test_cross_volume_publication_closes_both_temporary_handles() -> None:
    temp_volume = Path(tempfile.gettempdir()).stat().st_dev
    target_volume = Path.cwd().stat().st_dev
    if temp_volume == target_volume:
        pytest.skip("test temp and target are on the same volume")

    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        runtime = QtPngArchiveRuntime(Path.cwd(), builder=None)
        runtime._temporary, runtime._temporary_file = _create_temp_file(None)
        with zipfile.ZipFile(runtime._temporary_file, "w") as archive:
            archive.writestr("manifest.json", '{"manifest_version": 1}')
        target = Path(directory) / "cross-volume.zip"
        runtime._link_target(target, CancellationToken(), None, 1)
        temporary_paths = (runtime._temporary, runtime._copy_temporary)
        assert all(path is not None and path.exists() for path in temporary_paths)
        assert runtime._close_temp_files() == []
        assert all(path is not None and not path.exists() for path in temporary_paths)
        with zipfile.ZipFile(target) as archive:
            assert archive.testzip() is None


@pytest.mark.parametrize("stage", ("copying", "published"))
def test_process_exit_during_cross_volume_work_preserves_lifetime(stage: str) -> None:
    if Path(tempfile.gettempdir()).stat().st_dev == Path.cwd().stat().st_dev:
        pytest.skip("system temp and workspace are on the same volume")
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        root = Path(directory)
        marker = root / "temp-names.json"
        target = root / "cross-volume.zip"
        script = """
import json, os, sys, zipfile
from pathlib import Path
import componentpress.execution.png_archive_runtime as module
from componentpress.execution.cancellation import CancellationToken
stage, marker, target = sys.argv[1:]
runtime = module.QtPngArchiveRuntime(Path.cwd(), builder=None)
runtime._temporary, runtime._temporary_file = module._create_temp_file(None)
names = [str(runtime._temporary)]
Path(marker).write_text(json.dumps(names))
with zipfile.ZipFile(runtime._temporary_file, 'w', compression=zipfile.ZIP_STORED) as archive:
    archive.writestr('large.bin', bytes(range(256)) * 8192)
source_size = runtime._temporary_file.tell()
create = module._create_temp_file
def record_temp(directory):
    path, stream = create(directory)
    names.append(str(path))
    Path(marker).write_text(json.dumps(names))
    return path, stream
module._create_temp_file = record_temp
def stop_during_copy(update):
    if stage == 'copying':
        Path(marker).write_text(json.dumps({'names': names, 'source_size': source_size,
                                           'copied_size': runtime._copy_temporary_file.tell()}))
        os._exit(74)
runtime._link_target(Path(target), CancellationToken(), stop_during_copy, 1)
os._exit(74 if stage == 'copying' else 75)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join((str(Path.cwd() / "src"), env.get("PYTHONPATH", "")))
        result = subprocess.run(
            [sys.executable, "-c", script, stage, str(marker), str(target)], cwd=Path.cwd(), env=env, timeout=30
        )
        assert result.returncode == (74 if stage == "copying" else 75)
        crash_state = json.loads(marker.read_text())
        names = crash_state if isinstance(crash_state, list) else crash_state['names']
        assert all(not Path(name).exists() for name in names)
        if stage == "copying":
            assert 0 < crash_state["copied_size"] < crash_state["source_size"]
        assert target.exists() is (stage == "published")
        if target.exists():
            with zipfile.ZipFile(target) as archive:
                assert archive.testzip() is None


def test_png_job_recovery_keeps_published_zip(tmp_path: Path) -> None:
    target = tmp_path / "published.zip"
    target.write_bytes(b"published archive")
    store = JobStore(tmp_path / "project")
    store.initialize("job-id", "project-id")
    store.update("job-id", operation="png_export", mode="prod")
    store.release("job-id")

    diagnostics = store.recover()
    assert any(item.code == "BUILD_INTERRUPTED" for item in diagnostics)
    assert target.read_bytes() == b"published archive"


def test_cross_volume_copy_cancellation_does_not_publish() -> None:
    if Path(tempfile.gettempdir()).stat().st_dev == Path.cwd().stat().st_dev:
        pytest.skip("system temp and workspace are on the same volume")
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        runtime = QtPngArchiveRuntime(Path.cwd(), builder=None)
        runtime._temporary, runtime._temporary_file = _create_temp_file(None)
        with zipfile.ZipFile(runtime._temporary_file, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("large.bin", os.urandom(2 * 1024 * 1024))
        source_temp = runtime._temporary
        target = Path(directory) / "cancelled.zip"
        cancellation = CancellationToken()
        updates = []

        def cancel_during_copy(update) -> None:
            updates.append(update)
            cancellation.cancel()

        with pytest.raises(ProjectError) as error:
            runtime._link_target(target, cancellation, cancel_during_copy, 1)
        assert error.value.diagnostic.code == "BUILD_CANCELLED"
        destination_temp = runtime._copy_temporary
        assert updates and not target.exists()
        assert runtime._close_temp_files() == []
        assert not source_temp.exists() and not destination_temp.exists()


def test_logging_failure_after_publish_does_not_skip_handle_cleanup(tmp_path: Path, monkeypatch) -> None:
    import componentpress.execution.png_archive_runtime as png_runtime

    runtime = QtPngArchiveRuntime(tmp_path, builder=None)
    runtime._job_id = "0" * 32
    runtime._temporary, runtime._temporary_file = _create_temp_file(None)
    temporary = runtime._temporary
    snapshot = SimpleNamespace(
        mode="prod", instances=(), total_copies=0, project=SimpleNamespace(id="project"), input_hashes={}
    )
    original_log_event = png_runtime.log_event

    def fail_selected_events(event: str, **context) -> None:
        if event.endswith(".publish") and context.get("status") == "succeeded":
            raise OSError("log sink unavailable")
        if event.endswith(".cleanup") and context.get("phase") == "cleanup" and context.get("status") == "started":
            raise OSError("log sink unavailable")
        original_log_event(event, **context)

    monkeypatch.setattr(png_runtime, "log_event", fail_selected_events)
    target = tmp_path / "published.zip"
    result = runtime.publish(snapshot, target, CancellationToken(), None)
    runtime.finish("job-id", True, None)

    assert result.path == target and target.is_file()
    assert not temporary.exists()
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
