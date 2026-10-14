from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
from uuid import uuid4

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.files import atomic_create, atomic_write, file_hash
from componentpress.project_io.paths import check_exact_case, resolve_project_path, validate_relative_path


class ResourceRepository:
    @staticmethod
    def list_images(root: Path) -> tuple[Path, ...]:
        images = resolve_project_path(root, "assets/images")
        found: list[Path] = []
        for folder, directories, files in os.walk(images, topdown=True, followlinks=False):
            current = Path(folder)
            directories[:] = [name for name in directories if not (current / name).is_symlink() and not (current / name).is_junction()]
            found.extend(
                current / name for name in files
                if (current / name).suffix.lower() in {".png", ".jpg", ".jpeg"}
                and not (current / name).is_symlink() and not (current / name).is_junction()
            )
        return tuple(sorted(found, key=lambda path: path.relative_to(root).as_posix().casefold()))

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
        if not source.is_file() or source.is_symlink() or source.is_junction() or source.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ProjectError(Diagnostic("RESOURCE_INVALID", "нужен существующий PNG/JPEG", source))
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), source)) from exc
        self._validate_image_bytes(source, data)
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

    def replace_image(self, root: Path, relative: str, source: Path, expected_hash: str) -> None:
        target = resolve_project_path(root, relative)
        images_root = resolve_project_path(root, "assets/images")
        if not target.is_relative_to(images_root) or source.suffix.lower() != target.suffix.lower():
            raise ProjectError(Diagnostic("RESOURCE_INVALID", "замена должна иметь тот же формат в assets/images", source))
        if not source.is_file() or source.is_symlink() or source.is_junction():
            raise ProjectError(Diagnostic("RESOURCE_INVALID", "выберите обычный файл изображения", source))
        data = source.read_bytes()
        self._validate_image_bytes(source, data)
        atomic_write(target, data, expected_hash)

    @staticmethod
    def _validate_image_bytes(source: Path, data: bytes) -> None:
        signature_ok = data.startswith(b"\x89PNG\r\n\x1a\n") if source.suffix.lower() == ".png" else data.startswith(b"\xff\xd8\xff")
        if not signature_ok:
            raise ProjectError(Diagnostic("RESOURCE_INVALID", "содержимое не соответствует PNG/JPEG", source))
        from PySide6.QtCore import QByteArray
        from PySide6.QtGui import QImage

        if QImage.fromData(QByteArray(data)).isNull():
            raise ProjectError(Diagnostic("RESOURCE_INVALID", "не удалось прочитать изображение", source))

    def import_image_folder(self, root: Path, source: Path, folder: str = "", *, cancellation=None, on_progress=None) -> tuple[str, ...]:
        from componentpress.project_io.resource_transactions import publish_resources

        root = root.resolve()
        images_root = resolve_project_path(root, "assets/images")
        source = Path(os.path.abspath(source))
        if any(part.is_symlink() or part.is_junction() for part in (source, *source.parents) if part.exists()):
            raise ProjectError(Diagnostic("RESOURCE_SOURCE_INVALID", "символические ссылки не допускаются", source))
        source = source.resolve()
        if source == images_root or source.is_relative_to(images_root):
            raise ProjectError(Diagnostic("RESOURCE_SOURCE_INVALID", "папку assets/images нельзя импортировать в неё же", source))
        if not source.is_dir() or source.is_symlink() or source.is_junction():
            raise ProjectError(Diagnostic("RESOURCE_SOURCE_INVALID", "выберите обычную исходную папку", source))
        destination = f"assets/images/{folder}" if folder else "assets/images"
        validate_relative_path(destination)
        target_root = resolve_project_path(root, destination)
        if not target_root.is_relative_to(images_root):
            raise ProjectError(Diagnostic("PATH_ESCAPE", "папка назначения вне assets/images", target_root))
        existing = {path.name.casefold() for path in target_root.iterdir()} if target_root.is_dir() else set()
        entries: list[tuple[Path, str, str, int]] = []
        collisions: set[str] = set()
        for current_name, directories, files in os.walk(source, topdown=True, followlinks=False):
            if cancellation is not None:
                cancellation.check()
            current = Path(current_name)
            directories[:] = [name for name in directories if not (current / name).is_symlink() and not (current / name).is_junction()]
            for name in files:
                item = current / name
                if item.is_symlink() or item.is_junction() or item.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                    continue
                relative = item.relative_to(source)
                flattened = "_".join((*relative.parts[:-1], relative.name))
                target = f"{destination}/{flattened}"
                validate_relative_path(target)
                folded = flattened.casefold()
                if folded in existing or folded in collisions:
                    raise ProjectError(Diagnostic("FILE_EXISTS", f"совпадающее имя ресурса: {flattened}", target_root / flattened))
                collisions.add(folded)
                data = item.read_bytes()
                self._validate_image_bytes(item, data)
                entries.append((item, target, hashlib.sha256(data).hexdigest(), len(data)))
        if not entries:
            return ()

        def verify() -> None:
            if cancellation is not None:
                cancellation.check()
            for item, target, digest, _size in entries:
                if file_hash(item) != digest:
                    raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "исходное изображение изменилось во время импорта", item))
                target_path = resolve_project_path(root, target)
                collision = target_path.exists()
                if not collision and target_path.parent.exists():
                    collision = any(sibling.name.casefold() == target_path.name.casefold() for sibling in target_path.parent.iterdir())
                if collision:
                    raise ProjectError(Diagnostic("FILE_EXISTS", f"совпадающее имя ресурса: {target_path.name}", target_path))

        publish_resources(root, entries, verify=verify, cancellation=cancellation, on_progress=on_progress)
        return tuple(target for _item, target, _digest, _size in entries)

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
