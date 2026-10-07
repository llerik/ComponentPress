"""Stable technical identifiers allocated by application services."""

from __future__ import annotations

from collections.abc import Callable, Collection
import re
import unicodedata
from uuid import UUID, uuid4, uuid5


class IdentifierGenerator:
    """Generate a portable ID and retry until it is unique in the given scope."""

    def __init__(self, token_factory: Callable[[], str] | None = None) -> None:
        self._token_factory = token_factory or (lambda: uuid4().hex[:8])

    @staticmethod
    def _slug(value: str, fallback: str) -> str:
        ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
        if not slug or not slug[0].isalpha():
            slug = fallback
        return slug[:40].rstrip("-") or fallback

    def create(self, name: str, existing: Collection[str], *, prefix: str) -> str:
        base = self._slug(name, prefix)
        occupied = set(existing)
        for _attempt in range(100):
            token = re.sub(r"[^a-z0-9]", "", self._token_factory().lower())[:12]
            if not token:
                continue
            candidate = f"{base}-{token}"
            if candidate not in occupied:
                return candidate
        raise RuntimeError("не удалось создать уникальный технический ID")


_INSTANCE_NAMESPACE = UUID("1f8f7f5c-8b38-5f19-b836-1ce6c606865e")


def instance_id(project_id: str, component_id: str, sheet: str, row_number: int) -> str:
    """Stable row identity; moving a row intentionally changes its ID."""
    key = "\0".join((project_id, component_id, sheet, str(row_number)))
    return str(uuid5(_INSTANCE_NAMESPACE, key))
