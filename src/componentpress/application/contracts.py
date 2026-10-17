from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic
from componentpress.domain.project import PrintSettings, ProjectDefinition


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
class FileFingerprint:
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class RenderResult:
    png: Path
    pdf: Path | None
    width_px: int
    height_px: int
    dpi: int


@dataclass(frozen=True)
class SnapshotResource:
    relative_path: str
    content_hash: str
    size: int
    data: bytes | None = None
    copied_path: Path | None = None


@dataclass(frozen=True)
class BuildInstance:
    index: int
    component_id: str
    component_name: str
    instance_id: str
    row_number: int | None
    copies: int
    component: ComponentDefinition
    document_path: Path
    png_name: str


@dataclass(frozen=True)
class BuildInputSnapshot:
    job_id: str
    root: Path
    job_directory: Path
    staging_directory: Path
    project: ProjectDefinition
    print_settings: PrintSettings
    mode: str
    instances: tuple[BuildInstance, ...]
    resources: Mapping[str, SnapshotResource]
    input_hashes: Mapping[str, str]
    font_families: tuple[str, ...]
    registered_font_ids: tuple[int, ...]
    max_render_workers: int
    memory_budget_bytes: int
    warnings: tuple[Diagnostic, ...] = ()

    @property
    def total_copies(self) -> int:
        return sum(instance.copies for instance in self.instances)


@dataclass(frozen=True)
class BuildRequest:
    """User choices for one project build; project format remains unchanged."""

    component_ids: tuple[str, ...] | None = None
    print_settings: PrintSettings | None = None
    max_render_workers: int = 2
    memory_budget_bytes: int = 128 * 1024 * 1024
    mode: str = "prod"


@dataclass(frozen=True)
class PngArchiveProgress:
    phase: Literal["render", "pack"]
    completed: int
    total: int


@dataclass(frozen=True)
class PngArchiveResult:
    path: Path
    mode: str
    png_count: int
    copies: int
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class ArchiveExportResult:
    """Archive location and whether version patch and journal removal completed."""

    path: Path
    next_version: str
    postprocessing_complete: bool
    detail: str | None = None


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
