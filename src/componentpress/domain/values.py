"""Immutable spreadsheet values shared by adapters, preview and future builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class FormulaState(str, Enum):
    NONE = "none"
    CACHED = "cached"
    EMPTY = "empty"
    MISSING = "missing"
    ERROR = "error"


ScalarValue = str | int | float | bool | date | datetime | time | Decimal | None


@dataclass(frozen=True)
class CellValue:
    value: ScalarValue
    coordinate: str
    formula_state: FormulaState = FormulaState.NONE
    formula: str | None = None


@dataclass(frozen=True)
class DataRow:
    row_number: int
    instance_id: str
    copies: int
    values: Mapping[str, CellValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class DataSheetSnapshot:
    source: str
    path: Path
    sheet: str
    content_hash: str
    version: int
    headers: tuple[str, ...]
    rows: tuple[DataRow, ...]
    request_id: int = 0
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def value_to_text(value: ScalarValue) -> str:
    """Stable, locale-independent representation required by the project format."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        if value.time() == time(0, 0):
            return value.date().isoformat()
        return value.isoformat(timespec="seconds")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        if not value.is_finite() if isinstance(value, Decimal) else not __import__("math").isfinite(value):
            raise ValueError("число должно быть конечным")
        if value == int(value):
            return str(int(value))
        return format(value, "f" if isinstance(value, Decimal) else ".15g")
    return str(value)
