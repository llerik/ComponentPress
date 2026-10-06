from pathlib import Path

from PySide6.QtCore import QLockFile, QUrl
from PySide6.QtGui import QDesktopServices

from componentpress.domain.diagnostics import Diagnostic, ProjectError


class ProjectWriteLock:
    def __init__(self, root: Path):
        self.path = root / ".componentpress" / "project.lock"
        self.lock: QLockFile | None = None

    def __enter__(self) -> "ProjectWriteLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = QLockFile(str(self.path))
        self.lock.setStaleLockTime(0)
        if not self.lock.tryLock(0):
            raise ProjectError(Diagnostic("PROJECT_LOCKED", "проект занят другим экземпляром", self.path))
        return self

    def __exit__(self, *_: object) -> None:
        if self.lock:
            self.lock.unlock()


class PlatformServices:
    @staticmethod
    def open_path(path: Path) -> bool:
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
