"""Compose adapters for the requested entry point."""

from typing import TYPE_CHECKING

from componentpress.project_io.repository import FileProjectRepository
from componentpress.resources.repository import ResourceRepository
from componentpress.platforms.services import ProjectWriteLock
from componentpress.application.project_service import ProjectService
from componentpress.application.preview_service import PreviewService
from componentpress.application.build_service import BuildService
from componentpress.data_sources import XlsxReader

if TYPE_CHECKING:
    from componentpress.editor.controllers import ProjectController


def project_repository() -> FileProjectRepository:
    return FileProjectRepository()


def resource_repository() -> ResourceRepository:
    return ResourceRepository()


def project_service() -> ProjectService:
    return ProjectService(project_repository(), resource_repository(), ProjectWriteLock)


def project_controller() -> "ProjectController":
    from componentpress.editor.controllers import ProjectController

    service = project_service()
    return ProjectController(service, PreviewService(XlsxReader()), BuildService(service))


def build_service() -> BuildService:
    service = project_service()
    return BuildService(service)
