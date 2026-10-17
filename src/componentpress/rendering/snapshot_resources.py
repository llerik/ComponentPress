"""Thread-owned image loader backed by captured build resources."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QImage

from componentpress.application.contracts import SnapshotResource
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.rendering.resources import LoadedImage


def _read_resource(resource: SnapshotResource, relative_path: str, field: str | None) -> bytes:
    if resource.data is not None:
        data = resource.data
    else:
        if resource.copied_path is None:
            raise RuntimeError("resource has no storage")
        try:
            data = resource.copied_path.read_bytes()
        except OSError as exc:
            raise ProjectError(Diagnostic("SNAPSHOT_READ", str(exc), resource.copied_path, field)) from exc
    if hashlib.sha256(data).hexdigest() != resource.content_hash:
        raise ProjectError(Diagnostic("SNAPSHOT_CORRUPT", f"повреждена копия ресурса {relative_path}", resource.copied_path, field))
    return data


def _decode_image(data: bytes, resource: SnapshotResource, relative_path: str,
                  owner: Path | None, field: str | None) -> LoadedImage:
    image = QImage.fromData(QByteArray(data))
    if image.isNull():
        raise ProjectError(Diagnostic("RESOURCE_INVALID", f"не удалось прочитать изображение: {relative_path}", owner, field))
    return LoadedImage(image, resource.content_hash)


class SnapshotResourceLoader:
    """Cache decoded images locally to the rendering thread."""

    def __init__(self, resources: Mapping[str, SnapshotResource], max_cache_bytes: int = 64 * 1024 * 1024):
        self.resources = resources
        self.max_cache_bytes = max_cache_bytes
        self._images: dict[str, LoadedImage] = {}
        self._cache_bytes = 0

    def image(self, relative_path: str, *, owner: Path | None = None, field: str | None = None) -> LoadedImage:
        resource = self.resources.get(relative_path)
        if resource is None:
            raise ProjectError(Diagnostic("RESOURCE_NOT_SNAPSHOTTED", f"ресурс не включён в снимок: {relative_path}", owner, field))
        cached = self._images.get(resource.content_hash)
        if cached is not None:
            return cached
        data = _read_resource(resource, relative_path, field)
        loaded = _decode_image(data, resource, relative_path, owner, field)
        size = loaded.image.sizeInBytes()
        if self._cache_bytes + size <= self.max_cache_bytes:
            self._images[resource.content_hash] = loaded
            self._cache_bytes += size
        return loaded
