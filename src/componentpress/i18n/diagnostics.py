"""User-facing formatting for neutral project diagnostics."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

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


def _relative_project_path(path: Path | None, root: Path | None) -> Path | None:
    if path is None or root is None:
        return path
    try:
        return path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return path


def format_diagnostic_event(
    diagnostic: Diagnostic, *, project_root: Path | None,
    hide_project_path: bool, locale: str | None = None,
) -> str:
    selected = locale if locale in catalogs() else language()
    translate = lambda key, **params: tr(key, locale=selected, **params)
    shown_path = diagnostic.path
    if hide_project_path:
        relative = _relative_project_path(shown_path, project_root)
        shown_path = Path(translate("ui.diagnostic_project")) if relative == Path(".") else relative
    shown = replace(diagnostic, path=shown_path)
    message = _message(shown, selected, translate)
    if shown.incident_id:
        message = f"{message} [{translate('error.incident_id', incident_id=shown.incident_id)}]"
    return f"{message} — {_location(shown, translate)}: {shown.code}"


def format_project_error(error: ProjectError, *, locale: str | None = None) -> str:
    return format_diagnostic(error.diagnostic, locale=locale)
