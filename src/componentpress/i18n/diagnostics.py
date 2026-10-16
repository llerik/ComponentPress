"""User-facing formatting for neutral project diagnostics."""

from collections.abc import Callable

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.i18n import catalogs, language, tr


def _location(diagnostic: Diagnostic, translate: Callable[..., str]) -> str:
    location = str(diagnostic.path) if diagnostic.path else translate("ui.diagnostic_project")
    if diagnostic.line is not None:
        location += f":{diagnostic.line}"
    if diagnostic.field:
        location += f" ({diagnostic.field})"
    details = [
        translate(f"ui.diagnostic_{name}", value=value)
        for name, value in (
            ("source", diagnostic.source), ("sheet", diagnostic.sheet),
            ("cell", diagnostic.cell), ("node", diagnostic.node_id),
        )
        if value
    ]
    return f"{location} [{', '.join(details)}]" if details else location


def _message(diagnostic: Diagnostic, locale: str, translate: Callable[..., str]) -> str:
    if diagnostic.message_key:
        return translate(diagnostic.message_key, **(diagnostic.params or {}))
    return diagnostic.message if locale == "ru" else translate("error.diagnostic", code=diagnostic.code)


def format_diagnostic(diagnostic: Diagnostic, *, locale: str | None = None) -> str:
    selected = locale if locale in catalogs() else language()
    translate = lambda key, **params: tr(key, locale=selected, **params)
    message = _message(diagnostic, selected, translate)
    if diagnostic.incident_id:
        incident = translate("error.incident_id", incident_id=diagnostic.incident_id)
        message = f"{message} [{incident}]"
    return f"{_location(diagnostic, translate)}: {diagnostic.code}: {message}"


def format_project_error(error: ProjectError, *, locale: str | None = None) -> str:
    return format_diagnostic(error.diagnostic, locale=locale)
