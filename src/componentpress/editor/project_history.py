"""Undo/redo cursor for component membership owned by the editor."""

from componentpress.application.contracts import ComponentHistoryEntry


class ComponentHistory:
    def __init__(self, limit: int = 64):
        self.limit = limit
        self.entries: list[ComponentHistoryEntry] = []
        self.index = 0

    def undo_entry(self) -> ComponentHistoryEntry | None:
        return self.entries[self.index - 1] if self.index else None

    def redo_entry(self) -> ComponentHistoryEntry | None:
        return self.entries[self.index] if self.index < len(self.entries) else None

    def accept_undo(self) -> None:
        self.index -= 1

    def accept_redo(self) -> None:
        self.index += 1

    def append(self, entry: ComponentHistoryEntry) -> tuple[ComponentHistoryEntry, ...]:
        discarded = tuple(self.entries[self.index:])
        self.entries[self.index:] = [entry]
        if len(self.entries) > self.limit:
            discarded += (self.entries.pop(0),)
        self.index = len(self.entries)
        return discarded

    def clear(self) -> tuple[ComponentHistoryEntry, ...]:
        discarded = tuple(self.entries)
        self.entries.clear()
        self.index = 0
        return discarded
