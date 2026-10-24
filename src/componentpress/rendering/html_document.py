"""Validated QTextDocument preparation in a fixed 96-DPI coordinate space."""

from pathlib import Path
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import QRectF, QSizeF, QUrl
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QColor, QFont, QImage, QPainter, QTextCharFormat,
    QTextCursor, QTextDocument, QTextFormat, QTextOption,
)
from PySide6.QtCore import Qt

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.html_resources import html_image_sources
from componentpress.domain.nodes import HtmlNode
from componentpress.bindings.html_policy import validate_html
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


def _apply_default_alignment(block, node: HtmlNode, cursor: QTextCursor) -> None:
    alignment = {
        "left": Qt.AlignmentFlag.AlignLeft,
        "center": Qt.AlignmentFlag.AlignHCenter,
        "right": Qt.AlignmentFlag.AlignRight,
    }[node.text_align]
    block_format = block.blockFormat()
    if not block_format.hasProperty(QTextFormat.Property.BlockAlignment):
        block_format.setAlignment(alignment)
        cursor.setPosition(block.position())
        cursor.setBlockFormat(block_format)


def _apply_default_color(block, color: QColor, cursor: QTextCursor) -> None:
    iterator = block.begin()
    while not iterator.atEnd():
        fragment = iterator.fragment()
        if fragment.isValid() and not fragment.charFormat().hasProperty(QTextFormat.Property.ForegroundBrush):
            text_format = QTextCharFormat()
            text_format.setForeground(color)
            cursor.setPosition(fragment.position())
            cursor.setPosition(fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(text_format)
        iterator += 1


def _apply_default_text_format(document: QTextDocument, node: HtmlNode) -> None:
    cursor = QTextCursor(document)
    color = QColor(node.color)
    block = document.begin()
    while block.isValid():
        _apply_default_alignment(block, node, cursor)
        _apply_default_color(block, color, cursor)
        block = block.next()


def _metrics_device() -> QImage:
    metrics = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    dpm = round(HTML_DPI / 0.0254)
    metrics.setDotsPerMeterX(dpm)
    metrics.setDotsPerMeterY(dpm)
    return metrics


def _text_document(node: HtmlNode, metrics: QImage) -> QTextDocument:
    document = QTextDocument()
    document.documentLayout().setPaintDevice(metrics)
    document.setUseDesignMetrics(True)
    font = QFont(node.font_family)
    font.setPointSizeF(node.font_size_pt)
    document.setDefaultFont(font)
    option = document.defaultTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    document.setDefaultTextOption(option)
    return document


def _validate_default_color(node: HtmlNode, owner: Path) -> None:
    color = QColor(node.color)
    if not color.isValid():
        raise ProjectError(Diagnostic("COLOR_INVALID", "ожидается цвет #RRGGBB или #AARRGGBB", owner, f"elements.{node.id}.color"))


def _load_inline_images(document: QTextDocument, html: str, node: HtmlNode,
                        loader: ProjectResourceLoader, owner: Path) -> None:
    field = f"elements.{node.id}.html"
    for source in html_image_sources(html):
        relative = _resource_path(source, owner, f"{field}.img.src")
        loaded = loader.image(relative, owner=owner, field=f"{field}.img.src")
        document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(source), loaded.image)


def _finish_document(document: QTextDocument, node: HtmlNode, html: str,
                     loader: ProjectResourceLoader, owner: Path) -> None:
    document.setDefaultStyleSheet(
        f"body, p {{ margin: 0; }} td, th {{ text-align: {node.text_align}; }}"
    )
    _load_inline_images(document, html, node, loader, owner)
    document.setHtml(html)
    _apply_default_text_format(document, node)
    document.setDocumentMargin(0)


def prepare_html(node: HtmlNode, html: str, loader: ProjectResourceLoader, owner: Path) -> PreparedHtml:
    validate_html(html, owner, f"elements.{node.id}.html")
    width_px, height_px = mm_to_html_pixels(node.width_mm), mm_to_html_pixels(node.height_mm)
    metrics = _metrics_device()
    document = _text_document(node, metrics)
    _validate_default_color(node, owner)
    _finish_document(document, node, html, loader, owner)
    document.setPageSize(QSizeF(width_px, -1))
    return PreparedHtml(document, metrics, width_px, height_px)
