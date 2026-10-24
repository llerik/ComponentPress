"""Temporary editor state for one active canvas gesture."""

from dataclasses import dataclass, field

from PySide6.QtCore import QPointF

from componentpress.domain.component import ComponentDefinition


@dataclass
class MoveGesture:
    start: QPointF
    starts: dict[str, QPointF]
    component: ComponentDefinition
    context: object


@dataclass
class ResizeGesture:
    start: QPointF
    node_id: str
    handle: int
    rect: object
    local_start: QPointF
    component: ComponentDefinition
    context: object


@dataclass
class DrawGesture:
    start: QPointF
    tool: str
    preview: object
    component: ComponentDefinition
    context: object


@dataclass
class NudgeGesture:
    key: int
    node_ids: tuple[str, ...]
    starts: dict[str, QPointF]
    component: ComponentDefinition
    context: object
    delta: QPointF = field(default_factory=QPointF)


CanvasGesture = MoveGesture | ResizeGesture | DrawGesture | NudgeGesture
