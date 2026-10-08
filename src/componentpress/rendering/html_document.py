"""Validated QTextDocument preparation in a fixed 96-DPI coordinate space."""

from pathlib import Path
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import QRectF, QSizeF, QUrl
from PySide6.QtGui import QAbstractTextDocumentLayout, QColor, QFont, QImage, QPainter, QTextDocument
from PySide6.QtCore import Qt

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import HtmlNode
from componentpress.bindings.html_policy import validate_html
from componentpress.project_io.yaml_codec import html_image_sources
from .geometry import HTML_DPI, mm_to_html_pixels
from .resources import ProjectResourceLoader


class PreparedHtml:
    def __init__(self, document: QTextDocument, metrics_device: QImage, width_px: float, height_px: float):
        self.document = document
        # QAbstractTextDocumentLayout stores a pointer to this device.
        self.metrics_device = metrics_device
        self.width_px = width_px
        self.height_px = height_px

    def draw(self, painter: QPainter) -> None:
        context = QAbstractTextDocumentLayout.PaintContext()
        context.clip = QRectF(0, 0, self.width_px, self.height_px)
        self.document.documentLayout().draw(painter, context)


def _resource_path(source: str, owner: Path, field: str) -> str:
    parts = urlsplit(source)
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        raise ProjectError(Diagnostic("RESOURCE_URL_UNSUPPORTED", "разрешены только локальные пути проекта", owner, field))
    value = unquote(parts.path)
    if value.startswith("/"):
        raise ProjectError(Diagnostic("PATH_INVALID", "путь изображения должен быть относительным", owner, field))
    return value


def prepare_html(node: HtmlNode, html: str, loader: ProjectResourceLoader, owner: Path) -> PreparedHtml:
    field = f"elements.{node.id}.html"
    validate_html(html, owner, field)

    width_px = mm_to_html_pixels(node.width_mm)
    height_px = mm_to_html_pixels(node.height_mm)
    metrics = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    dpm = round(HTML_DPI / 0.0254)
    metrics.setDotsPerMeterX(dpm)
    metrics.setDotsPerMeterY(dpm)
    document = QTextDocument()
    document.documentLayout().setPaintDevice(metrics)
    document.setUseDesignMetrics(True)
    font = QFont(node.font_family)
    font.setPointSizeF(node.font_size_pt)
    document.setDefaultFont(font)
    color = QColor(node.color)
    if not color.isValid():
        raise ProjectError(Diagnostic("COLOR_INVALID", "ожидается цвет #RRGGBB или #AARRGGBB", owner, f"elements.{node.id}.color"))
    css_color = (
        color.name(QColor.NameFormat.HexRgb)
        if color.alpha() == 255
        else f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alphaF():.4f})"
    )
    alignment = {
        "left": Qt.AlignmentFlag.AlignLeft,
        "center": Qt.AlignmentFlag.AlignHCenter,
        "right": Qt.AlignmentFlag.AlignRight,
    }[node.text_align]
    alignment_css = {"left": "left", "center": "center", "right": "right"}[node.text_align]
    document.setDefaultStyleSheet(
        f"body, p {{ margin: 0; color: {css_color}; text-align: {alignment_css}; }}"
    )
    for source in html_image_sources(html):
        relative = _resource_path(source, owner, f"{field}.img.src")
        loaded = loader.image(relative, owner=owner, field=f"{field}.img.src")
        document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(source), loaded.image)
    document.setHtml(html)
    document.setDocumentMargin(0)
    document.setPageSize(QSizeF(width_px, -1))
    layout = document.documentLayout()
    size = layout.documentSize()
    # A small tolerance absorbs sub-pixel font metric rounding, not actual content.
    if size.height() > height_px + 0.5 or document.idealWidth() > width_px + 0.5:
        raise ProjectError(Diagnostic("TEXT_OVERFLOW", f"содержимое HTML ({size.width():.1f}×{size.height():.1f} px) не помещается в блок ({width_px:.1f}×{height_px:.1f} px)", owner, field))
    return PreparedHtml(document, metrics, width_px, height_px)
