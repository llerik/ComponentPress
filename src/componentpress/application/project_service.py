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


class ProjectService:
    def __init__(self, projects: ProjectRepository, resources: ResourceRepository, write_lock: WriteLockFactory, identifiers: IdentifierGenerator | None = None):
        self.projects = projects
        self.resources = resources
        self.write_lock = write_lock
        self.identifiers = identifiers or IdentifierGenerator()

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

    def add_to_session(self, session: ProjectSession, name: str, component_id: str | None = None) -> str:
        component_id = component_id or self.identifiers.create(name, session.documents, prefix="component")
        snapshot = self.projects.add_component(session.snapshot, component_id, name)
        session.replace_snapshot(snapshot)
        return component_id

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

    def clear_data_source(self, session: ProjectSession) -> None:
        if any(item.model.data is not None for item in session.documents.values()):
            raise ProjectError(Diagnostic("DATA_SOURCE_IN_USE", "сначала отвяжите листы компонентов от Excel", session.snapshot.root))
        updated = self.projects.save_project(session.snapshot, data_source=None)
        session.replace_snapshot(updated)

    def set_project_details(self, session: ProjectSession, *, name: str, version: str) -> None:
        updated = self.projects.save_project(session.snapshot, name=name, version=version)
        session.replace_snapshot(updated)

    def set_copies_columns(self, session: ProjectSession, *, prod: str, test: str) -> None:
        updated = self.projects.save_project(session.snapshot, copies_columns=CopiesColumns(prod=prod, test=test))
        session.replace_snapshot(updated)
