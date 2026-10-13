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
        message = self.message
        if self.message_key:
            from componentpress.i18n import tr

            message = tr(self.message_key, **(self.params or {}))
        else:
            from componentpress.i18n import language, tr

            if language() != "ru":
                message = tr("error.diagnostic", code=self.code)
        if self.incident_id:
            message = f"{message} [{tr('error.incident_id', incident_id=self.incident_id)}]"
        return f"{location}: {self.code}: {message}"


class ProjectError(Exception):
    def __init__(self, diagnostic: Diagnostic):
        self.diagnostic = diagnostic
        super().__init__(str(diagnostic))
