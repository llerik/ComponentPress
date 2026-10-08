"""Shared safe HTML subset used by Excel validation and Qt rendering."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from componentpress.domain.diagnostics import Diagnostic, ProjectError

TAGS = {"p", "br", "span", "b", "strong", "i", "em", "u", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "img"}
GLOBAL_ATTRS = {"align"}
ATTRS = {
    "p": {"color"}, "span": {"color"}, "table": {"border", "cellspacing", "cellpadding", "width", "bgcolor"},
    "td": {"width", "bgcolor", "colspan", "rowspan", "valign"},
    "th": {"width", "bgcolor", "colspan", "rowspan", "valign"},
    "img": {"src", "width", "height", "alt", "valign"},
}


class _Validator(HTMLParser):
    def __init__(self, owner: Path, field: str, *, allow_images: bool):
        super().__init__(convert_charrefs=True)
        self.owner = owner
        self.field = field
        self.allow_images = allow_images

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        self._tag(tag, [])

    def _tag(self, raw_tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = raw_tag.lower()
        if tag not in TAGS or (tag == "img" and not self.allow_images):
            raise ProjectError(Diagnostic("HTML_UNSUPPORTED", f"тег <{tag}> не поддерживается", self.owner, self.field))
        allowed = GLOBAL_ATTRS | ATTRS.get(tag, set())
        for raw_name, value in attrs:
            name = raw_name.lower()
            if name not in allowed:
                raise ProjectError(Diagnostic("HTML_UNSUPPORTED", f"атрибут {name!r} тега <{tag}> не поддерживается", self.owner, self.field))


def validate_html(html: str, owner: Path, field: str, *, allow_images: bool = True) -> None:
    parser = _Validator(owner, field, allow_images=allow_images)
    try:
        parser.feed(html)
        parser.close()
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(Diagnostic("HTML_INVALID", str(exc), owner, field)) from exc
