"""File-heavy version operations isolated from the Qt interface thread."""

from PySide6.QtCore import QObject, QRunnable, Signal


class VersionTaskSignals(QObject):
    finished = Signal(object, object)
    progress = Signal(int, int, str)


class VersionTask(QRunnable):
    def __init__(self, operation, cancellation):
        super().__init__()
        self.operation = operation
        self.cancellation = cancellation
        self.signals = VersionTaskSignals()

    def run(self) -> None:
        try:
            self.signals.finished.emit(self.operation(self.cancellation, self.signals.progress.emit), None)
        except Exception as exc:  # Preserve a typed operation error for the UI.
            self.signals.finished.emit(None, exc)

