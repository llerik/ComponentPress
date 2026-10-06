"""Project font registration and requested-family validation."""

from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFontDatabase

from componentpress.domain.diagnostics import Diagnostic, ProjectError


class ProjectFonts:
    def __init__(self, root: Path):
        self.root = root
        self._ids: list[int] = []

    def register(self) -> None:
        folder = self.root / "assets" / "fonts"
        if not folder.exists():
            return
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise ProjectError(Diagnostic("PATH_SYMLINK", "символические ссылки шрифтов не поддерживаются", path))
            if not path.is_file() or path.suffix.lower() not in (".ttf", ".otf"):
                continue
            try:
                font_id = QFontDatabase.addApplicationFontFromData(QByteArray(path.read_bytes()))
            except OSError as exc:
                raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
            if font_id < 0:
                raise ProjectError(Diagnostic("FONT_INVALID", "не удалось зарегистрировать шрифт", path))
            self._ids.append(font_id)

    def require(self, family: str, *, owner: Path, field: str) -> None:
        available = {name.casefold() for name in QFontDatabase.families()}
        if family.casefold() not in available:
            raise ProjectError(Diagnostic("FONT_MISSING", f"шрифт {family!r} не найден", owner, field))

    def close(self) -> None:
        for font_id in reversed(self._ids):
            QFontDatabase.removeApplicationFont(font_id)
        self._ids.clear()

    def __enter__(self) -> "ProjectFonts":
        self.register()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
