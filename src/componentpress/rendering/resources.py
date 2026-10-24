"""Safe, project-relative image loading for standalone and HTML images."""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QImage

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.paths import check_exact_case, resolve_project_path


@dataclass(frozen=True)
class LoadedImage:
    image: QImage
    content_hash: str


class ProjectResourceLoader:
    """Reads each resource through the same validated project-path boundary."""

    def __init__(self, root: Path, max_cache_bytes: int = 64 * 1024 * 1024):
        self.root = root.resolve()
        self.max_cache_bytes = max_cache_bytes
        self._cache_bytes = 0
        self._path_hashes: dict[str, str] = {}
        self._images: OrderedDict[str, LoadedImage] = OrderedDict()

    def _cached_image(self, relative_path: str) -> LoadedImage | None:
        known_hash = self._path_hashes.get(relative_path)
        cached = self._images.get(known_hash) if known_hash is not None else None
        if cached is not None:
            self._images.move_to_end(known_hash)
        return cached

    def _read_bytes(self, relative_path: str, owner: Path | None, field: str | None) -> tuple[Path, bytes]:
        try:
            check_exact_case(self.root, relative_path)
            path = resolve_project_path(self.root, relative_path)
        except ProjectError as exc:
            raise ProjectError(Diagnostic(
                exc.diagnostic.code, exc.diagnostic.message, owner or exc.diagnostic.path, field,
                message_key=exc.diagnostic.message_key, params=exc.diagnostic.params,
                incident_id=exc.diagnostic.incident_id,
            )) from exc
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ProjectError(Diagnostic("RESOURCE_MISSING", f"изображение не найдено: {relative_path}", owner or path, field)) from exc
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), owner or path, field)) from exc
        return path, data

    def _decode(self, relative_path: str, path: Path, data: bytes,
                owner: Path | None, field: str | None) -> LoadedImage:
        image = QImage.fromData(QByteArray(data))
        if image.isNull():
            raise ProjectError(Diagnostic("RESOURCE_INVALID", f"не удалось прочитать изображение: {relative_path}", owner or path, field))
        return LoadedImage(image, hashlib.sha256(data).hexdigest())

    def _remember(self, relative_path: str, loaded: LoadedImage) -> LoadedImage:
        content_hash = loaded.content_hash
        cached = self._images.get(content_hash)
        if cached is not None:
            self._path_hashes[relative_path] = content_hash
            self._images.move_to_end(content_hash)
            return cached
        image_bytes = loaded.image.sizeInBytes()
        if image_bytes <= self.max_cache_bytes:
            while self._images and self._cache_bytes + image_bytes > self.max_cache_bytes:
                _, evicted = self._images.popitem(last=False)
                self._cache_bytes -= evicted.image.sizeInBytes()
            self._images[content_hash] = loaded
            self._path_hashes[relative_path] = content_hash
            self._cache_bytes += image_bytes
        return loaded

    def image(self, relative_path: str, *, owner: Path | None = None, field: str | None = None) -> LoadedImage:
        cached = self._cached_image(relative_path)
        if cached is not None:
            return cached
        path, data = self._read_bytes(relative_path, owner, field)
        return self._remember(relative_path, self._decode(relative_path, path, data, owner, field))
