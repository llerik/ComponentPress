from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.project import ProjectDefinition
from componentpress.domain.project import PrintSettings
from componentpress.domain.diagnostics import Diagnostic


@dataclass(frozen=True)
class DocumentSnapshot:
    path: Path
    text: str
    disk_hash: str
    model: ComponentDefinition


@dataclass(frozen=True)
class ProjectSnapshot:
    root: Path
    text: str
    disk_hash: str
    model: ProjectDefinition
    documents: Mapping[str, DocumentSnapshot]


@dataclass(frozen=True)
class BuildRequest:
    """User choices for one project build; project format remains unchanged."""

    component_ids: tuple[str, ...] | None = None
    print_settings: PrintSettings | None = None
    max_render_workers: int = 2
    memory_budget_bytes: int = 128 * 1024 * 1024
    mode: str = "prod"


@dataclass(frozen=True)
class BuildProgress:
    job_id: str
    phase: str
    completed: int
    total: int
    message: str = ""


@dataclass(frozen=True)
class BuildResult:
    job_id: str
    status: str
    output_directory: Path | None = None
    pdf_path: Path | None = None
    report_path: Path | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    cleanup_state: str = "done"
    statistics: Mapping[str, int] = field(default_factory=dict)
    mode: str = "prod"
