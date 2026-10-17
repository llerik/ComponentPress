"""Application scenario for creating and restoring project archives."""

from pathlib import Path

from componentpress.application.contracts import ProjectSnapshot
from componentpress.application.ports import ArchiveRepository, ProjectRepository


class VersionService:
    def __init__(self, archives: ArchiveRepository, projects: ProjectRepository):
        self.archives = archives
        self.projects = projects

    def export(self, snapshot: ProjectSnapshot, target: Path, *, replace: bool = False, cancellation=None, on_progress=None):
        return self.archives.export(
            snapshot, target, replace=replace, cancellation=cancellation, on_progress=on_progress,
        )

    def restore_path(self, archive_path: Path, destination: Path, *, cancellation=None, on_progress=None) -> Path:
        return self.archives.restore_path(
            archive_path, destination, cancellation=cancellation, on_progress=on_progress,
        )

    def restore(self, archive_path: Path, destination: Path, *, cancellation=None, on_progress=None) -> ProjectSnapshot:
        root = self.restore_path(archive_path, destination, cancellation=cancellation, on_progress=on_progress)
        return self.projects.open(root)
