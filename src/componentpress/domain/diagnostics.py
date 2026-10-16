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
    message_key: str | None = None
    params: dict[str, object] | None = None
    incident_id: str | None = None

    def __str__(self) -> str:
        return self.code


class ProjectError(Exception):
    def __init__(self, diagnostic: Diagnostic):
        self.diagnostic = diagnostic
        super().__init__(diagnostic.code)
