"""Registry of version-specific project data conversions."""

from dataclasses import dataclass
from typing import Callable

from . import v8_to_v9


@dataclass(frozen=True)
class SchemaMigration:
    id: str
    source_version: int
    target_version: int
    prepare: Callable
    validate_staged: Callable


# Register new version-specific modules here; existing conversions stay unchanged.
MIGRATIONS = (
    SchemaMigration(v8_to_v9.ID, v8_to_v9.SOURCE_VERSION, v8_to_v9.TARGET_VERSION,
                    v8_to_v9.prepare, v8_to_v9.validate_staged),
)
MIGRATIONS_BY_ID = {migration.id: migration for migration in MIGRATIONS}
MIGRATIONS_BY_SOURCE = {migration.source_version: migration for migration in MIGRATIONS}
