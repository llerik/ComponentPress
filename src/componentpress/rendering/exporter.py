"""PNG export and the stage-two single-component PDF proof."""

from dataclasses import dataclass
import os
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QMarginsF, QRectF, QSizeF
from PySide6.QtGui import QGuiApplication, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication

from componentpress.application.contracts import ProjectSnapshot
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, Node
from .fonts import ProjectFonts
from .geometry import MM_PER_INCH, pixel_size
from .painter import paint_component
from .resources import ProjectResourceLoader


_application: QApplication | None = None


@dataclass(frozen=True)
class RenderResult:
    png: Path
    pdf: Path | None
    width_px: int
    height_px: int
    dpi: int


def ensure_gui_application() -> QGuiApplication:
    global _application
    existing = QGuiApplication.instance()
    if existing is not None:
        return existing
    # A full QApplication also satisfies headless rendering and lets a later
    # editor window be created in the same process (notably in the test suite).
    _application = QApplication([])
    return _application


def _temporary_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.{uuid4().hex}.tmp")


def _save_png(image: QImage, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(target)
    try:
        if not image.save(str(temporary), "PNG"):
            raise ProjectError(Diagnostic("PNG_WRITE", "Qt не смог сохранить PNG", target))
        os.replace(temporary, target)
    except ProjectError:
        raise
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_WRITE", str(exc), target)) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _save_pdf(image: QImage, target: Path, width_mm: float, height_mm: float, dpi: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(target)
    try:
        writer = QPdfWriter(str(temporary))
        writer.setResolution(dpi)
        page = QPageSize(
            QSizeF(width_mm, height_mm),
            QPageSize.Unit.Millimeter,
            "Component",
            QPageSize.SizeMatchPolicy.ExactMatch,
        )
        writer.setPageSize(page)
        writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
        painter = QPainter(writer)
        if not painter.isActive():
            raise ProjectError(Diagnostic("PDF_WRITE", "Qt не смог начать запись PDF", target))
        try:
            target_rect = QRectF(0, 0, writer.width(), writer.height())
            painter.drawImage(target_rect, image)
        finally:
            painter.end()
        os.replace(temporary, target)
    except ProjectError:
        raise
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_WRITE", str(exc), target)) from exc
    finally:
        temporary.unlink(missing_ok=True)


def render_component_image(
    snapshot: ProjectSnapshot,
    component_id: str,
    *,
    component: ComponentDefinition | None = None,
    dpi: int = 96,
    bindings_resolved: bool | None = None,
) -> QImage:
    """Render an unpublished image for the editor or other in-memory previews."""
    ensure_gui_application()
    document = snapshot.documents.get(component_id)
    if document is None:
        raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", "компонент не найден", snapshot.root))
    if dpi <= 0:
        raise ProjectError(Diagnostic("DPI_INVALID", "DPI должен быть положительным", document.path))
    rendered = component if component is not None else document.model
    if bindings_resolved is None:
        # An explicitly supplied component is a ready-to-render value (normally
        # produced by PreviewService). The document model remains a template.
        bindings_resolved = component is not None
    size = pixel_size(rendered.size_mm.width, rendered.size_mm.height, dpi)
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    dpm = round(dpi / 0.0254)
    image.setDotsPerMeterX(dpm)
    image.setDotsPerMeterY(dpm)
    image.fill(0)
    loader = ProjectResourceLoader(snapshot.root)
    with ProjectFonts(snapshot.root) as fonts:
        def check_fonts(nodes: tuple[Node, ...]) -> None:
            for node in nodes:
                if isinstance(node, HtmlNode):
                    fonts.require(node.font_family, owner=document.path, field=f"elements.{node.id}.font_family")
                elif isinstance(node, GroupNode):
                    check_fonts(node.children)
        check_fonts(rendered.elements)
        painter = QPainter(image)
        if not painter.isActive():
            raise ProjectError(Diagnostic("RENDER_INIT", "не удалось создать растровый холст", document.path))
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.scale(dpi / MM_PER_INCH, dpi / MM_PER_INCH)
            paint_component(
                painter, rendered, loader, document.path, snapshot.model.variables,
                bindings_resolved=bindings_resolved,
            )
        finally:
            painter.end()
    return image


def render_component(snapshot: ProjectSnapshot, component_id: str, output: Path, *, component: ComponentDefinition | None = None, dpi: int | None = None, pdf: Path | None = None) -> RenderResult:
    if pdf is not None and output.resolve() == pdf.resolve():
        raise ProjectError(Diagnostic("OUTPUT_CONFLICT", "PNG и PDF должны иметь разные пути", output))
    document = snapshot.documents.get(component_id)
    if document is None:
        raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", "компонент не найден", snapshot.root))
    actual_dpi = dpi if dpi is not None else snapshot.model.print.dpi
    rendered = component if component is not None else document.model
    image = render_component_image(
        snapshot, component_id, component=rendered, dpi=actual_dpi,
        bindings_resolved=component is not None,
    )
    _save_png(image, output)
    if pdf is not None:
        _save_pdf(image, pdf, rendered.size_mm.width, rendered.size_mm.height, actual_dpi)
    return RenderResult(output, pdf, image.width(), image.height(), actual_dpi)
