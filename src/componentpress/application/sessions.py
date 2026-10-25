"""In-memory project and document state shared by the editor views."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError

from .contracts import DocumentSnapshot, ProjectSnapshot


@dataclass
class DocumentSession:
    """Editable state for one component without any Qt dependencies."""

    source: DocumentSnapshot
    committed: DocumentSnapshot
    registered_id: str = ""
    draft_text: str | None = None
    draft_diagnostics: tuple[Diagnostic, ...] = ()
    draft_snapshot: DocumentSnapshot | None = None
    revision: int = 0

    @classmethod
    def from_snapshot(cls, snapshot: DocumentSnapshot, component_id: str) -> "DocumentSession":
        return cls(source=snapshot, committed=snapshot, registered_id=component_id)

    @property
    def model(self) -> ComponentDefinition | None:
        return self.committed.model

    @property
    def component_id(self) -> str:
        return self.registered_id or (self.model.id if self.model is not None else "")

    @property
    def display_name(self) -> str:
        return self.model.name if self.model is not None else self.source.path.stem

    @property
    def dirty(self) -> bool:
        return self.current_text != self.source.text or self.committed.model != self.source.model

    @property
    def current_text(self) -> str:
        return self.draft_text if self.draft_text is not None else self.committed.text

    @property
    def saved_hash(self) -> str:
        return hashlib.sha256(self.source.text.encode("utf-8")).hexdigest()

    @property
    def has_invalid_draft(self) -> bool:
        return self.draft_text is not None and (
            self.draft_snapshot is None
            or self.draft_snapshot.model is None
            or any(item.severity == "error" for item in self.draft_diagnostics)
        )

    @property
    def current_diagnostics(self) -> tuple[Diagnostic, ...]:
        return self.draft_diagnostics if self.draft_text is not None else self.committed.diagnostics

    @property
    def model_ready(self) -> bool:
        current = self.draft_snapshot if self.draft_text is not None else self.committed
        return current is not None and current.model is not None and not any(
            item.severity == "error" for item in self.current_diagnostics
        )

    def require_model(self) -> ComponentDefinition:
        if self.draft_text is not None or not self.model_ready or self.model is None:
            diagnostic = next(iter(self.current_diagnostics), None)
            if diagnostic is None:
                diagnostic = Diagnostic("COMPONENT_INVALID", "component model is unavailable", self.source.path)
            raise ProjectError(diagnostic)
        return self.model

    def update(self, **changes: object) -> None:
        if self.model is None:
            raise ValueError("cannot update a component without a valid model")
        values = self.model.model_dump(mode="python")
        values.update(changes)
        model = ComponentDefinition.model_validate(values)
        self.committed = DocumentSnapshot(
            self.committed.path, self.committed.text, self.committed.disk_hash, model,
            self.committed.diagnostics,
        )
        self.revision += 1

    def replace_snapshot(self, snapshot: DocumentSnapshot) -> None:
        """Install a validated text/model pair, including undo/redo states."""
        self.committed = snapshot
        self.draft_text = None
        self.draft_diagnostics = ()
        self.draft_snapshot = None
        self.revision += 1

    def set_draft(
        self, text: str, diagnostics: tuple[Diagnostic, ...] = (),
        snapshot: DocumentSnapshot | None = None,
    ) -> None:
        self.draft_text = text
        self.draft_diagnostics = diagnostics
        self.draft_snapshot = snapshot
        self.revision += 1

    def clear_draft(self) -> None:
        self.draft_text = None
        self.draft_diagnostics = ()
        self.draft_snapshot = None

    def accept_saved(self, snapshot: DocumentSnapshot) -> None:
        self.source = snapshot
        self.committed = snapshot
        self.clear_draft()
        self.revision += 1

    def refresh_source(self, snapshot: DocumentSnapshot) -> None:
        """Refresh hashes after another document was saved, preserving edits."""
        self.source = snapshot

    def revert(self) -> None:
        self.committed = self.source
        self.clear_draft()
        self.revision += 1


@dataclass
class ProjectSession:
    """One open project and the independent editable component sessions."""

    snapshot: ProjectSnapshot
    documents: dict[str, DocumentSession] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, snapshot: ProjectSnapshot) -> "ProjectSession":
        return cls(
            snapshot=snapshot,
            documents={
                component_id: DocumentSession.from_snapshot(document, component_id)
                for component_id, document in snapshot.documents.items()
            },
        )

    @property
    def dirty(self) -> bool:
        return any(document.dirty for document in self.documents.values())

    def replace_snapshot(self, snapshot: ProjectSnapshot, *, saved: set[str] | frozenset[str] = frozenset()) -> None:
        self.snapshot = snapshot
        for component_id, source in snapshot.documents.items():
            document = self.documents.get(component_id)
            if document is None:
                self.documents[component_id] = DocumentSession.from_snapshot(source, component_id)
            elif component_id in saved:
                document.accept_saved(source)
            else:
                document.refresh_source(source)

    def revert(self, component_id: str) -> None:
        self.documents[component_id].revert()
