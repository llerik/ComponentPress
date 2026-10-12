from pathlib import Path, PurePosixPath

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.files import atomic_create
from componentpress.project_io.paths import check_exact_case, resolve_project_path, validate_relative_path


class ResourceRepository:
    def project_data_path(self, root: Path, source: Path) -> str | None:
        root = root.resolve()
        try:
            relative = source.absolute().relative_to(root)
        except ValueError:
            return None
        value = PurePosixPath(relative).as_posix()
        check_exact_case(root, value)
        target = resolve_project_path(root, value)
        if target.is_file() and source.resolve() == target:
            return value
        return None

    def import_image(self, root: Path, source: Path, folder: str = "") -> str:
        if not source.is_file() or source.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ProjectError(Diagnostic("RESOURCE_INVALID", "нужен существующий PNG/JPEG", source))
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), source)) from exc
        signature_ok = (
            data.startswith(b"\x89PNG\r\n\x1a\n")
            if source.suffix.lower() == ".png"
            else data.startswith(b"\xff\xd8\xff")
        )
        if not signature_ok:
            raise ProjectError(Diagnostic("RESOURCE_INVALID", "содержимое не соответствует PNG/JPEG", source))
        folder_path = f"assets/images/{folder}" if folder else "assets/images"
        validate_relative_path(folder_path)
        relative = f"{folder_path}/{source.name}"
        check_exact_case(root, relative)
        target = resolve_project_path(root, relative)
        if not target.is_relative_to(resolve_project_path(root, "assets/images")):
            raise ProjectError(Diagnostic("PATH_ESCAPE", "ресурс вне assets/images", target))
        parent = target.parent
        for existing in parent.iterdir() if parent.exists() else ():
            if existing.name.casefold() == target.name.casefold():
                raise ProjectError(Diagnostic("FILE_EXISTS", "файл с таким именем уже есть", existing))
        atomic_create(target, data)
        return PurePosixPath(relative).as_posix()

    def import_data(self, root: Path, source: Path) -> str:
        if not source.is_file() or source.suffix.lower() != ".xlsx":
            raise ProjectError(Diagnostic("DATA_SOURCE_INVALID", "нужен существующий файл XLSX", source))
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), source)) from exc
        if not data.startswith(b"PK"):
            raise ProjectError(Diagnostic("DATA_SOURCE_INVALID", "содержимое не похоже на XLSX", source))
        project_path = self.project_data_path(root, source)
        if project_path is not None:
            return project_path
        relative = f"data/{source.name}"
        check_exact_case(root, relative)
        target = resolve_project_path(root, relative)
        for existing in target.parent.iterdir() if target.parent.exists() else ():
            if existing.name.casefold() == target.name.casefold():
                raise ProjectError(Diagnostic("FILE_EXISTS", "файл с таким именем уже есть", existing))
        atomic_create(target, data)
        return PurePosixPath(relative).as_posix()
