"""Validated QTextDocument preparation in a fixed 96-DPI coordinate space."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import QRectF, QSizeF, QUrl
from PySide6.QtGui import QAbstractTextDocumentLayout, QFont, QImage, QPainter, QTextDocument

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import HtmlNode
from componentpress.project_io.yaml_codec import html_image_sources
from .geometry import HTML_DPI, mm_to_html_pixels
from .resources import ProjectResourceLoader


_TAGS = {"p", "br", "span", "b", "strong", "i", "em", "u", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "img"}
_GLOBAL_ATTRS = {"style", "align"}
_ATTRS = {
    "p": {"color"}, "span": {"color"}, "table": {"border", "cellspacing", "cellpadding", "width", "bgcolor"},
    "td": {"width", "bgcolor", "colspan", "rowspan", "valign"},
    "th": {"width", "bgcolor", "colspan", "rowspan", "valign"},
    "img": {"src", "width", "height", "alt", "valign"},
}
_CSS = {"color", "font-family", "font-size", "font-weight", "font-style", "text-decoration", "text-align", "background-color", "border", "border-color", "border-style", "border-width", "width", "padding", "margin", "margin-top", "margin-right", "margin-bottom", "margin-left", "vertical-align"}


class _HtmlValidator(HTMLParser):
    def __init__(self, owner: Path, field: str):
        super().__init__(convert_charrefs=True)
        self.owner = owner
        self.field = field

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in _TAGS:
            raise ProjectError(Diagnostic("HTML_UNSUPPORTED", f"тег <{tag}> не поддерживается", self.owner, self.field))
        allowed = _GLOBAL_ATTRS | _ATTRS.get(tag, set())
        for raw_name, value in attrs:
            name = raw_name.lower()
            if name not in allowed:
                raise ProjectError(Diagnostic("HTML_UNSUPPORTED", f"атрибут {name!r} тега <{tag}> не поддерживается", self.owner, self.field))
            if name == "style" and value:
                for declaration in value.split(";"):
                    if not declaration.strip():
                        continue
                    prop, separator, _ = declaration.partition(":")
                    if not separator or prop.strip().lower() not in _CSS:
                        raise ProjectError(Diagnostic("HTML_UNSUPPORTED", f"CSS-свойство {prop.strip()!r} не поддерживается", self.owner, self.field))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() not in _TAGS:
            raise ProjectError(Diagnostic("HTML_UNSUPPORTED", f"тег </{tag}> не поддерживается", self.owner, self.field))


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
    parser = _HtmlValidator(owner, field)
    try:
        parser.feed(html)
        parser.close()
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(Diagnostic("HTML_INVALID", str(exc), owner, field)) from exc

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
    document.setDefaultStyleSheet(f"body, p {{ margin: 0; color: {node.color}; }}")
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
