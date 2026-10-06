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
    component_id: str
    placements: tuple[Placement, ...]


@dataclass(frozen=True)
class ImpositionPlan:
    paper_width_mm: float
    paper_height_mm: float
    pages: tuple[ImpositionPage, ...]


def paper_size_mm(settings: PrintSettings) -> tuple[float, float]:
    width, height = (210.0, 297.0) if settings.paper == "A4" else (215.9, 279.4)
    return (height, width) if settings.orientation == "landscape" else (width, height)


def calculate_imposition(instances: tuple[BuildInstance, ...], settings: PrintSettings) -> ImpositionPlan:
    paper_width, paper_height = paper_size_mm(settings)
    pages: list[ImpositionPage] = []
    current_component: str | None = None
    current: list[Placement] = []
    current_capacity = 0
    columns = 0

    def finish_page() -> None:
        nonlocal current
        if current:
            pages.append(ImpositionPage(len(pages), current[0].component_id, tuple(current)))
            current = []

    for instance in instances:
        width = instance.component.size_mm.width
        height = instance.component.size_mm.height
        cols = math.floor((paper_width - 2 * settings.margin_mm + settings.gap_mm) / (width + settings.gap_mm))
        rows = math.floor((paper_height - 2 * settings.margin_mm + settings.gap_mm) / (height + settings.gap_mm))
        if cols < 1 or rows < 1:
            raise ProjectError(Diagnostic(
                "COMPONENT_DOES_NOT_FIT",
                f"компонент {instance.component_id!r} размером {width:g}×{height:g} мм не помещается на странице",
                instance.document_path,
            ))
        if instance.component_id != current_component:
            finish_page()
            current_component = instance.component_id
            current_capacity = cols * rows
            columns = cols
        for copy_index in range(instance.copies):
            if len(current) >= current_capacity:
                finish_page()
            slot = len(current)
            row, column = divmod(slot, columns)
            current.append(Placement(
                instance.index,
                copy_index,
                instance.component_id,
                settings.margin_mm + column * (width + settings.gap_mm),
                settings.margin_mm + row * (height + settings.gap_mm),
                width,
                height,
            ))
    finish_page()
    return ImpositionPlan(paper_width, paper_height, tuple(pages))
