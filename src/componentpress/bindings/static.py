"""Stage-two substitution of project variables without evaluating template code."""

from html import escape
from pathlib import Path
import re

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from .grammar import GrammarError, parse_references


_VARIABLE = re.compile(r'''\{\{\s*vars\s*\[\s*(["'])([^"'\\]+)\1\s*\]\s*\}\}''')


def resolve_static(value: str, variables: dict[str, str], *, html: bool, owner: Path, field: str) -> str:
    try:
        references = parse_references(value)
    except GrammarError as exc:
        raise ProjectError(Diagnostic(exc.code, f"{exc} (символ {exc.offset + 1})", owner, field)) from exc
    pieces: list[str] = []
    cursor = 0
    for reference in references:
        pieces.append(value[cursor:reference.offset].replace(r"\{", "{").replace(r"\}", "}"))
        if reference.kind == "column":
            raise ProjectError(Diagnostic("BINDING_UNRESOLVED", f"для столбца {reference.name!r} нужна строка Excel (этап 6)", owner, field))
        match = _VARIABLE.match(value, reference.offset)
        if match is None:
            raise ProjectError(Diagnostic("TEMPLATE_SYNTAX", "не удалось разобрать переменную", owner, field))
        if reference.name not in variables:
            raise ProjectError(Diagnostic("VARIABLE_UNKNOWN", f"нет переменной {reference.name!r}", owner, field))
        replacement = variables[reference.name]
        if html:
            replacement = escape(replacement, quote=True)
        pieces.append(replacement)
        cursor = match.end()
    pieces.append(value[cursor:].replace(r"\{", "{").replace(r"\}", "}"))
    return "".join(pieces)
