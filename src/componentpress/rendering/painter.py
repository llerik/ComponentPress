"""Tree-order painting in millimetres."""

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QBrush

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, LineNode, Node, ShapeNode
from .geometry import HTML_DPI, MM_PER_INCH, image_fit_geometry
from .html_document import prepare_html
from .resources import ProjectResourceLoader


def _draw_image(painter: QPainter, node: ImageNode, loader: ProjectResourceLoader, owner: Path, x: float, y: float, source: str) -> None:
    loaded = loader.image(source, owner=owner, field=f"elements.{node.id}.source")
    image = loaded.image
    target = QRectF(x, y, node.width_mm, node.height_mm)
    painter.save()
    painter.setClipRect(target, Qt.ClipOperation.IntersectClip)
    tx, ty, tw, th, sx, sy, sw, sh = image_fit_geometry(
        image.width(), image.height(), x, y, node.width_mm, node.height_mm, node.fit
    )
    painter.drawImage(QRectF(tx, ty, tw, th), image, QRectF(sx, sy, sw, sh))
    painter.restore()


def paint_component(
    painter: QPainter,
    component: ComponentDefinition,
    loader: ProjectResourceLoader,
    owner: Path,
    variables: dict[str, str],
    *,
    bindings_resolved: bool = False,
) -> None:
    bounds = QRectF(0, 0, component.size_mm.width, component.size_mm.height)
    painter.save()
    painter.setClipRect(bounds, Qt.ClipOperation.ReplaceClip)
    painter.fillRect(bounds, QColor(component.background))

    def resolve(value: str, *, html: bool, field: str) -> str:
        if bindings_resolved:
            return value
        from componentpress.bindings.static import resolve_static
        return resolve_static(value, variables, html=html, owner=owner, field=field)

    def visit(nodes: tuple[Node, ...], offset_x: float, offset_y: float) -> None:
        for node in nodes:
            x, y = offset_x + node.x_mm, offset_y + node.y_mm
            if isinstance(node, ImageNode):
                source = resolve(node.source, html=False, field=f"elements.{node.id}.source")
                _draw_image(painter, node, loader, owner, x, y, source)
            elif isinstance(node, HtmlNode):
                html = resolve(node.html, html=True, field=f"elements.{node.id}.html")
                prepared = prepare_html(node, html, loader, owner)
                painter.save()
                painter.setClipRect(QRectF(x, y, node.width_mm, node.height_mm), Qt.ClipOperation.IntersectClip)
                painter.translate(x, y)
                painter.scale(MM_PER_INCH / HTML_DPI, MM_PER_INCH / HTML_DPI)
                prepared.draw(painter)
                painter.restore()
            elif isinstance(node, ShapeNode):
                painter.save()
                style = {"solid": Qt.PenStyle.SolidLine, "dash": Qt.PenStyle.DashLine, "dot": Qt.PenStyle.DotLine}[node.stroke_style]
                pen = QPen(QColor(node.stroke), 0, style)
                pen.setWidthF(node.stroke_width_mm)
                painter.setPen(pen)
                painter.setBrush(QBrush(QColor(node.fill)) if node.fill is not None else Qt.BrushStyle.NoBrush)
                rect = QRectF(x, y, node.width_mm, node.height_mm)
                if node.type == "rectangle":
                    painter.drawRect(rect)
                else:
                    painter.drawEllipse(rect)
                painter.restore()
            elif isinstance(node, LineNode):
                painter.save()
                style = {"solid": Qt.PenStyle.SolidLine, "dash": Qt.PenStyle.DashLine, "dot": Qt.PenStyle.DotLine}[node.stroke_style]
                pen = QPen(QColor(node.stroke), 0, style)
                pen.setWidthF(node.stroke_width_mm)
                painter.setPen(pen)
                painter.drawLine(x, y, x + node.dx_mm, y + node.dy_mm)
                painter.restore()
            elif isinstance(node, ConditionalGroupNode):
                if node.condition_matches is True:
                    visit(node.children, x, y)
            elif isinstance(node, GroupNode):
                visit(node.children, x, y)
            else:
                raise ProjectError(Diagnostic("NODE_UNSUPPORTED", f"тип узла {type(node).__name__} не поддерживается", owner))

    try:
        visit(component.elements, 0, 0)
    finally:
        painter.restore()
