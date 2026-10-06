from __future__ import annotations

from threading import Event

from componentpress.domain.diagnostics import Diagnostic, ProjectError


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise ProjectError(Diagnostic("BUILD_CANCELLED", "сборка отменена пользователем"))
