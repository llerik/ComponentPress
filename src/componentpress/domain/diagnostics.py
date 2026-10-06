from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: Path | None = None
    field: str | None = None
    line: int | None = None
    severity: str = "error"
    source: str | None = None
    sheet: str | None = None
    cell: str | None = None
    node_id: str | None = None

    def __str__(self) -> str:
        location = str(self.path) if self.path else "проект"
        if self.line is not None:
            location += f":{self.line}"
        if self.field:
            location += f" ({self.field})"
        details = []
        if self.source:
            details.append(f"источник {self.source}")
        if self.sheet:
            details.append(f"лист {self.sheet}")
        if self.cell:
            details.append(f"ячейка {self.cell}")
        if self.node_id:
            details.append(f"элемент {self.node_id}")
        if details:
            location += " [" + ", ".join(details) + "]"
        return f"{location}: {self.code}: {self.message}"


class ProjectError(Exception):
    def __init__(self, diagnostic: Diagnostic):
        self.diagnostic = diagnostic
        super().__init__(str(diagnostic))
