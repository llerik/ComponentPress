"""PNG export and the stage-two single-component PDF proof."""

import os
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QMarginsF, QRectF, QSizeF
from PySide6.QtGui import QGuiApplication, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication

from componentpress.application.contracts import DocumentSnapshot, ProjectSnapshot, RenderResult
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, Node
from .fonts import ProjectFonts
from .geometry import MM_PER_INCH, pixel_size
from .painter import paint_component
from .resources import ProjectResourceLoader


_application: QApplication | None = None


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
        _write_pdf(image, temporary, target, width_mm, height_mm, dpi)
        os.replace(temporary, target)
    except ProjectError:
        raise
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_WRITE", str(exc), target)) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _write_pdf(image: QImage, temporary: Path, target: Path,
               width_mm: float, height_mm: float, dpi: int) -> None:
    writer = QPdfWriter(str(temporary))
    writer.setResolution(dpi)
    page = QPageSize(QSizeF(width_mm, height_mm), QPageSize.Unit.Millimeter,
                     "Component", QPageSize.SizeMatchPolicy.ExactMatch)
    writer.setPageSize(page)
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
    painter = QPainter(writer)
    if not painter.isActive():
        raise ProjectError(Diagnostic("PDF_WRITE", "Qt не смог начать запись PDF", target))
    try:
        painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
        painter.drawImage(QRectF(0, 0, writer.width(), writer.height()), image)
    finally:
        painter.end()


def _render_document(snapshot: ProjectSnapshot, component_id: str) -> DocumentSnapshot:
    document = snapshot.documents.get(component_id)
    if document is None:
        raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", "компонент не найден", snapshot.root))
    return document


def _render_inputs(snapshot: ProjectSnapshot, component_id: str,
                   component: ComponentDefinition | None, dpi: int,
                   resolved: bool | None) -> tuple[DocumentSnapshot, ComponentDefinition, bool]:
    document = _render_document(snapshot, component_id)
    if dpi <= 0:
        raise ProjectError(Diagnostic("DPI_INVALID", "DPI должен быть положительным", document.path))
    rendered = component if component is not None else document.model
    return document, rendered, component is not None if resolved is None else resolved


def _new_raster(component: ComponentDefinition, dpi: int) -> QImage:
    image = QImage(pixel_size(component.size_mm.width, component.size_mm.height, dpi), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDotsPerMeterX(round(dpi / 0.0254))
    image.setDotsPerMeterY(round(dpi / 0.0254))
    image.fill(0)
    return image


def _check_node_fonts(nodes: tuple[Node, ...], fonts: ProjectFonts, owner: Path) -> None:
    for node in nodes:
        if isinstance(node, HtmlNode):
            fonts.require(node.font_family, owner=owner, field=f"elements.{node.id}.font_family")
        elif isinstance(node, GroupNode):
            _check_node_fonts(node.children, fonts, owner)


def _paint_image(image: QImage, rendered: ComponentDefinition, loader: ProjectResourceLoader,
                 owner: Path, variables: dict[str, str], dpi: int, resolved: bool,
                 preserve_logical_size: bool) -> None:
    painter = QPainter(image)
    if not painter.isActive():
        raise ProjectError(Diagnostic("RENDER_INIT", "не удалось создать растровый холст", owner))
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.scale(dpi / MM_PER_INCH, dpi / MM_PER_INCH)
        paint_component(painter, rendered, loader, owner, variables, bindings_resolved=resolved)
    finally:
        painter.end()
    if preserve_logical_size:
        image.setDevicePixelRatio(dpi / 96.0)


def _render_with_fonts(image: QImage, rendered: ComponentDefinition, loader: ProjectResourceLoader,
                       project_root: Path,
                       owner: Path, variables: dict[str, str], dpi: int,
                       resolved: bool, register_fonts: bool, preserve_logical_size: bool) -> None:
    if register_fonts:
        with ProjectFonts(project_root) as fonts:
            _check_node_fonts(rendered.elements, fonts, owner)
            _paint_image(image, rendered, loader, owner, variables, dpi, resolved, preserve_logical_size)
    else:
        _paint_image(image, rendered, loader, owner, variables, dpi, resolved, preserve_logical_size)


def render_component_image(
    snapshot: ProjectSnapshot,
    component_id: str,
    *,
    component: ComponentDefinition | None = None,
    dpi: int = 96,
    bindings_resolved: bool | None = None,
    register_fonts: bool = True,
    resource_loader: ProjectResourceLoader | None = None,
    preserve_logical_size: bool = False,
) -> QImage:
    """Render an unpublished image for the editor or other in-memory previews."""
    ensure_gui_application()
    document, rendered, resolved = _render_inputs(snapshot, component_id, component, dpi, bindings_resolved)
    image = _new_raster(rendered, dpi)
    loader = resource_loader or ProjectResourceLoader(snapshot.root)
    _render_with_fonts(image, rendered, loader, snapshot.root, document.path,
                       snapshot.model.variables, dpi, resolved, register_fonts, preserve_logical_size)
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
