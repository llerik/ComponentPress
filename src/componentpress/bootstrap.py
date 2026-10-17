"""Compose adapters for the requested entry point."""

from typing import TYPE_CHECKING

from componentpress.project_io.repository import FileProjectRepository
from componentpress.resources.repository import ResourceRepository
from componentpress.platforms.services import ProjectWriteLock
from componentpress.application.project_service import ProjectService
from componentpress.application.preview_service import PreviewService
from componentpress.application.build_service import BuildService
from componentpress.execution.build_runtime import QtBuildRuntime
from componentpress.execution.png_archive_runtime import QtPngArchiveRuntime
from componentpress.execution.cancellation import CancellationToken
from componentpress.build.input_snapshot import InputSnapshotBuilder
from componentpress.bindings.resolver import BindingResolver
from componentpress.data_sources import XlsxReader
from componentpress.i18n import tr
from componentpress.i18n.diagnostics import format_diagnostic
from componentpress.project_io.validation_state import PreviewValidationStore
from componentpress.rendering.exporter import render_component

if TYPE_CHECKING:
    from componentpress.editor.controllers import ProjectController


def project_repository() -> FileProjectRepository:
    return FileProjectRepository()


def resource_repository() -> ResourceRepository:
    return ResourceRepository()


def project_service() -> ProjectService:
    return ProjectService(
        project_repository(), resource_repository(), ProjectWriteLock,
        diagnostic_formatter=format_diagnostic,
    )


def project_controller() -> "ProjectController":
    from componentpress.editor.controllers import ProjectController

    service = project_service()
    return ProjectController(service, preview_service(), build_service(service), export_png_archive)


def preview_service(reader=None) -> PreviewService:
    return PreviewService(reader or XlsxReader(), PreviewValidationStore(), BindingResolver, render_component)


def build_service(service=None) -> BuildService:
    service = service or project_service()
    return BuildService(
        service.save_all,
        lambda root: QtBuildRuntime(root, InputSnapshotBuilder(), ProjectWriteLock),
        CancellationToken,
        lambda: tr("ui.build_prepare"),
    )


def export_png_archive(source, target, *, mode="prod", component_ids=None, cancellation=None, on_progress=None):
    from componentpress.application.png_archive import export_png_archive as run_export

    return run_export(
        source, target,
        runtime=QtPngArchiveRuntime(source.root, InputSnapshotBuilder()),
        cancellation=cancellation or CancellationToken(),
        mode=mode, component_ids=component_ids, on_progress=on_progress,
    )
