"""Controllers joining editor intent to application use cases."""

from pathlib import Path

from componentpress.application.project_service import ProjectService
from componentpress.application.preview_service import PreviewService
from componentpress.application.build_service import BuildService
from componentpress.application.contracts import BuildRequest
from componentpress.application.sessions import DocumentSession, ProjectSession
from componentpress.domain.diagnostics import Diagnostic, ProjectError


class ProjectController:
    def __init__(self, service: ProjectService, preview: PreviewService | None = None, build: BuildService | None = None):
        self.service = service
        self.preview = preview
        self.build = build
        self.session: ProjectSession | None = None
        self.recovery_diagnostics: tuple[Diagnostic, ...] = ()

    def create(self, root: Path, name: str) -> ProjectSession:
        self.recovery_diagnostics = ()
        self.session = self.service.create_session(root, name)
        return self.session

    def open(self, root: Path) -> ProjectSession:
        self.session = self.service.open_session(root)
        version_diagnostics = getattr(self.service.projects, "version_recovery_diagnostics", ())
        build_diagnostics = self.build.recover(root) if self.build is not None else ()
        self.recovery_diagnostics = (*version_diagnostics, *build_diagnostics)
        return self.session

    def migrate(self, root: Path) -> ProjectSession:
        self.recovery_diagnostics = ()
        self.session = self.service.migrate_session(root)
        return self.session

    def import_project_version(self, archive_path: Path, destination: Path) -> ProjectSession:
        if self.session is not None:
            try:
                destination.resolve().relative_to(self.session.snapshot.root.resolve())
            except ValueError:
                pass
            else:
                raise ProjectError(Diagnostic("ARCHIVE_DESTINATION", "восстанавливайте проект в отдельную папку вне открытого проекта", destination))
        self.session = self.service.import_project_version(archive_path, destination)
        version_diagnostics = getattr(self.service.projects, "version_recovery_diagnostics", ())
        build_diagnostics = self.build.recover(destination) if self.build is not None else ()
        self.recovery_diagnostics = (*version_diagnostics, *build_diagnostics)
        return self.session

    def set_session(self, session: ProjectSession) -> None:
        self.session = session

    def document(self, component_id: str) -> DocumentSession:
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.session.documents[component_id]

    def add_component(self, name: str, component_id: str | None = None, *, width_mm: float = 63, height_mm: float = 88, data=None) -> str:
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.service.add_to_session(self.session, name, component_id, width_mm=width_mm, height_mm=height_mm, data=data)

    def remove_component_for_history(self, component_id: str, *, backup_path: str):
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.service.remove_component_for_history(self.session, component_id, backup_path=backup_path)

    def restore_component_from_history(self, component_id: str, **values: object):
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.service.restore_component_from_history(self.session, component_id, **values)

    def update_component(self, component_id: str, **changes: object) -> None:
        document = self.document(component_id)
        values = document.model.model_dump(mode="python")
        values.update(changes)
        prepared = self.service.prepare_model(
            self.session,
            component_id,
            type(document.model).model_validate(values),
        )
        document.replace_snapshot(prepared)

    def prepare_model(self, component_id: str, model):
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.service.prepare_model(self.session, component_id, model)

    def prepare_text(self, component_id: str, text: str):
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.service.prepare_text(self.session, component_id, text)

    def node_id(self, component_id: str, name: str, prefix: str) -> str:
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.service.node_id(self.session, component_id, name, prefix)

    def save_document(self, component_id: str) -> None:
        if self.session is None:
            raise RuntimeError("проект не открыт")
        self.service.save_document(self.session, component_id)

    def save_all(self) -> None:
        if self.session is None:
            return
        self.service.save_all(self.session)

    def revert_document(self, component_id: str) -> None:
        if self.session is not None:
            self.session.revert(component_id)

    def import_data(self, source: Path) -> str:
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.service.import_data(self.session, source)

    def update_project_settings(self, **values: object) -> None:
        if self.session is None:
            raise RuntimeError("проект не открыт")
        self.service.update_project_settings(self.session, **values)

    def start_build(self, request: BuildRequest, *, on_progress=None, on_finished=None) -> str:
        if self.session is None or self.build is None:
            raise RuntimeError("сервис сборки недоступен")
        return self.build.start(self.session, request, on_progress=on_progress, on_finished=on_finished)

    def cancel_build(self, job_id: str) -> bool:
        return bool(self.build and self.build.cancel(job_id))
