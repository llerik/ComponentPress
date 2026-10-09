"""Current shared project and component document schema."""

from typing import Any

SCHEMA_VERSION = 5


def require_current_schema(value: Any) -> int:
    """Require an actual integer using the one shared schema number."""
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValueError(f"неподдерживаемая версия схемы {value!r}")
    return value
