"""Project font registration and requested-family validation."""

from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFontDatabase

from componentpress.domain.diagnostics import Diagnostic, ProjectError


class ProjectFonts:
    def __init__(self, root: Path, families: set[str] | None = None):
        self.root = root
        self.families = None if families is None else {item.casefold() for item in families}
        self._ids: list[int] = []

    def register(self, captured: tuple[tuple[Path, bytes], ...] | None = None) -> None:
        folder = self.root / "assets" / "fonts"
        if not folder.exists():
            return
        candidates = captured
        if candidates is None:
            files = []
            for path in sorted(folder.rglob("*")):
                if path.is_symlink():
                    raise ProjectError(Diagnostic("PATH_SYMLINK", "символические ссылки шрифтов не поддерживаются", path))
                if path.is_file() and path.suffix.lower() in (".ttf", ".otf"):
                    try:
                        files.append((path, path.read_bytes()))
                    except OSError as exc:
                        raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
            candidates = tuple(files)
        for path, data in candidates:
            if not path.is_relative_to(folder):
                raise ProjectError(Diagnostic("PATH_ESCAPE", "шрифт находится вне каталога проекта", path))
            if self.families is not None:
                from PySide6.QtGui import QRawFont

                candidate = QRawFont(QByteArray(data), 16.0)
                if not candidate.isValid() or candidate.familyName().casefold() not in self.families:
                    continue
            font_id = QFontDatabase.addApplicationFontFromData(QByteArray(data))
            if font_id < 0:
                raise ProjectError(Diagnostic("FONT_INVALID", "не удалось зарегистрировать шрифт", path))
            self._ids.append(font_id)

    def register_captured(self, captured: tuple[tuple[Path, bytes], ...]) -> None:
        self.register(captured)

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
