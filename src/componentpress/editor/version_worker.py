"""File-heavy version operations isolated from the Qt interface thread."""

from PySide6.QtCore import QObject, QRunnable, Signal
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.diagnostics.logging import log_event, log_exception


class VersionTaskSignals(QObject):
    finished = Signal(object, object)
    progress = Signal(int, int, str)


class VersionTask(QRunnable):
    def __init__(self, operation, cancellation, kind: str = "archive"):
        super().__init__()
        self.operation = operation
        self.cancellation = cancellation
        self.kind = kind
        self.signals = VersionTaskSignals()

    def run(self) -> None:
        log_event("project.archive", operation=self.kind, status="started")
        try:
            result = self.operation(self.cancellation, self.signals.progress.emit)
            log_event("project.archive", operation=self.kind, status="succeeded")
            self.signals.finished.emit(result, None)
        except Exception as exc:  # Preserve a typed operation error for the UI.
            if isinstance(exc, ProjectError):
                log_event("project.archive", operation=self.kind, status="failed", code=exc.diagnostic.code)
            else:
                incident_id = log_exception("project.archive", exc, operation=self.kind, status="failed")
                exc = ProjectError(Diagnostic("VERSION_OPERATION_FAILED", "Unexpected archive operation error", incident_id=incident_id, message_key="error.archive_failed"))
            self.signals.finished.emit(None, exc)

