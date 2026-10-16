"""Pure print-grid calculation in millimetres."""

from __future__ import annotations

from dataclasses import dataclass
import math

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.project import PrintSettings
from .input_snapshot import BuildInstance


@dataclass(frozen=True)
class Placement:
    instance_index: int
    copy_index: int
    component_id: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float


@dataclass(frozen=True)
class ImpositionPage:
    index: int
    placements: tuple[Placement, ...]
    columns: int = 0
    rows: int = 0


@dataclass(frozen=True)
class ImpositionPlan:
    paper_width_mm: float
    paper_height_mm: float
    pages: tuple[ImpositionPage, ...]


def cut_grid_lines(page: ImpositionPage, gap_mm: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Return unique vertical and horizontal cut coordinates for the full grid."""
    if not page.placements:
        return (), ()
    first = page.placements[0]
    vertical = {
        round(first.x_mm + column * (first.width_mm + gap_mm), 9)
        for column in range(page.columns)
    }
    vertical.update(
        round(first.x_mm + column * (first.width_mm + gap_mm) + first.width_mm, 9)
        for column in range(page.columns)
    )
    horizontal = {
        round(first.y_mm + row * (first.height_mm + gap_mm), 9)
        for row in range(page.rows)
    }
    horizontal.update(
        round(first.y_mm + row * (first.height_mm + gap_mm) + first.height_mm, 9)
        for row in range(page.rows)
    )
    return tuple(sorted(vertical)), tuple(sorted(horizontal))


def paper_size_mm(settings: PrintSettings) -> tuple[float, float]:
    width, height = (210.0, 297.0) if settings.paper == "A4" else (215.9, 279.4)
    return (height, width) if settings.orientation == "landscape" else (width, height)


def calculate_imposition(instances: tuple[BuildInstance, ...], settings: PrintSettings) -> ImpositionPlan:
    paper_width, paper_height = paper_size_mm(settings)
    pages: list[ImpositionPage] = []
    groups: dict[tuple[float, float], list[BuildInstance]] = {}
    for instance in instances:
        size = (instance.component.size_mm.width, instance.component.size_mm.height)
        groups.setdefault(size, []).append(instance)

    for (width, height), group in groups.items():
        cols = math.floor((paper_width - 2 * settings.margin_mm + settings.gap_mm) / (width + settings.gap_mm))
        rows = math.floor((paper_height - 2 * settings.margin_mm + settings.gap_mm) / (height + settings.gap_mm))
        if cols < 1 or rows < 1:
            instance = group[0]
            raise ProjectError(Diagnostic(
                "COMPONENT_DOES_NOT_FIT",
                f"компонент {instance.component_id!r} размером {width:g}×{height:g} мм не помещается на странице",
                instance.document_path,
            ))
        capacity = cols * rows
        placements: list[Placement] = []
        for instance in group:
            for copy_index in range(instance.copies):
                if len(placements) == capacity:
                    pages.append(ImpositionPage(len(pages), tuple(placements), cols, rows))
                    placements = []
                slot = len(placements)
                row, column = divmod(slot, cols)
                placements.append(Placement(
                    instance.index, copy_index, instance.component_id,
                    settings.margin_mm + column * (width + settings.gap_mm),
                    settings.margin_mm + row * (height + settings.gap_mm),
                    width, height,
                ))
        if placements:
            pages.append(ImpositionPage(len(pages), tuple(placements), cols, rows))
    return ImpositionPlan(paper_width, paper_height, tuple(pages))
