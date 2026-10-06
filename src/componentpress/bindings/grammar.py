"""Parser for the two public placeholder forms; no template code is executed."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Reference:
    kind: str
    name: str
    offset: int


class GrammarError(ValueError):
    def __init__(self, code: str, message: str, offset: int):
        self.code = code
        self.offset = offset
        super().__init__(message)


_VAR = re.compile(r'''\{\{\s*vars\s*\[\s*(["'])([^"'\\]+)\1\s*\]\s*\}\}''')


def parse_references(text: str) -> tuple[Reference, ...]:
    refs: list[Reference] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in "{}":
            i += 2
            continue
        if text[i] == "}":
            raise GrammarError("TEMPLATE_SYNTAX", "лишняя закрывающая скобка", i)
        if text[i] != "{":
            i += 1
            continue
        if text.startswith("{{", i):
            match = _VAR.match(text, i)
            if match:
                refs.append(Reference("variable", match.group(2), i))
                i = match.end()
                continue
            code = "ROW_SYNTAX_UNSUPPORTED" if re.match(r"\{\{\s*row\b", text[i:]) else "TEMPLATE_SYNTAX"
            message = "используйте {Название} вместо {{ row[...] }}" if code == "ROW_SYNTAX_UNSUPPORTED" else "разрешена только запись {{ vars[\"ключ\"] }}"
            raise GrammarError(code, message, i)
        end = text.find("}", i + 1)
        if end < 0:
            raise GrammarError("TEMPLATE_SYNTAX", "незакрытая ссылка на столбец", i)
        name = text[i + 1:end].strip()
        if not name or "{" in name or "\\" in name:
            raise GrammarError("TEMPLATE_SYNTAX", "неверное имя столбца", i)
        refs.append(Reference("column", name, i))
        i = end + 1
    return tuple(refs)
