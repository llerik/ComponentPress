"""Resolved render input that never mutates the source component template."""

from dataclasses import dataclass

from .component import ComponentDefinition
from .values import DataRow


@dataclass(frozen=True)
class ResolvedComponent:
    component: ComponentDefinition
    source_component_id: str
    row: DataRow | None
    data_version: int | None
