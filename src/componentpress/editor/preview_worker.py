"""Thread-owned work for the visual editor's replaceable preview frame."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage

from componentpress.preview_coordinator import PreviewIdentity, PreviewRequest
from componentpress.rendering import render_component_image
from componentpress.rendering.image_quality import ImageQuality, measure_component_images
from componentpress.rendering.resources import ProjectResourceLoader
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.diagnostics.logging import log_exception


@dataclass(frozen=True)
class FontReadSucceeded:
    identity: PreviewIdentity
    files: tuple[tuple[Path, bytes], ...]


@dataclass(frozen=True)
class FontReadFailed:
    identity: PreviewIdentity
    error: Exception


@dataclass(frozen=True)
class PreviewRenderSucceeded:
    identity: PreviewIdentity
    image: QImage
    quality: tuple[ImageQuality, ...]


@dataclass(frozen=True)
class PreviewRenderFailed:
    identity: PreviewIdentity
    error: Exception


PreviewTaskResult: TypeAlias = FontReadSucceeded | FontReadFailed | PreviewRenderSucceeded | PreviewRenderFailed


class PreviewSignals(QObject):
    finished = Signal(object)


class PreviewTask(QRunnable):
    def __init__(self, request: PreviewRequest, signals: PreviewSignals):
        super().__init__()
        self.request = request
        self.signals = signals

    def run(self) -> None:
        try:
            image, quality = _render_preview(self.request)
        except Exception as exc:
            self.signals.finished.emit(PreviewRenderFailed(self.request.identity, _operation_error(exc, "render")))
        else:
            self.signals.finished.emit(PreviewRenderSucceeded(self.request.identity, image, quality))


class PreviewFontReadTask(QRunnable):
    def __init__(self, request: PreviewRequest, signals: PreviewSignals):
        super().__init__()
        self.request = request
        self.signals = signals

    def run(self) -> None:
        try:
            files = _read_preview_fonts(self.request)
        except Exception as exc:
            self.signals.finished.emit(FontReadFailed(self.request.identity, _operation_error(exc, "fonts")))
        else:
            self.signals.finished.emit(FontReadSucceeded(self.request.identity, files))


def _render_preview(request: PreviewRequest) -> tuple[QImage, tuple[ImageQuality, ...]]:
    loader = ProjectResourceLoader(request.snapshot.root)
    image = render_component_image(
        request.snapshot, request.identity.component_id, component=request.component,
        dpi=request.dpi, bindings_resolved=request.bindings_resolved, register_fonts=False,
        resource_loader=loader, preserve_logical_size=True,
    )
    quality = measure_component_images(
        request.component, request.snapshot.model, loader,
        request.snapshot.documents[request.identity.component_id].path,
        bindings_resolved=request.bindings_resolved,
    )
    return image, quality


def _read_preview_fonts(request: PreviewRequest) -> tuple[tuple[Path, bytes], ...]:
    folder = request.snapshot.root / "assets" / "fonts"
    if not folder.exists():
        return ()
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ProjectError(Diagnostic("PATH_SYMLINK", "", path, message_key="ui.font_symlink_unsupported"))
        if path.is_file() and path.suffix.lower() in (".ttf", ".otf"):
            files.append(_read_font(path))
    return tuple(files)


def _read_font(path: Path) -> tuple[Path, bytes]:
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
        raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "", path, message_key="ui.font_changed"))
    return path, data


def _operation_error(exc: Exception, phase: str) -> Exception:
    if isinstance(exc, ProjectError):
        return exc
    incident = log_exception(f"preview.{phase}", exc, operation="preview", phase=phase)
    return ProjectError(Diagnostic("PREVIEW_ERROR", "Unexpected preview error", incident_id=incident, message_key="error.preview_failed"))
