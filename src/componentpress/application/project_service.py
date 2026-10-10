"""Project use cases, independent of file and Qt adapters."""

from pathlib import Path

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.application.edits import ensure_visual_edit_respects_locks
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.project import CopiesColumns, DataSource
import re
import unicodedata
from componentpress.domain.tree import locations
from .contracts import ProjectSnapshot
from .identifiers import IdentifierGenerator
from .ports import ProjectRepository, ResourceRepository, WriteLockFactory
from .sessions import ProjectSession
from .version_service import VersionService


class ProjectService:
    def __init__(self, projects: ProjectRepository, resources: ResourceRepository, write_lock: WriteLockFactory, identifiers: IdentifierGenerator | None = None):
        self.projects = projects
        self.resources = resources
        self.write_lock = write_lock
        self.identifiers = identifiers or IdentifierGenerator()
        self.versions = VersionService(projects, write_lock)

    def create(self, root: Path, name: str) -> ProjectSnapshot:
        return self.projects.create(root, name)

    def open(self, root: Path) -> ProjectSnapshot:
        return self.projects.open(root)

    def migrate(self, root: Path) -> ProjectSnapshot:
        return self.projects.migrate(root)

    def create_session(self, root: Path, name: str) -> ProjectSession:
        return ProjectSession.from_snapshot(self.create(root, name))

    def open_session(self, root: Path) -> ProjectSession:
        return ProjectSession.from_snapshot(self.open(root))

    def migrate_session(self, root: Path) -> ProjectSession:
        return ProjectSession.from_snapshot(self.migrate(root))

    def rename(self, root: Path, name: str) -> ProjectSnapshot:
        return self.projects.save_project(self.projects.open(root), name=name)

    def resize(self, root: Path, component_id: str, width_mm: float, height_mm: float) -> ProjectSnapshot:
        snapshot = self.projects.open(root)
        document = snapshot.documents.get(component_id)
        if document is None:
            raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", "компонент не найден", root))
        size = document.model.size_mm.model_copy(update={"width": width_mm, "height": height_mm})
        return self.projects.save_component(snapshot, component_id, size_mm=size)

    def register(self, root: Path, component_id: str | None, name: str) -> ProjectSnapshot:
        snapshot = self.projects.open(root)
        component_id = component_id or self.identifiers.create(name, snapshot.documents, prefix="component")
        return self.projects.add_component(snapshot, component_id, name)

    def add_to_session(self, session: ProjectSession, name: str, component_id: str | None = None, *, width_mm: float = 63, height_mm: float = 88, data=None) -> str:
        component_id = component_id or self.identifiers.create(name, session.documents, prefix="component")
        snapshot = self.projects.add_component(session.snapshot, component_id, name, width_mm=width_mm, height_mm=height_mm, data=data)
        session.replace_snapshot(snapshot)
        return component_id

    def remove_component_for_history(self, session: ProjectSession, component_id: str, *, backup_path: str):
        return self.projects.remove_component(session.snapshot, component_id, backup_path=backup_path)

    def restore_component_from_history(self, session: ProjectSession, component_id: str, **values: object):
        return self.projects.restore_component(session.snapshot, component_id, **values)

    def node_id(self, session: ProjectSession, component_id: str, name: str, prefix: str) -> str:
        existing = locations(session.documents[component_id].model)
        return self.identifiers.create(name, existing, prefix=prefix)

    def prepare_model(self, session: ProjectSession, component_id: str, model: ComponentDefinition):
        ensure_visual_edit_respects_locks(session.documents[component_id].model, model)
        return self.projects.prepare_component(
            session.snapshot, component_id, model, session.documents[component_id].committed
        )

    def prepare_text(self, session: ProjectSession, component_id: str, text: str):
        return self.projects.prepare_text(session.snapshot, component_id, text)

    def save_document(self, session: ProjectSession, component_id: str) -> None:
        document = session.documents.get(component_id)
        if document is None:
            raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", "компонент не найден", session.snapshot.root))
        if document.draft_text is not None:
            document.replace_snapshot(self.prepare_text(session, component_id, document.draft_text))
        # Legacy callers may have changed only the model on DocumentSession.
        prepared = self.projects.prepare_component(
            session.snapshot, component_id, document.model, document.committed
        )
        if document.committed.text != prepared.text:
            document.replace_snapshot(prepared)
        snapshot = self.projects.save_prepared(session.snapshot, component_id, document.committed)
        session.replace_snapshot(snapshot, saved={component_id})

    def save_all(self, session: ProjectSession) -> None:
        for component_id in tuple(session.documents):
            if session.documents[component_id].dirty:
                self.save_document(session, component_id)

    def import_image(self, root: Path, source: Path, folder: str = "") -> str:
        snapshot = self.projects.open(root)
        with self.write_lock(snapshot.root):
            return self.resources.import_image(snapshot.root, source, folder)

    def import_data(self, session: ProjectSession, source: Path) -> str:
        """Import the project's single XLSX source."""
        with self.write_lock(session.snapshot.root):
            relative = self.resources.import_data(session.snapshot.root, source)
        updated = self.projects.save_project(session.snapshot, data_source=DataSource(path=relative))
        session.replace_snapshot(updated)
        return "main"

    def update_project_settings(self, session: ProjectSession, *, name: str, version: str,
                                prod: str, test: str, data_source_file: Path | None = None,
                                clear_data_source: bool = False) -> None:
        from componentpress.project_io.files import file_hash
        from componentpress.project_io.paths import resolve_project_path
        source_path = session.snapshot.model.data_source.path if session.snapshot.model.data_source else None
        created_path: Path | None = None
        created_hash: str | None = None
        original_project_hash = session.snapshot.disk_hash
        if data_source_file is not None:
            with self.write_lock(session.snapshot.root):
                relative = self.resources.import_data(session.snapshot.root, data_source_file)
            created_path = resolve_project_path(session.snapshot.root, relative)
            created_hash = file_hash(created_path)
            source_path = relative
        elif clear_data_source:
            source_path = None
        try:
            if source_path is None and any(document.model.data is not None for document in session.documents.values()):
                raise ProjectError(Diagnostic("DATA_SOURCE_IN_USE", "сначала отвяжите листы компонентов от Excel", session.snapshot.root))
            current = session.snapshot.model
            from componentpress.domain.project import CopiesColumns, DataSource, ProjectDefinition
            candidate = ProjectDefinition.model_validate(current.model_dump(mode="python") | {
                "name": name, "version": version,
                "data_source": DataSource(path=source_path) if source_path else None,
                "copies_columns": CopiesColumns(prod=prod, test=test),
            })
            updated = self.projects.save_project(
                session.snapshot, name=candidate.name, version=candidate.version,
                data_source=candidate.data_source, copies_columns=candidate.copies_columns,
            )
        except Exception:
            project_path = session.snapshot.root / "project.yaml"
            if (created_path is not None and created_path.exists() and created_hash is not None
                    and file_hash(created_path) == created_hash
                    and file_hash(project_path) == original_project_hash):
                try:
                    created_path.unlink()
                except OSError:
                    pass
            raise
        session.replace_snapshot(updated)

    def clear_data_source(self, session: ProjectSession) -> None:
        if any(item.model.data is not None for item in session.documents.values()):
            raise ProjectError(Diagnostic("DATA_SOURCE_IN_USE", "сначала отвяжите листы компонентов от Excel", session.snapshot.root))
        updated = self.projects.save_project(session.snapshot, data_source=None)
        session.replace_snapshot(updated)

    def set_project_details(self, session: ProjectSession, *, name: str, version: str) -> None:
        updated = self.projects.save_project(session.snapshot, name=name, version=version)
        session.replace_snapshot(updated)

    def export_project_version(self, session: ProjectSession, target: Path, *, replace: bool = False, cancellation=None, on_progress=None) -> tuple[Path, str, bool, str | None]:
        self.save_all(session)
        result = self.versions.export(session.snapshot, target, replace=replace, cancellation=cancellation, on_progress=on_progress)
        refreshed = self.projects.open(session.snapshot.root)
        session.replace_snapshot(refreshed, saved=set(refreshed.documents))
        return result

    def import_project_version(self, archive_path: Path, destination: Path) -> ProjectSession:
        return ProjectSession.from_snapshot(self.versions.restore(archive_path, destination))

    def set_copies_columns(self, session: ProjectSession, *, prod: str, test: str) -> None:
        updated = self.projects.save_project(session.snapshot, copies_columns=CopiesColumns(prod=prod, test=test))
        session.replace_snapshot(updated)
