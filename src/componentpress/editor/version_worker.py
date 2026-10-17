"""File-heavy version operations isolated from the Qt interface thread."""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, TypeAlias

from PySide6.QtCore import QObject, QRunnable, Signal
from componentpress.application.contracts import ArchiveExportResult, ProjectSnapshot
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.diagnostics.logging import log_event, log_exception
from componentpress.i18n import language
from componentpress.i18n.diagnostics import format_project_error


class VersionTaskSignals(QObject):
    finished = Signal(object)
    progress = Signal(int, int, str)


@dataclass(frozen=True)
class ExportArchiveRequest:
    snapshot: ProjectSnapshot
    target: Path
    replace: bool = False
    kind: ClassVar[Literal["export"]] = "export"


@dataclass(frozen=True)
class RestoreArchiveRequest:
    archive_path: Path
    destination: Path
    kind: ClassVar[Literal["import"]] = "import"


@dataclass(frozen=True)
class ImportImageFolderRequest:
    root: Path
    source: Path
    folder: str
    kind: ClassVar[Literal["image_import"]] = "image_import"


VersionTaskRequest: TypeAlias = ExportArchiveRequest | RestoreArchiveRequest | ImportImageFolderRequest
VersionTaskResult: TypeAlias = ArchiveExportResult | Path | tuple[str, ...] | None


@dataclass(frozen=True)
class VersionTaskCompletion:
    request: VersionTaskRequest
    result: VersionTaskResult = None
    error: ProjectError | None = None
    error_message: str | None = None


class VersionTask(QRunnable):
    def __init__(self, request: VersionTaskRequest, operation, cancellation, locale: str | None = None):
        super().__init__()
        self.request = request
        self.operation = operation
        self.cancellation = cancellation
        self.locale = locale or language()
        self.signals = VersionTaskSignals()

    def run(self) -> None:
        _safe_log("project.archive", operation=self.request.kind, status="started")
        try:
            result = self.operation(self.cancellation, self.signals.progress.emit)
            _safe_log("project.archive", operation=self.request.kind, status="succeeded")
            self.signals.finished.emit(VersionTaskCompletion(self.request, result))
        except Exception as exc:  # Preserve a typed operation error for the UI.
            if isinstance(exc, ProjectError):
                _safe_log("project.archive", operation=self.request.kind, status="failed", code=exc.diagnostic.code)
            else:
                incident_id = _safe_exception("project.archive", exc, operation=self.request.kind, status="failed")
                exc = ProjectError(Diagnostic("VERSION_OPERATION_FAILED", "Unexpected archive operation error", incident_id=incident_id, message_key="error.archive_failed"))
            message = format_project_error(exc, locale=self.locale)
            self.signals.finished.emit(VersionTaskCompletion(self.request, error=exc, error_message=message))


def _safe_log(event: str, **context) -> None:
    try:
        log_event(event, **context)
    except Exception:
        pass


def _safe_exception(event: str, error: BaseException, **context) -> str | None:
    try:
        return log_exception(event, error, **context)
    except Exception:
        return None

