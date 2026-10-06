"""In-memory project and document state shared by the editor views."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic

from .contracts import DocumentSnapshot, ProjectSnapshot


@dataclass
class DocumentSession:
    """Editable state for one component without any Qt dependencies."""

    source: DocumentSnapshot
    committed: DocumentSnapshot
    draft_text: str | None = None
    draft_diagnostics: tuple[Diagnostic, ...] = ()
    revision: int = 0

    @classmethod
    def from_snapshot(cls, snapshot: DocumentSnapshot) -> "DocumentSession":
        return cls(source=snapshot, committed=snapshot)

    @property
    def model(self) -> ComponentDefinition:
        return self.committed.model

    @property
    def component_id(self) -> str:
        return self.source.model.id

    @property
    def dirty(self) -> bool:
        return self.current_text != self.source.text or self.model != self.source.model

    @property
    def current_text(self) -> str:
        return self.draft_text if self.draft_text is not None else self.committed.text

    @property
    def saved_hash(self) -> str:
        return hashlib.sha256(self.source.text.encode("utf-8")).hexdigest()

    @property
    def has_invalid_draft(self) -> bool:
        return self.draft_text is not None and bool(self.draft_diagnostics)

    def update(self, **changes: object) -> None:
        values = self.model.model_dump(mode="python")
        values.update(changes)
        model = ComponentDefinition.model_validate(values)
        self.committed = DocumentSnapshot(
            self.committed.path, self.committed.text, self.committed.disk_hash, model
        )
        self.revision += 1

    def replace_snapshot(self, snapshot: DocumentSnapshot) -> None:
        """Install a validated text/model pair, including undo/redo states."""
        self.committed = snapshot
        self.draft_text = None
        self.draft_diagnostics = ()
        self.revision += 1

    def set_draft(self, text: str, diagnostics: tuple[Diagnostic, ...] = ()) -> None:
        self.draft_text = text
        self.draft_diagnostics = diagnostics
        self.revision += 1

    def clear_draft(self) -> None:
        self.draft_text = None
        self.draft_diagnostics = ()

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
                component_id: DocumentSession.from_snapshot(document)
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
                self.documents[component_id] = DocumentSession.from_snapshot(source)
            elif component_id in saved:
                document.accept_saved(source)
            else:
                document.refresh_source(source)

    def revert(self, component_id: str) -> None:
        self.documents[component_id].revert()
