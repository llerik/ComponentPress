"""Shared Prod/Test quantity selection."""

from __future__ import annotations

from typing import Literal

from .diagnostics import Diagnostic, ProjectError
from .values import DataRow

CopyMode = Literal["prod", "test"]


def validate_copy_mode(mode: str) -> CopyMode:
    if mode not in ("prod", "test"):
        raise ProjectError(Diagnostic("COPY_MODE_INVALID", f"неизвестный режим тиража: {mode!r}"))
    return mode  # type: ignore[return-value]


def copies_for_mode(row: DataRow, mode: str) -> int:
    selected = validate_copy_mode(mode)
    return row.copies if selected == "prod" else row.test_copies
