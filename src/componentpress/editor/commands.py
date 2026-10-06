"""Undo commands for validated document snapshots."""

from collections.abc import Callable

from PySide6.QtGui import QUndoCommand

from componentpress.application.sessions import DocumentSession
from componentpress.application.contracts import DocumentSnapshot


class ReplaceComponentCommand(QUndoCommand):
    def __init__(
        self,
        text: str,
        session: DocumentSession,
        before: DocumentSnapshot,
        after: DocumentSnapshot,
        refreshed: Callable[[], None],
    ) -> None:
        super().__init__(text)
        self._session = session
        self._before = before
        self._after = after
        self._refreshed = refreshed

    def undo(self) -> None:
        self._session.replace_snapshot(self._before)
        self._refreshed()

    def redo(self) -> None:
        self._session.replace_snapshot(self._after)
        self._refreshed()
