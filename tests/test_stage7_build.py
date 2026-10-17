"""Coder checks for the stage-seven immutable threaded build pipeline."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest
from PySide6.QtCore import QEventLoop, QSize
from PySide6.QtGui import QFontDatabase, QImage
from PySide6.QtPdf import QPdfDocument

from componentpress.application.contracts import BuildRequest, SnapshotResource
from componentpress.bootstrap import build_service, project_service
from componentpress.build.engine import BuildEngine
from componentpress.build.imposition import calculate_imposition, cut_grid_lines
from componentpress.build.input_snapshot import BuildInstance, InputSnapshotBuilder, SnapshotResourceLoader
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.project import PrintSettings
from componentpress.execution.cancellation import CancellationToken
from componentpress.execution.cleanup import cleanup_job_directory
from componentpress.execution.job_store import JobStore
from componentpress.execution.qt_wait import run_build
from componentpress.execution.render_pool import render_instances
from componentpress.execution.tasks import RenderInstanceTask
from componentpress.editor.controllers import ProjectController
from componentpress.domain.diagnostics import Diagnostic
from componentpress.rendering.exporter import ensure_gui_application


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo-excel-game"


def _copy_demo(tmp_path: Path, source: Path = DEMO) -> Path:
    root = tmp_path / "Игра с пробелом и кириллицей"
    shutil.copytree(source, root, ignore=shutil.ignore_patterns("output", "archive", ".componentpress"))
    return root


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_imposition_places_ten_cards_as_nine_plus_one() -> None:
    snapshot = project_service().open(DEMO)
    component = snapshot.documents["forest-card"].model
    instances = tuple(
        BuildInstance(index, "forest-card", component.name, str(index + 1), index + 2, 1, component, snapshot.documents["forest-card"].path, f"{index}.png")
        for index in range(10)
    )
    plan = calculate_imposition(instances, PrintSettings())
    assert (plan.paper_width_mm, plan.paper_height_mm) == (210.0, 297.0)
    assert [len(page.placements) for page in plan.pages] == [9, 1]
    assert [(page.columns, page.rows) for page in plan.pages] == [(3, 3), (3, 3)]
    assert [(item.x_mm, item.y_mm) for item in plan.pages[0].placements[:4]] == [
        (5.0, 5.0), (71.0, 5.0), (137.0, 5.0), (5.0, 96.0)
    ]


def test_cut_grid_deduplicates_touching_cells_when_gap_is_zero() -> None:
    snapshot = project_service().open(DEMO)
    component = snapshot.documents["forest-card"].model
    instance = BuildInstance(0, "forest-card", component.name, "1", None, 1, component,
                             snapshot.documents["forest-card"].path, "0.png")
    touching = calculate_imposition((instance,), PrintSettings(gap_mm=0)).pages[0]
    spaced = calculate_imposition((instance,), PrintSettings(gap_mm=3)).pages[0]

    assert tuple(map(len, cut_grid_lines(touching, 0))) == (touching.columns + 1, touching.rows + 1)
    assert tuple(map(len, cut_grid_lines(spaced, 3))) == (spaced.columns * 2, spaced.rows * 2)


def test_pdf_omits_cut_grid_when_setting_is_disabled(tmp_path: Path) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    project_file = root / "project.yaml"
    project_file.write_text(project_file.read_text(encoding="utf-8").replace(
        "cut_lines: true", "cut_lines: false", 1
    ), encoding="utf-8")
    service = project_service()
    result = run_build(build_service(service), service.open_session(root), BuildRequest(max_render_workers=1))
    assert result.status == "succeeded", result.diagnostics

    pdf = QPdfDocument()
    assert pdf.load(str(result.pdf_path)) == QPdfDocument.Error.None_
    page = pdf.render(0, QSize(840, 1188))
    blank = lambda color: color.alpha() == 0 or color.lightness() > 240
    assert all(blank(page.pixelColor(x, 20)) for x in range(20))
    assert all(blank(page.pixelColor(20, y)) for y in range(20))
    pdf.close()


def test_build_is_deterministic_for_one_two_and_four_workers(tmp_path: Path) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    service = project_service()
    session = service.open_session(root)
    builds = build_service(service)
    hashes = []
    results = []
    for workers in (1, 2, 4):
        result = run_build(builds, session, BuildRequest(max_render_workers=workers))
        assert result.status == "succeeded", result.diagnostics
        assert result.cleanup_state == "done"
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        hashes.append(tuple(report["output_hashes"][item["png"]] for item in report["instances"]))
        results.append((result, report))
    assert hashes[0] == hashes[1] == hashes[2]
    for result, report in results:
        assert report["complete"] is True
        assert report["counts"] == {"instances": 3, "copies": 4, "png": 3, "pages": 1}
        assert [item["component_id"] for item in report["instances"]] == [
            "forest-card", "event-card", "event-card"
        ]
        first = QImage(str(result.output_directory / report["instances"][0]["png"]))
        assert first.size() == QSize(744, 1039)
        pdf = QPdfDocument()
        assert pdf.load(str(result.pdf_path)) == QPdfDocument.Error.None_
        assert pdf.pageCount() == 1
        points = pdf.pagePointSize(0)
        assert points.width() == pytest.approx(210 / 25.4 * 72, abs=0.51)
        assert points.height() == pytest.approx(297 / 25.4 * 72, abs=0.51)
        rendered_page = pdf.render(0, QSize(840, 1188))
        # The dashed 5 mm grid line spans the sheet, including its margins.
        row_ink = sum(rendered_page.pixelColor(x, 20).lightness() < 200 for x in range(rendered_page.width()))
        column_ink = sum(rendered_page.pixelColor(20, y).lightness() < 200 for y in range(rendered_page.height()))
        assert 100 < row_ink < rendered_page.width()
        assert 100 < column_ink < rendered_page.height()
    assert not any((root / ".componentpress" / "jobs").iterdir())


def test_snapshot_uses_disk_spill_only_for_used_resources(tmp_path: Path) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    unused = root / "assets" / "images" / "unused.png"
    unused.write_bytes((root / "assets" / "images" / "animals" / "enemy" / "leaf.png").read_bytes())
    source = project_service().open(root)
    store = JobStore(root)
    job_id = "spill-test"
    directory = store.initialize(job_id, source.model.id)
    snapshot = InputSnapshotBuilder().create(
        source,
        job_id=job_id,
        job_directory=directory,
        component_ids=("forest-card",),
        memory_budget_bytes=0,
        max_render_workers=1,
    )
    try:
        assert snapshot.resources
        assert all(item.data is None and item.copied_path and item.copied_path.is_file() for item in snapshot.resources.values())
        assert "assets/images/unused.png" not in snapshot.resources
        assert "assets/images/unused.png" not in snapshot.input_hashes
        captured = {
            name: SnapshotResourceLoader(snapshot.resources).image(name).content_hash
            for name in snapshot.resources
        }
        live = root / "assets" / "images" / "animals" / "enemy" / "leaf.png"
        live.write_bytes(b"changed after snapshot")
        name = "assets/images/animals/enemy/leaf.png"
        loader = SnapshotResourceLoader({name: snapshot.resources[name]})
        assert loader.image(name).content_hash == captured[name]
    finally:
        for font_id in reversed(snapshot.registered_font_ids):
            QFontDatabase.removeApplicationFont(font_id)
        cleanup_job_directory(store, job_id)
        store.release(job_id)


def test_memory_and_disk_snapshots_render_identically(tmp_path: Path) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    source = project_service().open(root)
    rendered = []
    snapshots = []
    store = JobStore(root)
    for job_id, budget in (("memory-zero", 0), ("memory-enough", 1024 * 1024 * 1024)):
        directory = store.initialize(job_id, source.model.id)
        snapshot = InputSnapshotBuilder().create(
            source,
            job_id=job_id,
            job_directory=directory,
            component_ids=("forest-card",),
            memory_budget_bytes=budget,
            max_render_workers=1,
        )
        snapshots.append(snapshot)
        try:
            assert snapshot.resources
            errors = []
            hashes = []
            for instance in snapshot.instances:
                RenderInstanceTask(
                    snapshot, instance, CancellationToken(),
                    lambda _index, error: errors.append(error) if error else None,
                ).run()
                assert not errors, errors
                hashes.append(_hash(snapshot.staging_directory / "images" / instance.png_name))
            rendered.append(tuple(hashes))
        finally:
            for font_id in reversed(snapshot.registered_font_ids):
                QFontDatabase.removeApplicationFont(font_id)
            cleanup_job_directory(store, job_id)
            store.release(job_id)

    memory, disk = snapshots
    assert memory.input_hashes == disk.input_hashes
    assert memory.instances == disk.instances
    assert memory.warnings == disk.warnings
    assert all(item.data is None and item.copied_path for item in memory.resources.values())
    assert all(item.data is not None for item in disk.resources.values())
    assert rendered[0] == rendered[1]


def test_snapshot_resource_loader_prefers_memory_over_disk(tmp_path: Path) -> None:
    data = (DEMO / "assets" / "images" / "animals" / "enemy" / "leaf.png").read_bytes()
    name = "assets/image.png"
    resource = SnapshotResource(name, hashlib.sha256(data).hexdigest(), len(data), data, tmp_path / "missing.png")

    loaded = SnapshotResourceLoader({name: resource}).image(name)

    assert loaded.content_hash == resource.content_hash


@pytest.mark.parametrize("storage", ["none", "missing", "corrupt", "empty"])
def test_snapshot_resource_loader_preserves_read_diagnostics(storage: str, tmp_path: Path) -> None:
    name = "assets/image.png"
    copied = tmp_path / "missing.png"
    resource = SnapshotResource(name, "bad-hash", 0, copied_path=copied)
    if storage == "corrupt":
        copied.write_bytes((DEMO / "assets" / "images" / "animals" / "enemy" / "leaf.png").read_bytes())
    elif storage == "empty":
        copied.write_bytes((DEMO / "assets" / "images" / "animals" / "enemy" / "leaf.png").read_bytes())
        resource = SnapshotResource(name, hashlib.sha256(b"").hexdigest(), 0, data=b"", copied_path=copied)
    loader = SnapshotResourceLoader({name: resource})

    if storage == "none":
        resource = SnapshotResource(name, "bad-hash", 0)
        loader = SnapshotResourceLoader({name: resource})
        with pytest.raises(RuntimeError, match="resource has no storage"):
            loader.image(name)
        return
    with pytest.raises(ProjectError) as error:
        loader.image(name, owner=DEMO, field="image.source")
    assert error.value.diagnostic.code == {
        "missing": "SNAPSHOT_READ", "corrupt": "SNAPSHOT_CORRUPT", "empty": "RESOURCE_INVALID",
    }[storage]
    assert error.value.diagnostic.field == "image.source"
    if storage in {"missing", "corrupt"}:
        assert error.value.diagnostic.path == copied


def test_external_change_during_snapshot_is_diagnosed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    source = project_service().open(root)
    store = JobStore(root)
    job_id = "change-test"
    directory = store.initialize(job_id, source.model.id)
    import componentpress.build.input_snapshot as module

    original = module.file_hash
    changed = False

    def changing_hash(path: Path) -> str:
        nonlocal changed
        if not changed and path.name == "project.yaml":
            changed = True
            path.write_text(path.read_text(encoding="utf-8") + "# external\n", encoding="utf-8")
        return original(path)

    monkeypatch.setattr(module, "file_hash", changing_hash)
    try:
        with pytest.raises(ProjectError) as error:
            InputSnapshotBuilder().create(source, job_id=job_id, job_directory=directory)
        assert error.value.diagnostic.code == "FILE_CHANGED_EXTERNALLY"
    finally:
        cleanup_job_directory(store, job_id)
        store.release(job_id)


def test_cancelled_build_does_not_publish_and_cleans_job(tmp_path: Path) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    source = project_service().open(root)
    store = JobStore(root)
    job_id = "cancel-test"
    directory = store.initialize(job_id, source.model.id)
    snapshot = InputSnapshotBuilder().create(source, job_id=job_id, job_directory=directory)
    token = CancellationToken()
    token.cancel()
    result = BuildEngine(store, render_instances).execute(snapshot, token, lambda _progress: None)
    for font_id in reversed(snapshot.registered_font_ids):
        QFontDatabase.removeApplicationFont(font_id)
    store.release(job_id)
    assert result.status == "cancelled"
    assert result.output_directory is None
    assert not directory.exists()
    assert not (root / "output").exists()


def test_png_snapshot_failure_does_not_publish_or_remove_previous_build(tmp_path: Path) -> None:
    """A render-time snapshot error must leave only an already-published build."""
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    previous = root / "output" / "build-previous"
    previous.mkdir(parents=True)
    (previous / "marker.txt").write_text("keep", encoding="utf-8")
    source = project_service().open(root)
    store = JobStore(root)
    job_id = "broken-png"
    directory = store.initialize(job_id, source.model.id)
    snapshot = InputSnapshotBuilder().create(
        source,
        job_id=job_id,
        job_directory=directory,
        component_ids=("forest-card",),
        max_render_workers=2,
    )
    try:
        # The renderer can now only read the captured bytes.  Corrupting that
        # managed copy makes the PNG phase fail after preparation has succeeded.
        resource = next(item for item in snapshot.resources.values() if item.copied_path is None)
        object.__setattr__(resource, "data", b"not an image")
        result = BuildEngine(store, render_instances).execute(snapshot, CancellationToken(), lambda _progress: None)
    finally:
        for font_id in reversed(snapshot.registered_font_ids):
            QFontDatabase.removeApplicationFont(font_id)
        store.release(job_id)
    assert result.status == "failed"
    assert result.output_directory is None
    assert any(item.code in {"SNAPSHOT_CORRUPT", "RESOURCE_INVALID"} for item in result.diagnostics)
    assert (previous / "marker.txt").read_text(encoding="utf-8") == "keep"
    assert sorted((root / "output").iterdir()) == [previous]
    assert not directory.exists()


def test_service_can_cancel_during_background_preparation(tmp_path: Path) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    service = project_service()
    session = service.open_session(root)
    builds = build_service(service)
    loop = QEventLoop()
    results = []
    job_id = builds.start(session, BuildRequest(), on_finished=lambda result: (results.append(result), loop.quit()))
    assert builds.cancel(job_id)
    loop.exec()
    assert results[0].status == "cancelled"
    assert not (root / "output").exists()
    assert not any((root / ".componentpress" / "jobs").iterdir())


def test_recovery_preserves_published_result_and_unknown_metadata(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = JobStore(root)
    job_id = "published-test"
    directory = store.initialize(job_id, "project")
    output = root / "output" / "build-published"
    output.mkdir(parents=True)
    (output / "print.pdf").write_bytes(b"pdf")
    digest = _hash(output / "print.pdf")
    (output / "build-report.json").write_text(json.dumps({
        "job_id": job_id,
        "complete": True,
        "output_hashes": {"print.pdf": digest},
    }), encoding="utf-8")
    store.update(job_id, phase="publishing", output_directory="output/build-published")
    (directory / "staging").mkdir()
    store.release(job_id)
    diagnostics = JobStore(root).recover()
    assert any(item.code == "BUILD_RECOVERED" for item in diagnostics)
    assert output.is_dir()
    assert not directory.exists()

    unknown = root / ".componentpress" / "jobs" / "unknown"
    unknown.mkdir(parents=True)
    (unknown / "job.json").write_text("broken", encoding="utf-8")
    diagnostics = JobStore(root).recover()
    assert any(item.code == "JOB_METADATA_INVALID" for item in diagnostics)
    assert unknown.exists()


def test_recovery_does_not_delete_live_owned_job(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    owner = JobStore(root)
    directory = owner.initialize("active-test", "project")
    diagnostics = JobStore(root).recover()
    assert any(item.code == "JOB_OWNER_UNKNOWN" for item in diagnostics)
    assert directory.exists()
    cleanup_job_directory(owner, "active-test")
    owner.release("active-test")


def test_post_publish_status_failure_keeps_successful_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    source = project_service().open(root)
    store = JobStore(root)
    job_id = "post-publish-failure"
    directory = store.initialize(job_id, source.model.id)
    snapshot = InputSnapshotBuilder().create(
        source,
        job_id=job_id,
        job_directory=directory,
        component_ids=("event-card",),
        max_render_workers=1,
    )
    original_update = store.update

    def failing_update(target_job_id: str, **changes) -> None:
        if changes.get("phase") == "succeeded":
            raise ProjectError(Diagnostic("INJECTED_STATUS_FAILURE", "status write failed"))
        original_update(target_job_id, **changes)

    monkeypatch.setattr(store, "update", failing_update)
    try:
        result = BuildEngine(store, render_instances).execute(snapshot, CancellationToken(), lambda _progress: None)
    finally:
        for font_id in reversed(snapshot.registered_font_ids):
            QFontDatabase.removeApplicationFont(font_id)
        store.release(job_id)
    assert result.status == "succeeded"
    assert result.output_directory is not None and result.output_directory.is_dir()
    assert result.pdf_path is not None and result.pdf_path.is_file()
    assert result.report_path is not None and JobStore.verify_published(result.output_directory, job_id)
    assert any(item.code == "BUILD_BOOKKEEPING_FAILED" for item in result.diagnostics)
    assert not directory.exists()


def test_post_publish_history_failure_is_successful_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    source = project_service().open(root)
    store = JobStore(root)
    job_id = "post-publish-history"
    directory = store.initialize(job_id, source.model.id)
    snapshot = InputSnapshotBuilder().create(
        source,
        job_id=job_id,
        job_directory=directory,
        component_ids=("event-card",),
        max_render_workers=1,
    )
    monkeypatch.setattr(store, "save_history", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("history busy")))
    try:
        result = BuildEngine(store, render_instances).execute(snapshot, CancellationToken(), lambda _progress: None)
    finally:
        for font_id in reversed(snapshot.registered_font_ids):
            QFontDatabase.removeApplicationFont(font_id)
        store.release(job_id)
    assert result.status == "succeeded"
    assert result.cleanup_state == "cleanup_pending"
    assert result.output_directory is not None and JobStore.verify_published(result.output_directory, job_id)
    assert any(item.code == "BUILD_BOOKKEEPING_FAILED" for item in result.diagnostics)
    assert any(item.code == "CLEANUP_PENDING" for item in result.diagnostics)
    assert directory.exists()
    recovered = JobStore(root).recover()
    assert any(item.code == "BUILD_RECOVERED" for item in recovered)
    assert not directory.exists()


def test_unused_broken_project_font_does_not_block_build(tmp_path: Path) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    font = root / "assets" / "fonts" / "unused-broken.ttf"
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(b"not a font")
    service = project_service()
    result = run_build(
        build_service(service),
        service.open_session(root),
        BuildRequest(component_ids=("event-card",), max_render_workers=1),
    )
    assert result.status == "succeeded", result.diagnostics
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert "assets/fonts/unused-broken.ttf" not in report["input_hashes"]


def test_broken_used_project_font_is_diagnosed(tmp_path: Path) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    font = root / "assets" / "fonts" / "used-broken.ttf"
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(b"not a font")
    component = root / "components" / "event-card.yaml"
    component.write_text(
        component.read_text(encoding="utf-8").replace("font_family: Arial", "font_family: used-broken"),
        encoding="utf-8",
    )
    service = project_service()
    result = run_build(
        build_service(service),
        service.open_session(root),
        BuildRequest(component_ids=("event-card",), max_render_workers=1),
    )
    assert result.status == "failed"
    assert any(item.code == "FONT_INVALID" and item.path == font for item in result.diagnostics)
    assert not (root / "output").exists()


def test_recovery_reports_cleanup_pending_without_false_cleaned_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    owner = JobStore(root)
    directory = owner.initialize("cleanup-pending", "project")
    owner.release("cleanup-pending")

    pending = Diagnostic("CLEANUP_PENDING", "file is busy", directory / "busy.tmp", severity="warning")
    monkeypatch.setattr("componentpress.execution.cleanup.cleanup_job_directory", lambda _store, _job_id: (pending,))
    diagnostics = JobStore(root).recover()
    assert pending in diagnostics
    recovered = next(item for item in diagnostics if item.code == "BUILD_INTERRUPTED")
    assert "очищено" not in recovered.message
    assert "повторной очистки" in recovered.message


def test_recovery_warnings_reach_controller_and_build_result(tmp_path: Path) -> None:
    ensure_gui_application()
    root = _copy_demo(tmp_path)
    unknown = root / ".componentpress" / "jobs" / "unknown"
    unknown.mkdir(parents=True)
    (unknown / "job.json").write_text("broken", encoding="utf-8")
    service = project_service()
    builds = build_service(service)
    controller = ProjectController(service, build=builds)
    session = controller.open(root)
    assert any(item.code == "JOB_METADATA_INVALID" for item in controller.recovery_diagnostics)
    result = run_build(builds, session, BuildRequest(component_ids=("event-card",), max_render_workers=1))
    assert result.status == "succeeded", result.diagnostics
    assert any(item.code == "JOB_METADATA_INVALID" for item in result.diagnostics)


def test_demo_copy_ignores_runtime_artifacts_from_dirty_source(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty-demo"
    shutil.copytree(DEMO, dirty, ignore=shutil.ignore_patterns("output", "archive", ".componentpress"))
    (dirty / "output" / "build-old").mkdir(parents=True)
    (dirty / "output" / "build-old" / "print.pdf").write_bytes(b"old")
    (dirty / "archive").mkdir()
    (dirty / ".componentpress" / "jobs" / "old").mkdir(parents=True)
    copied = _copy_demo(tmp_path / "isolated", dirty)
    assert not (copied / "output").exists()
    assert not (copied / "archive").exists()
    assert not (copied / ".componentpress").exists()
