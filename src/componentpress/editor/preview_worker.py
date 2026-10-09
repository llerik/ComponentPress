"""Thread-owned work for the visual editor's replaceable preview frame."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from componentpress.application.contracts import ProjectSnapshot
from componentpress.domain.component import ComponentDefinition
from componentpress.rendering import render_component_image
from componentpress.rendering.image_quality import measure_component_images
from componentpress.rendering.resources import ProjectResourceLoader


@dataclass(frozen=True)
class PreviewRequest:
    component_id: str
    generation: int
    snapshot: ProjectSnapshot
    component: ComponentDefinition
    dpi: int
    bindings_resolved: bool
    row_number: int | None = None


class PreviewSignals(QObject):
    finished = Signal(object)


class PreviewTask(QRunnable):
    def __init__(self, request: PreviewRequest, signals: PreviewSignals):
        super().__init__()
        self.request = request
        self.signals = signals

    def run(self) -> None:
        try:
            loader = ProjectResourceLoader(self.request.snapshot.root)
            image = render_component_image(
                self.request.snapshot,
                self.request.component_id,
                component=self.request.component,
                dpi=self.request.dpi,
                bindings_resolved=self.request.bindings_resolved,
                register_fonts=False,
                resource_loader=loader,
                preserve_logical_size=True,
            )
            quality = measure_component_images(
                self.request.component,
                self.request.snapshot.model,
                loader,
                self.request.snapshot.documents[self.request.component_id].path,
                bindings_resolved=self.request.bindings_resolved,
            )
        except Exception as exc:
            self.signals.finished.emit(("render", self.request, None, exc))
        else:
            self.signals.finished.emit(("render", self.request, (image, quality), None))


@dataclass(frozen=True)
class FontReadRequest:
    render: PreviewRequest


class PreviewFontReadTask(QRunnable):
    def __init__(self, request: PreviewRequest, signals: PreviewSignals):
        super().__init__()
        self.request = request
        self.signals = signals

    def run(self) -> None:
        folder = self.request.snapshot.root / "assets" / "fonts"
        try:
            files = []
            if folder.exists():
                for path in sorted(folder.rglob("*")):
                    if path.is_symlink():
                        from componentpress.domain.diagnostics import Diagnostic, ProjectError

                        raise ProjectError(Diagnostic("PATH_SYMLINK", "символические ссылки шрифтов не поддерживаются", path))
                    if path.is_file() and path.suffix.lower() in (".ttf", ".otf"):
                        before = path.stat()
                        data = path.read_bytes()
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
                            from componentpress.domain.diagnostics import Diagnostic, ProjectError

                            raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "шрифт изменился во время чтения", path))
                        files.append((path, data))
        except Exception as exc:
            self.signals.finished.emit(("fonts", self.request, None, exc))
        else:
            self.signals.finished.emit(("fonts", self.request, tuple(files), None))
