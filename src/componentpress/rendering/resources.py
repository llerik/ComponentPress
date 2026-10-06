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

    def image(self, relative_path: str, *, owner: Path | None = None, field: str | None = None) -> LoadedImage:
        known_hash = self._path_hashes.get(relative_path)
        cached = self._images.get(known_hash) if known_hash is not None else None
        if cached is not None:
            self._images.move_to_end(known_hash)
            return cached
        try:
            check_exact_case(self.root, relative_path)
            path = resolve_project_path(self.root, relative_path)
        except ProjectError as exc:
            raise ProjectError(Diagnostic(exc.diagnostic.code, exc.diagnostic.message, owner or exc.diagnostic.path, field)) from exc
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ProjectError(Diagnostic("RESOURCE_MISSING", f"изображение не найдено: {relative_path}", owner or path, field)) from exc
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), owner or path, field)) from exc
        image = QImage.fromData(QByteArray(data))
        if image.isNull():
            raise ProjectError(Diagnostic("RESOURCE_INVALID", f"не удалось прочитать изображение: {relative_path}", owner or path, field))
        content_hash = hashlib.sha256(data).hexdigest()
        cached = self._images.get(content_hash)
        if cached is not None:
            self._path_hashes[relative_path] = content_hash
            self._images.move_to_end(content_hash)
            return cached
        loaded = LoadedImage(image, content_hash)
        image_bytes = image.sizeInBytes()
        if image_bytes <= self.max_cache_bytes:
            while self._images and self._cache_bytes + image_bytes > self.max_cache_bytes:
                _, evicted = self._images.popitem(last=False)
                self._cache_bytes -= evicted.image.sizeInBytes()
            self._images[content_hash] = loaded
            self._path_hashes[relative_path] = content_hash
            self._cache_bytes += image_bytes
        return loaded
