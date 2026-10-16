"""One-active, latest-pending scheduling for editor preview work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from componentpress.application.contracts import ProjectSnapshot
from componentpress.domain.component import ComponentDefinition

class PreviewPhase(str, Enum):
    FONTS = "fonts"
    RENDER = "render"


@dataclass(frozen=True)
class PreviewIdentity:
    project: str
    component_id: str
    tab: str
    revision: int
    generation: int
    request: str


@dataclass(frozen=True)
class PreviewRequest:
    identity: PreviewIdentity
    snapshot: ProjectSnapshot
    component: ComponentDefinition
    dpi: int
    bindings_resolved: bool
    row_number: int | None
    selected_ids: tuple[str, ...]
    fit: bool


@dataclass(frozen=True)
class PreviewJob:
    request: PreviewRequest
    tab: object
    fonts: object


@dataclass(frozen=True)
class PreviewTransition:
    finished: PreviewJob | None = None
    start: PreviewJob | None = None


class PreviewCoordinator:
    """Track request identity and phase while retaining only the newest waiter."""

    def __init__(self) -> None:
        self.active: PreviewJob | None = None
        self.phase: PreviewPhase | None = None
        self.pending: PreviewJob | None = None
        self.closed = False

    def submit(self, job: PreviewJob) -> PreviewJob | None:
        if self.closed:
            return None
        if self.active is not None:
            self.pending = job
            return None
        self.active, self.phase = job, PreviewPhase.FONTS
        return job

    def begin_render(self, request: str) -> PreviewJob | None:
        if not self._matches(request, PreviewPhase.FONTS):
            return None
        self.phase = PreviewPhase.RENDER
        return self.active

    def accepts(self, request: str, phase: PreviewPhase) -> bool:
        return self._matches(request, phase)

    def finish(self, request: str, phase: PreviewPhase) -> PreviewTransition:
        if not self._matches(request, phase):
            return PreviewTransition()
        finished = self.active
        self.active = self.pending
        self.pending = None
        self.phase = PreviewPhase.FONTS if self.active is not None else None
        return PreviewTransition(finished, self.active)

    def discard_pending(self, tab: str | None = None) -> None:
        if self.pending is not None and (tab is None or self.pending.request.identity.tab == tab):
            self.pending = None

    def shutdown(self) -> None:
        self.closed = True
        self.pending = None

    def _matches(self, request: str, phase: PreviewPhase) -> bool:
        return bool(self.active and self.phase is phase and self.active.request.identity.request == request)
