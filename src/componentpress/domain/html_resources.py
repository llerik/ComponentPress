"""HTML resource references shared by project validation and rendering."""

from html.parser import HTMLParser


class _ImageSources(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            self.sources.extend(value for name, value in attrs if name.lower() == "src" and value is not None)


def html_image_sources(html: str) -> tuple[str, ...]:
    parser = _ImageSources()
    parser.feed(html)
    return tuple(parser.sources)
