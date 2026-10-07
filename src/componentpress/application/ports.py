from pathlib import Path
from typing import Callable, ContextManager, Protocol, TYPE_CHECKING

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.values import DataSheetSnapshot
from componentpress.domain.project import DataSource
from .contracts import DocumentSnapshot, ProjectSnapshot
from .contracts import BuildProgress, BuildResult

if TYPE_CHECKING:
    from componentpress.build.input_snapshot import BuildInputSnapshot


class ProjectRepository(Protocol):
    def create(self, root: Path, name: str) -> ProjectSnapshot: ...
    def open(self, root: Path) -> ProjectSnapshot: ...
    def migrate(self, root: Path) -> ProjectSnapshot: ...
    def save_project(self, snapshot: ProjectSnapshot, **changes: object) -> ProjectSnapshot: ...
    def save_component(self, snapshot: ProjectSnapshot, component_id: str, **changes: object) -> ProjectSnapshot: ...
    def prepare_component(self, snapshot: ProjectSnapshot, component_id: str, model: ComponentDefinition, base: DocumentSnapshot | None = None) -> DocumentSnapshot: ...
    def prepare_text(self, snapshot: ProjectSnapshot, component_id: str, text: str) -> DocumentSnapshot: ...
    def save_prepared(self, snapshot: ProjectSnapshot, component_id: str, document: DocumentSnapshot) -> ProjectSnapshot: ...
    def add_component(self, snapshot: ProjectSnapshot, component_id: str, name: str) -> ProjectSnapshot: ...


class ResourceRepository(Protocol):
    def import_image(self, root: Path, source: Path, folder: str = "") -> str: ...
    def import_data(self, root: Path, source: Path) -> str: ...


class DataSourceReader(Protocol):
    def read(
        self, path: Path, sheet: str, *, source: str = "main",
        id_column: str | None = None, copies_column: str | None = None,
        version: int = 1, request_id: int = 0, data: bytes | None = None,
        copies_columns: tuple[str, str] | None = None,
    ) -> DataSheetSnapshot: ...


class BuildRunner(Protocol):
    def start(
        self,
        snapshot: "BuildInputSnapshot",
        on_progress: Callable[[BuildProgress], None] | None = None,
        on_finished: Callable[[BuildResult], None] | None = None,
    ) -> str: ...

    def cancel(self, job_id: str) -> bool: ...

    def is_active(self, job_id: str | None = None) -> bool: ...


WriteLockFactory = Callable[[Path], ContextManager[object]]
